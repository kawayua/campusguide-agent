"""语料 100 题在当前图流水线上的复测 runner。

- 测试集：``data/eval/answer_quality/gold_v1.jsonl``（100 题，as_of=2026-10-25）。
- 每题独立新会话；支持断点续跑与失败重跑。
- 遇到限流/熔断等待 60s 后重试；不修改冻结语料与历史报告。
- 输出独立目录：run/results/summary，并生成可供 audit_answer_quality.py
  使用的结果副本。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from campusguide.agents.providers import classify_error
from campusguide.eval.w3 import (
    build_run_snapshot,
    git_info,
    hash_configs,
    save_run,
    vector_manifest_info,
)
from campusguide.eval.w6b import row_from_graph
from campusguide.qa.runtime import RuntimePaths, create_graph_runtime

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "eval" / "answer_quality" / "gold_v1.jsonl"
DEFAULT_OUT = ROOT / "data" / "eval" / "answer_quality" / "corpus100_retest_20261030_run01"
DEFAULT_AS_OF = "2026-10-25"
CONFIG_FILES = [
    ROOT / "configs" / "graph.yaml",
    ROOT / "configs" / "handoff.yaml",
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "abstention.yaml",
    ROOT / "configs" / "aliases.yaml",
    ROOT / "configs" / "mcp.yaml",
]
RENDERED = {"ANSWERED", "PARTIAL_ANSWER"}
HARD_FAILURES = {"GENERATION_FAILED", "CLIENT_ERROR", "TOOL_UNAVAILABLE", "INDEX_UNAVAILABLE"}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _is_rate_limit_text(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "429" in lowered
        or "rate limit" in lowered
        or "rate_limit" in lowered
        or "too many requests" in lowered
        or "circuit_open" in lowered
        or "熔断" in lowered
        or "限流" in lowered
    )


def _rate_limit_signals(state: dict[str, Any]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for entry in state.get("trace") or []:
        if not isinstance(entry, dict):
            continue
        provider_error = entry.get("provider_error")
        if isinstance(provider_error, dict):
            if (
                provider_error.get("kind") in {"rate_limit", "circuit_open"}
                or provider_error.get("status_code") == 429
            ):
                signals.append(
                    {
                        "node": entry.get("node"),
                        "kind": provider_error.get("kind"),
                        "status_code": provider_error.get("status_code"),
                    }
                )
            elif _is_rate_limit_text(str(provider_error.get("message") or "")):
                signals.append({"node": entry.get("node"), "message": str(provider_error)[:200]})
        semantic = entry.get("semantic")
        if isinstance(semantic, dict) and semantic.get("error"):
            error = str(semantic["error"])
            if classify_error(None, error) == "rate_limit" or _is_rate_limit_text(error):
                signals.append({"node": "verifier.semantic", "message": error[:200]})
        error = entry.get("error")
        if isinstance(error, str) and (
            classify_error(None, error) == "rate_limit" or _is_rate_limit_text(error)
        ):
            signals.append({"node": entry.get("node"), "message": error[:200]})
    return signals


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _row_from_state(case: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": case["id"],
        "category": case.get("category", ""),
        "question": case["question"],
        "expected_outcome": "",
        "expected_article_ids": [],
        "block_types": [],
    }
    row = row_from_graph(item, state)
    row.update(
        {
            "id": case["id"],
            "allowed_resolution": case.get("allowed_resolution", "answer"),
            "required_checks": list(case.get("required_checks") or []),
            "scope_and_boundary": case.get("scope_and_boundary", ""),
            "baseline_review": case.get("baseline_review", ""),
            "baseline_complete_protected": bool(case.get("baseline_complete_protected")),
        }
    )
    return row


def _failure_row(case: dict[str, Any], *, error: str) -> dict[str, Any]:
    return {
        "id": case["id"],
        "category": case.get("category", ""),
        "question": case["question"],
        "expected_outcome": "",
        "expected_article_ids": [],
        "block_types": [],
        "outcome": "CLIENT_ERROR",
        "decision": None,
        "partial": False,
        "reason_code": "CLIENT_ERROR",
        "answer_markdown": "",
        "message": error[:300],
        "claims": [],
        "citations": [],
        "drafts": [],
        "rendered_claim_ids": [],
        "dropped_claim_ids": [],
        "issues": [],
        "scope_issues": [],
        "evidence": [],
        "retrieval": [],
        "queries": [],
        "index_version": "",
        "retrieval_timing": {},
        "retrieval_warnings": [],
        "mcp": {},
        "quote_repair": {},
        "probe": {},
        "probe_ms": 0,
        "qa_ms": 0,
        "duration_ms": 0,
        "llm_model": "",
        "retrieval_recall_at_8": None,
        "structure": {
            "citations_total": 0,
            "valid_citations": 0,
            "fabricated_id_count": 0,
            "wrong_part_count": 0,
            "quotes_total": 0,
            "quotes_located": 0,
            "pass_with_blocking": False,
        },
        "outcome_correct": False,
        "allowed_resolution": case.get("allowed_resolution", "answer"),
        "required_checks": list(case.get("required_checks") or []),
        "scope_and_boundary": case.get("scope_and_boundary", ""),
        "baseline_review": case.get("baseline_review", ""),
        "baseline_complete_protected": bool(case.get("baseline_complete_protected")),
        "runner_error": error[:1000],
    }


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _latest_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        qid = record.get("id")
        if qid not in latest:
            order.append(qid)
        latest[qid] = record
    return [latest[qid] for qid in order]


def _first_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        qid = record.get("id")
        if qid not in first:
            first[qid] = record
            order.append(qid)
    return [first[qid] for qid in order]


def _percentile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 1)


def _write_audit_input(out_dir: Path, records: list[dict[str, Any]]) -> None:
    """生成 audit_answer_quality.py 可直接读取的结果副本。"""
    with (out_dir / "results_audit.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            row = record.get("row") or {}
            handle.write(
                json.dumps(
                    {
                        "id": record["id"],
                        "question": record["question"],
                        "category": record.get("category", ""),
                        "reference_case_id": record["id"],
                        "session_id": record.get("session_id"),
                        "outcome": record.get("outcome"),
                        "answer": row.get("answer_markdown", ""),
                        "reply": {"citations": row.get("citations") or []},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _write_summary(
    out_dir: Path,
    *,
    records: list[dict[str, Any]],
    total: int,
    started_at: str,
    elapsed_s: float,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    all_records = records
    first_records = _first_records(records)
    records = _latest_records(records)
    rows = [record["row"] for record in records if record.get("row")]
    outcomes = Counter(row.get("outcome", "UNKNOWN") for row in rows)
    answer_cases = [row for row in rows if row.get("allowed_resolution") == "answer"]
    clarify_cases = [row for row in rows if row.get("allowed_resolution") == "clarification"]
    bounded_cases = [row for row in rows if row.get("allowed_resolution") == "bounded_abstention"]
    renderable = sum(1 for row in answer_cases if row.get("outcome") in RENDERED)
    empty_answers = sum(
        1
        for row in rows
        if row.get("outcome") in RENDERED and not (row.get("answer_markdown") or "").strip()
    )
    citations_total = sum(
        int((row.get("structure") or {}).get("citations_total", 0)) for row in rows
    )
    valid_citations = sum(
        int((row.get("structure") or {}).get("valid_citations", 0)) for row in rows
    )
    quotes_total = sum(int((row.get("structure") or {}).get("quotes_total", 0)) for row in rows)
    quotes_located = sum(int((row.get("structure") or {}).get("quotes_located", 0)) for row in rows)
    latencies = [int(row.get("qa_ms") or 0) for row in rows if row.get("qa_ms")]

    failures = []
    for record in records:
        row = record.get("row") or {}
        qid = record["id"]
        hard = row.get("outcome") in HARD_FAILURES
        empty = row.get("outcome") in RENDERED and not (row.get("answer_markdown") or "").strip()
        missing_answer = (
            row.get("allowed_resolution") == "answer" and row.get("outcome") not in RENDERED
        )
        if not (hard or empty or missing_answer):
            continue
        failures.append(
            {
                "id": qid,
                "allowed_resolution": row.get("allowed_resolution"),
                "outcome": row.get("outcome"),
                "rate_limit_hits": record.get("rate_limit_hits", 0),
                "reason": (row.get("message") or "")[:160],
                "answer_excerpt": (row.get("answer_markdown") or "")[:200],
                "runner_error": row.get("runner_error"),
            }
        )

    summary = {
        "schema": "corpus100-graph-retest-summary-v1",
        "started_at": started_at,
        "updated_at": datetime.now().astimezone().isoformat(),
        "elapsed_s": round(elapsed_s, 1),
        "total": total,
        "completed": len(records),
        "outcomes": dict(outcomes),
        "first_pass_outcomes": dict(
            Counter(
                (record.get("row") or {}).get("outcome", "UNKNOWN")
                for record in first_records
                if record.get("row")
            )
        ),
        "answer_cases": len(answer_cases),
        "renderable": {
            "count": renderable,
            "total": len(answer_cases),
            "rate": _rate(renderable, len(answer_cases)),
        },
        "empty_answers": empty_answers,
        "clarification_cases": len(clarify_cases),
        "bounded_abstention_cases": len(bounded_cases),
        "citations": {
            "total": citations_total,
            "valid": valid_citations,
            "valid_rate": _rate(valid_citations, citations_total),
            "quotes_total": quotes_total,
            "quotes_located": quotes_located,
            "quote_rate": _rate(quotes_located, quotes_total),
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "samples": len(latencies),
            "note": f"{metadata.get('device', 'cpu')} 嵌入/重排，仅供回归；与演示环境不可直接比较",
        },
        "rate_limited_cases": sum(1 for record in records if record.get("rate_limit_hits")),
        "rerun_cases": sorted({record["id"] for record in all_records if record.get("rerun")}),
        "failures": failures,
        "metadata": metadata,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    return summary


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _render_markdown(summary: dict[str, Any]) -> str:
    meta = summary.get("metadata") or {}
    lines = [
        "# 语料 100 题复测（当前图流水线）",
        "",
        f"- 运行目录：`{meta.get('output_dir', '')}`",
        f"- 测试集：`{meta.get('source', '')}`（{summary['total']} 题，as_of {meta.get('as_of', '')}）",
        f"- 流水线：{meta.get('pipeline', '')}",
        f"- 模型：`{json.dumps(meta.get('models', {}), ensure_ascii=False)}`",
        f"- prompt：`{json.dumps(meta.get('prompts', {}), ensure_ascii=False)}`",
        f"- 设备：{meta.get('device', '')}",
        f"- 限流重试：每题最多 {meta.get('retry_passes', 0)} 次，每次等待 {meta.get('retry_wait_s', 0)} 秒",
        f"- 完成：{summary['completed']}/{summary['total']}，用时 {summary['elapsed_s']} 秒",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 可作答题的渲染率 | {summary['renderable']['count']}/{summary['renderable']['total']}（{_pct(summary['renderable']['rate'])}） |",
        f"| ANSWERED / PARTIAL | {summary['outcomes'].get('ANSWERED', 0)} / {summary['outcomes'].get('PARTIAL_ANSWER', 0)} |",
        f"| 空答案 | {summary['empty_answers']} |",
        f"| 引用合法 | {summary['citations']['valid']}/{summary['citations']['total']}（{_pct(summary['citations']['valid_rate'])}） |",
        f"| quote 定位 | {summary['citations']['quotes_located']}/{summary['citations']['quotes_total']}（{_pct(summary['citations']['quote_rate'])}） |",
        f"| p50 / p95（qa_ms） | {summary['latency_ms']['p50']} / {summary['latency_ms']['p95']} ms |",
        f"| 触发限流的题 | {summary['rate_limited_cases']} |",
        f"| 重跑题数 | {len(summary['rerun_cases'])} |",
        "",
        "> 说明：outcome 只表示系统是否给出可渲染答案，不代表内容正确；内容正确性需人工/AI 逐题审计。",
        "",
        "## 非渲染 / 硬失败",
        "",
    ]
    failures = summary.get("failures") or []
    if failures:
        for item in failures:
            lines.append(
                f"- **{item['id']}** → {item['outcome']}（allowed={item['allowed_resolution']}）"
                + (
                    f"；限流重试 {item['rate_limit_hits']} 次"
                    if item.get("rate_limit_hits")
                    else ""
                )
            )
            if item.get("reason"):
                lines.append(f"  - 原因：{item['reason']}")
            if item.get("runner_error"):
                lines.append(f"  - Runner 异常：{item['runner_error']}")
    else:
        lines.append("无。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="逗号分隔的题目 id，用于烟雾测试")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--rerun-ids")
    parser.add_argument("--pre-wait", type=int, default=60)
    parser.add_argument("--retry-wait", type=int, default=60)
    parser.add_argument(
        "--case-delay",
        type=int,
        default=0,
        help="每题结束后的间隔秒数，用于降低供应商 RPM 压力",
    )
    parser.add_argument("--retry-passes", type=int, default=3)
    parser.add_argument("--max-consecutive-rate-limits", type=int, default=5)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    cases_all = [
        json.loads(line)
        for line in args.source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases_all = [case for case in cases_all if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases_all}
        if missing:
            parser.error(f"未知 id：{sorted(missing)}")
    if args.limit:
        cases_all = cases_all[: args.limit]

    started_at = datetime.now().astimezone().isoformat()
    started = time.monotonic()
    if args.device:
        os.environ["EMBEDDING_DEVICE"] = args.device
        os.environ["RERANKER_DEVICE"] = args.device

    existing = _load_records(results_path)
    latest_by_id = {record["id"]: record for record in _latest_records(existing)}
    if args.rerun_ids:
        wanted = {item.strip() for item in args.rerun_ids.split(",") if item.strip()}
        unknown = wanted - {case["id"] for case in cases_all}
        if unknown:
            parser.error(f"未知 id：{sorted(unknown)}")
        pending = [case for case in cases_all if case["id"] in wanted]
    elif args.rerun_failed:
        pending = [
            case
            for case in cases_all
            if case["id"] not in latest_by_id
            or (latest_by_id[case["id"]].get("row") or {}).get("outcome") in HARD_FAILURES
            or (
                (latest_by_id[case["id"]].get("row") or {}).get("outcome") in RENDERED
                and not (
                    (latest_by_id[case["id"]].get("row") or {}).get("answer_markdown") or ""
                ).strip()
            )
            or (
                (latest_by_id[case["id"]].get("row") or {}).get("allowed_resolution") == "answer"
                and (latest_by_id[case["id"]].get("row") or {}).get("outcome") not in RENDERED
            )
        ]
    else:
        pending = [case for case in cases_all if case["id"] not in latest_by_id]
    if pending and (args.rerun_failed or args.rerun_ids) and args.pre_wait > 0:
        print(f"等待 {args.pre_wait}s 后重跑 {len(pending)} 题（等待限流/熔断冷却）", flush=True)
        time.sleep(args.pre_wait)

    metadata: dict[str, Any] = {
        "output_dir": str(out_dir),
        "source": str(args.source),
        "as_of": args.as_of,
        "pipeline": "create_graph_runtime / GraphRunner (MCP stdio)",
        "device": args.device or "auto",
        "retry_passes": args.retry_passes,
        "retry_wait_s": args.retry_wait,
        "case_delay_s": args.case_delay,
        "started_at": started_at,
        "note": "corpus 100 regression on current graph pipeline; outcome is not a correctness grade",
    }
    saved_metadata = out_dir / "run_metadata.json"
    if args.summary_only and saved_metadata.exists():
        metadata = {**metadata, **json.loads(saved_metadata.read_text(encoding="utf-8"))}

    if not args.summary_only and pending:
        runtime = create_graph_runtime(paths=RuntimePaths(), today=date.fromisoformat(args.as_of))
        metadata.update(
            {
                "model_profile": runtime.model_profile,
                "models": dict(runtime.models),
                "prompts": dict(runtime.prompt_versions),
                "index_version": runtime.index_version,
                "vector_manifest": vector_manifest_info(ROOT / "data" / "vector_manifest.json"),
                "git": git_info(ROOT),
                "config_sha256": hash_configs(CONFIG_FILES),
            }
        )
        (out_dir / "run_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        consecutive_unresolved = 0
        try:
            for index, case in enumerate(pending, start=1):
                qid = case["id"]
                as_of = date.fromisoformat(args.as_of)
                attempts: list[dict[str, Any]] = []
                rate_limit_hits = 0
                state: dict[str, Any] | None = None
                last_error = ""
                session_id = ""
                for attempt in range(1, args.retry_passes + 2):
                    session_id = f"corpus100-{qid}-a{attempt}-{int(time.time())}"
                    attempt_started = time.monotonic()
                    try:
                        state = runtime.runner.invoke(
                            session_id,
                            case["question"],
                            as_of=as_of,
                        )
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        is_rate = _is_rate_limit_text(str(exc))
                        rate_limit_hits += int(is_rate)
                        attempts.append(
                            {
                                "attempt": attempt,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:500],
                                "rate_limit": is_rate,
                                "elapsed_s": round(time.monotonic() - attempt_started, 1),
                            }
                        )
                        state = None
                        if is_rate and attempt <= args.retry_passes:
                            print(
                                f"{qid} 限流（{last_error[:80]}），{args.retry_wait}s 后重试",
                                flush=True,
                            )
                            time.sleep(args.retry_wait)
                            continue
                        break
                    signals = _rate_limit_signals(state)
                    outcome = (state.get("outcome") or {}).get("code") or "UNKNOWN"
                    attempts.append(
                        {
                            "attempt": attempt,
                            "outcome": outcome,
                            "rate_limit": bool(signals),
                            "signals": signals,
                            "elapsed_s": round(time.monotonic() - attempt_started, 1),
                        }
                    )
                    if signals:
                        rate_limit_hits += 1
                        if attempt <= args.retry_passes:
                            print(
                                f"{qid} 限流 {signals[0]}，{args.retry_wait}s 后重试",
                                flush=True,
                            )
                            time.sleep(args.retry_wait)
                            continue
                    break

                if state is not None:
                    row = _row_from_state(case, state)
                    trace = _json_safe(state.get("trace") or [])
                else:
                    row = _failure_row(case, error=last_error or "无结果")
                    trace = []
                prior = latest_by_id.get(qid)
                record = {
                    "id": qid,
                    "question": case["question"],
                    "category": case.get("category", ""),
                    "allowed_resolution": case.get("allowed_resolution", "answer"),
                    "outcome": row.get("outcome"),
                    "answer": row.get("answer_markdown", ""),
                    "session_id": session_id,
                    "attempts": attempts,
                    "rate_limit_hits": rate_limit_hits,
                    "elapsed_s": round(sum(item.get("elapsed_s", 0) for item in attempts), 1),
                    "row": row,
                    "trace": trace,
                    "rerun": prior is not None,
                    "prior_outcome": (prior.get("row") or {}).get("outcome") if prior else None,
                }
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                print(
                    f"[{index:03d}/{len(pending)}] {qid} {row.get('outcome'):<18} "
                    f"rl={rate_limit_hits} {record['elapsed_s']}s",
                    flush=True,
                )

                unresolved = row.get("outcome") in HARD_FAILURES and rate_limit_hits > 0
                consecutive_unresolved = consecutive_unresolved + 1 if unresolved else 0
                if (
                    args.max_consecutive_rate_limits > 0
                    and consecutive_unresolved >= args.max_consecutive_rate_limits
                ):
                    print(
                        f"停止：连续 {consecutive_unresolved} 题限流后仍未恢复；"
                        "保留已写结果，稍后可用同一目录续跑。",
                        flush=True,
                    )
                    break
                if args.case_delay > 0 and index < len(pending):
                    time.sleep(args.case_delay)
        finally:
            runtime.close()

    records = _load_records(results_path)
    rows = [record["row"] for record in _latest_records(records) if record.get("row")]
    metadata.update(
        {"models": metadata.get("models") or {}, "prompts": metadata.get("prompts") or {}}
    )
    if rows:
        run = build_run_snapshot(
            dataset=args.source.name,
            dataset_path=args.source,
            rows=rows,
            metadata=metadata,
        )
        save_run(run, out_dir / "run.json")
        _write_audit_input(out_dir, _latest_records(records))
    summary = _write_summary(
        out_dir,
        records=records,
        total=len(cases_all),
        started_at=started_at,
        elapsed_s=time.monotonic() - started,
        metadata=metadata,
    )
    print(
        json.dumps(
            {
                "completed": summary["completed"],
                "total": summary["total"],
                "outcomes": summary["outcomes"],
                "renderable": summary["renderable"],
                "rate_limited_cases": summary["rate_limited_cases"],
                "hard_failures": [item["id"] for item in summary["failures"]],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if summary["completed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
