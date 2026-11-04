"""W7 冻结 50 题在当前图流水线上的复测 runner。

用途：代码改动后，用 W7 冻结集做回归；每题独立新会话，支持断点续跑。
遇到模型限流（429 / rate_limit）时等待 ``--retry-wait`` 秒后重试该题。

边界：
- 不修改 W7 冻结集、标签或历史报告；输出独立目录。
- 当前图流水线、模型与 W7 remediation4（legacy QAService +
  mimo-v2.6-pro-ultraspeed）不同，因此本 run 不重算 W7 正式门禁。
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
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.profiles import UserProfile
from campusguide.eval.w3 import (
    build_run_snapshot,
    git_info,
    hash_configs,
    save_run,
    vector_manifest_info,
)
from campusguide.eval.w6b import row_from_graph
from campusguide.eval.w7 import verify_frozen
from campusguide.qa.runtime import RuntimePaths, create_graph_runtime

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTSET = ROOT / "data" / "eval" / "test_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "eval" / "test_v1.manifest.json"
DEFAULT_OUT = ROOT / "data" / "eval" / "answer_quality" / "w7_retest_20261029_run01"
BASELINE_RUN = ROOT / "data" / "eval" / "w7_remediation4_run.json"
CONFIG_FILES = [
    ROOT / "configs" / "graph.yaml",
    ROOT / "configs" / "handoff.yaml",
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "abstention.yaml",
    ROOT / "configs" / "aliases.yaml",
    ROOT / "configs" / "mcp.yaml",
]
RENDERED = {"ANSWERED", "PARTIAL_ANSWER"}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _profile(case: dict[str, Any]) -> UserProfile | None:
    data = case.get("user_profile") or {}
    profile = UserProfile.model_validate(data)
    return None if profile.is_empty else profile


def _audience(case: dict[str, Any]) -> Audience | None:
    value = (case.get("user_profile") or {}).get("audience")
    return Audience(value) if value else None


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
                        "attempts": provider_error.get("attempts"),
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


def _failure_row(case: dict[str, Any], *, error: str) -> dict[str, Any]:
    return {
        "id": case["qid"],
        "qid": case["qid"],
        "category": case.get("category", ""),
        "question": case["question"],
        "expected_outcome": case.get("expected_outcome", ""),
        "expected_article_ids": list(case.get("gold_article_ids") or []),
        "block_types": list(case.get("block_types") or ["article"]),
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
        "should_abstain": bool(case.get("should_abstain")),
        "version_sensitive": bool(case.get("version_sensitive")),
        "gold_answer": case.get("gold_answer", ""),
        "required_facts": list(case.get("required_facts") or []),
        "user_profile": case.get("user_profile") or {},
        "runner_error": error[:1000],
    }


def _row_from_state(case: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": case["qid"],
        "category": case.get("category", ""),
        "question": case["question"],
        "expected_outcome": case.get("expected_outcome", ""),
        "expected_article_ids": list(case.get("gold_article_ids") or []),
        "block_types": list(case.get("block_types") or ["article"]),
    }
    row = row_from_graph(item, state)
    row.update(
        {
            "qid": case["qid"],
            "should_abstain": bool(case.get("should_abstain")),
            "version_sensitive": bool(case.get("version_sensitive")),
            "gold_answer": case.get("gold_answer", ""),
            "required_facts": list(case.get("required_facts") or []),
            "user_profile": case.get("user_profile") or {},
        }
    )
    return row


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
    expected = Counter(row.get("expected_outcome", "") for row in rows)
    outcome_match = sum(1 for row in rows if row.get("outcome_correct"))
    answerable = [row for row in rows if not row.get("should_abstain")]
    unanswerable = [row for row in rows if row.get("should_abstain")]
    false_refusals = sum(1 for row in answerable if row.get("outcome") not in RENDERED)
    correct_refusals = sum(1 for row in unanswerable if row.get("outcome") not in RENDERED)
    reason_correct = sum(
        1 for row in unanswerable if row.get("outcome") == row.get("expected_outcome")
    )
    citations_total = sum(
        int((row.get("structure") or {}).get("citations_total", 0)) for row in rows
    )
    valid_citations = sum(
        int((row.get("structure") or {}).get("valid_citations", 0)) for row in rows
    )
    fabricated = sum(
        int((row.get("structure") or {}).get("fabricated_id_count", 0)) for row in rows
    )
    quotes_total = sum(int((row.get("structure") or {}).get("quotes_total", 0)) for row in rows)
    quotes_located = sum(int((row.get("structure") or {}).get("quotes_located", 0)) for row in rows)
    recall_values = [
        float(row["retrieval_recall_at_8"])
        for row in rows
        if row.get("retrieval_recall_at_8") is not None
    ]
    latencies = sorted(int(row.get("qa_ms") or 0) for row in rows if row.get("qa_ms"))
    version_rows = [row for row in rows if row.get("version_sensitive")]
    version_match = sum(1 for row in version_rows if row.get("outcome_correct"))

    def percentile(values: list[int], ratio: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return float(values[0])
        position = (len(values) - 1) * ratio
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        weight = position - lower
        return round(values[lower] * (1 - weight) + values[upper] * weight, 1)

    failures = []
    for record in records:
        row = record.get("row") or {}
        qid = record["qid"]
        empty_answer = (
            row.get("outcome") in RENDERED
            and row.get("expected_outcome") == "ANSWERED"
            and not (row.get("answer_markdown") or "").strip()
        )
        if row.get("outcome_correct") and not empty_answer:
            continue
        failures.append(
            {
                "qid": qid,
                "expected": row.get("expected_outcome"),
                "actual": row.get("outcome"),
                "reason": (row.get("message") or "")[:160],
                "answer_excerpt": (row.get("answer_markdown") or "")[:240],
                "rate_limit_hits": record.get("rate_limit_hits", 0),
                "runner_error": row.get("runner_error"),
            }
        )

    changes_vs_w7: list[dict[str, Any]] = []
    if BASELINE_RUN.exists():
        baseline = json.loads(BASELINE_RUN.read_text(encoding="utf-8"))
        baseline_by_qid = {row.get("qid"): row for row in baseline.get("rows") or []}
        for row in rows:
            old = baseline_by_qid.get(row.get("qid"))
            if old is None:
                continue
            if old.get("outcome") != row.get("outcome"):
                changes_vs_w7.append(
                    {
                        "qid": row.get("qid"),
                        "expected": row.get("expected_outcome"),
                        "w7_remediation4": old.get("outcome"),
                        "current": row.get("outcome"),
                    }
                )

    summary = {
        "schema": "w7-graph-retest-summary-v1",
        "started_at": started_at,
        "updated_at": datetime.now().astimezone().isoformat(),
        "elapsed_s": round(elapsed_s, 1),
        "total": total,
        "completed": len(records),
        "outcomes": dict(outcomes),
        "expected_outcomes": dict(expected),
        "first_pass_outcomes": dict(
            Counter(
                (record.get("row") or {}).get("outcome", "UNKNOWN")
                for record in first_records
                if record.get("row")
            )
        ),
        "first_pass_outcome_match": {
            "count": sum(
                1 for record in first_records if (record.get("row") or {}).get("outcome_correct")
            ),
            "total": len(first_records),
        },
        "rerun_cases": sorted({record["qid"] for record in all_records if record.get("rerun")}),
        "outcome_changes_after_rerun": [
            {
                "qid": record["qid"],
                "initial": (first_records[index].get("row") or {}).get("outcome"),
                "latest": (record.get("row") or {}).get("outcome"),
            }
            for index, record in enumerate(records)
            if (record.get("row") or {}).get("outcome")
            != (first_records[index].get("row") or {}).get("outcome")
        ],
        "outcome_match": {
            "count": outcome_match,
            "total": len(rows),
            "rate": _rate(outcome_match, len(rows)),
        },
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "false_refusal": {"count": false_refusals, "rate": _rate(false_refusals, len(answerable))},
        "refusal_recall": {
            "count": correct_refusals,
            "rate": _rate(correct_refusals, len(unanswerable)),
        },
        "reason_code_accuracy": {
            "count": reason_correct,
            "rate": _rate(reason_correct, len(unanswerable)),
        },
        "citations": {
            "total": citations_total,
            "valid": valid_citations,
            "fabricated": fabricated,
            "valid_rate": _rate(valid_citations, citations_total),
            "quotes_total": quotes_total,
            "quotes_located": quotes_located,
            "quote_rate": _rate(quotes_located, quotes_total),
        },
        "recall_at_8": {
            "mean": round(sum(recall_values) / len(recall_values), 4) if recall_values else None,
            "cases": len(recall_values),
        },
        "version_sensitive": {
            "count": version_match,
            "total": len(version_rows),
            "rate": _rate(version_match, len(version_rows)),
        },
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "samples": len(latencies),
            "note": "CPU 嵌入/重排，仅供回归；与 GPU/演示环境不可直接比较",
        },
        "rate_limited_cases": sum(1 for record in records if record.get("rate_limit_hits")),
        "changes_vs_w7_remediation4": changes_vs_w7,
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
        "# W7 冻结 50 题复测（当前图流水线）",
        "",
        f"- 运行目录：`{meta.get('output_dir', '')}`",
        f"- 测试集：`{meta.get('testset', '')}`（sha256 `{meta.get('testset_sha256', '')[:16]}…`）",
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
        f"| outcome 与期望一致 | {summary['outcome_match']['count']}/{summary['outcome_match']['total']}（{_pct(summary['outcome_match']['rate'])}） |",
        f"| 引用合法 | {summary['citations']['valid']}/{summary['citations']['total']}（{_pct(summary['citations']['valid_rate'])}） |",
        f"| quote 定位 | {summary['citations']['quotes_located']}/{summary['citations']['quotes_total']}（{_pct(summary['citations']['quote_rate'])}） |",
        f"| 拒答召回 | {summary['refusal_recall']['count']}/{summary['unanswerable']}（{_pct(summary['refusal_recall']['rate'])}） |",
        f"| 误拒 | {summary['false_refusal']['count']}/{summary['answerable']}（{_pct(summary['false_refusal']['rate'])}） |",
        f"| Recall@8 | {summary['recall_at_8']['mean'] if summary['recall_at_8']['mean'] is not None else '—'}（{summary['recall_at_8']['cases']} 题有 gold id） |",
        f"| version-sensitive outcome | {summary['version_sensitive']['count']}/{summary['version_sensitive']['total']}（{_pct(summary['version_sensitive']['rate'])}） |",
        f"| p50 / p95（qa_ms） | {summary['latency_ms']['p50']} / {summary['latency_ms']['p95']} ms |",
        f"| 触发限流的题 | {summary['rate_limited_cases']} |",
        f"| 首轮 outcome 一致 | {summary['first_pass_outcome_match']['count']}/{summary['first_pass_outcome_match']['total']} |",
        f"| 重跑题数 | {len(summary['rerun_cases'])} |",
        "",
        "> 说明：本 run 使用当前图流水线与当前模型，不是 W7 remediation4 的原样重跑；"
        "support/answer 人工标签未重标，不能据此重算 W7 正式门禁。",
        "",
        "## 相对 W7 remediation4 的 outcome 变化",
        "",
    ]
    changes = summary.get("changes_vs_w7_remediation4") or []
    if changes:
        lines += ["| qid | 期望 | W7 remediation4 | 本次 |", "|---|---|---|---|"]
        lines += [
            f"| {item['qid']} | {item['expected']} | {item['w7_remediation4']} | {item['current']} |"
            for item in changes
        ]
    else:
        lines.append("无（或基线 run 不可用）。")
    rerun_changes = summary.get("outcome_changes_after_rerun") or []
    lines += ["", "## 重跑前后变化", ""]
    if rerun_changes:
        lines += ["| qid | 首轮 | 最新 |", "|---|---|---|"]
        lines += [
            f"| {item['qid']} | {item['initial']} | {item['latest']} |" for item in rerun_changes
        ]
    else:
        lines.append("无（或未重跑）。")
    lines += ["", "## 未达期望 / 需人工复核", ""]
    failures = summary.get("failures") or []
    if failures:
        for item in failures:
            lines.append(
                f"- **{item['qid']}** 期望 {item['expected']}，实际 {item['actual']}"
                + (
                    f"；限流重试 {item['rate_limit_hits']} 次"
                    if item.get("rate_limit_hits")
                    else ""
                )
            )
            if item.get("reason"):
                lines.append(f"  - 原因：{item['reason']}")
            if item.get("answer_excerpt"):
                lines.append(f"  - 答案摘录：{item['answer_excerpt'].replace(chr(10), ' ')}")
            if item.get("runner_error"):
                lines.append(f"  - Runner 异常：{item['runner_error']}")
    else:
        lines.append("无。")
    lines.append("")
    return "\n".join(lines)


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _latest_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 qid 的复跑记录以最后一条为准；保留首次出现顺序。"""
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        qid = record.get("qid")
        if qid not in latest:
            order.append(qid)
        latest[qid] = record
    return [latest[qid] for qid in order]


def _first_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        qid = record.get("qid")
        if qid not in first:
            first[qid] = record
            order.append(qid)
    return [first[qid] for qid in order]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="逗号分隔的 qid，用于烟雾测试")
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="只重跑最近一次结果与期望不符（或异常）的题",
    )
    parser.add_argument("--rerun-ids", help="逗号分隔的 qid，指定重跑")
    parser.add_argument(
        "--pre-wait",
        type=int,
        default=60,
        help="开始重跑前的等待秒数，用于等待熔断/限流冷却",
    )
    parser.add_argument("--retry-wait", type=int, default=60, help="限流后等待秒数")
    parser.add_argument("--retry-passes", type=int, default=3, help="每题限流重试次数")
    parser.add_argument(
        "--max-consecutive-rate-limits",
        type=int,
        default=5,
        help="连续多少题重试后仍限流则停止，避免整批空转",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="嵌入/重排设备；默认 cpu，避免与 7860 演示进程争显存",
    )
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"

    rows_all, frozen = verify_frozen(
        testset_path=args.testset,
        manifest_path=args.manifest,
    )
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        rows_all = [row for row in rows_all if row["qid"] in wanted]
        missing = wanted - {row["qid"] for row in rows_all}
        if missing:
            parser.error(f"未知 qid：{sorted(missing)}")
    if args.limit:
        rows_all = rows_all[: args.limit]

    started_at = datetime.now().astimezone().isoformat()
    started = time.monotonic()
    if args.device:
        os.environ["EMBEDDING_DEVICE"] = args.device
        os.environ["RERANKER_DEVICE"] = args.device

    existing = _load_records(results_path)
    latest_by_qid = {record["qid"]: record for record in _latest_records(existing)}
    if args.rerun_ids:
        wanted = {item.strip() for item in args.rerun_ids.split(",") if item.strip()}
        unknown = wanted - {row["qid"] for row in rows_all}
        if unknown:
            parser.error(f"未知 qid：{sorted(unknown)}")
        pending = [row for row in rows_all if row["qid"] in wanted]
    elif args.rerun_failed:
        pending = [
            row
            for row in rows_all
            if row["qid"] not in latest_by_qid
            or not (latest_by_qid[row["qid"]].get("row") or {}).get("outcome_correct")
        ]
    else:
        pending = [row for row in rows_all if row["qid"] not in latest_by_qid]
    if pending and (args.rerun_failed or args.rerun_ids) and args.pre_wait > 0:
        print(f"等待 {args.pre_wait}s 后重跑 {len(pending)} 题（等待限流/熔断冷却）", flush=True)
        time.sleep(args.pre_wait)

    metadata: dict[str, Any] = {
        "output_dir": str(out_dir),
        "testset": str(args.testset),
        "testset_sha256": frozen["sha256"],
        "pipeline": "create_graph_runtime / GraphRunner (MCP stdio)",
        "device": args.device or "auto",
        "retry_passes": args.retry_passes,
        "retry_wait_s": args.retry_wait,
        "started_at": started_at,
        "note": "W7 frozen set reused for regression; graph pipeline and current models differ from W7 remediation4",
    }

    if not args.summary_only and pending:
        runtime = create_graph_runtime(paths=RuntimePaths(), today=date(2026, 10, 24))
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
                qid = case["qid"]
                as_of = date.fromisoformat(case["as_of"])
                profile = _profile(case)
                audience = _audience(case)
                block_types = [BlockType(value) for value in case.get("block_types") or ["article"]]
                attempts: list[dict[str, Any]] = []
                rate_limit_hits = 0
                state: dict[str, Any] | None = None
                last_error = ""
                for attempt in range(1, args.retry_passes + 2):
                    session_id = f"w7retest-{qid}-a{attempt}-{int(time.time())}"
                    attempt_started = time.monotonic()
                    try:
                        state = runtime.runner.invoke(
                            session_id,
                            case["question"],
                            as_of=as_of,
                            audience=audience,
                            profile=profile,
                            block_types=block_types,
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
                prior = latest_by_qid.get(qid)
                record = {
                    "qid": qid,
                    "question": case["question"],
                    "category": case.get("category", ""),
                    "expected_outcome": case.get("expected_outcome", ""),
                    "outcome": row.get("outcome"),
                    "answer": row.get("answer_markdown", ""),
                    "attempts": attempts,
                    "rate_limit_hits": rate_limit_hits,
                    "elapsed_s": round(sum(item.get("elapsed_s", 0) for item in attempts), 1),
                    "row": row,
                    "trace": trace,
                    "rerun": prior is not None,
                    "prior_outcome": (
                        (prior.get("row") or {}).get("outcome") if prior is not None else None
                    ),
                }
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                existing.append(record)
                print(
                    f"[{index:02d}/{len(pending)}] {qid} {row.get('outcome'):<18} "
                    f"expected={row.get('expected_outcome'):<22} "
                    f"rl={rate_limit_hits} {record['elapsed_s']}s",
                    flush=True,
                )

                unresolved = (
                    row.get("outcome") in {"GENERATION_FAILED", "CLIENT_ERROR"}
                    and rate_limit_hits > 0
                )
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
        finally:
            runtime.close()

    records = _load_records(results_path)
    rows = [record["row"] for record in _latest_records(records) if record.get("row")]
    metadata.update(
        {
            "models": metadata.get("models") or {},
            "prompts": metadata.get("prompts") or {},
        }
    )
    if rows:
        run = build_run_snapshot(
            dataset=args.testset.name,
            dataset_path=args.testset,
            rows=rows,
            metadata=metadata,
        )
        run["frozen"] = True
        run["w7_testset_sha256"] = frozen["sha256"]
        save_run(run, out_dir / "run.json")
    summary = _write_summary(
        out_dir,
        records=records,
        total=len(rows_all),
        started_at=started_at,
        elapsed_s=time.monotonic() - started,
        metadata=metadata,
    )
    print(
        json.dumps(
            {
                "completed": summary["completed"],
                "total": summary["total"],
                "outcome_match": summary["outcome_match"],
                "rate_limited_cases": summary["rate_limited_cases"],
                "failures": [item["qid"] for item in summary["failures"]],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if summary["completed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
