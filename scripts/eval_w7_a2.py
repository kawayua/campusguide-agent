"""W7 A2 Verifier 消融：eval-only pass-through verifier 跑 50 题。

关闭 W3 核验后直接渲染 draft，用于隔离 Verifier 的贡献；结构门禁不适用。
support 必须全量人工标注，AI 预标注不计入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from campusguide.agents.advisor import PROMPT_VERSION
from campusguide.config import load_retrieval_config
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.profiles import UserProfile
from campusguide.env import load_dotenv
from campusguide.eval.w3 import (
    build_run_snapshot,
    finalize_run,
    git_info,
    hash_configs,
    load_run,
    render_label_sheet,
    row_from_result,
    save_run,
    vector_manifest_info,
    write_labels,
)
from campusguide.eval.w7 import load_jsonl, refusal_summary, support_summary, verify_frozen
from campusguide.qa.runtime import RuntimePaths, create_mcp_qa_service
from campusguide.verification.passthrough import PassThroughVerifier

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTSET = ROOT / "data" / "eval" / "test_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "eval" / "test_v1.manifest.json"
DEFAULT_BASELINE = ROOT / "data" / "eval" / "w7_remediation2_run.json"
DEFAULT_BASELINE_LABELS = ROOT / "data" / "eval" / "w7_remediation2_support_labels.jsonl"
DEFAULT_RUN = ROOT / "data" / "eval" / "w7_a2_verifier_run.json"
DEFAULT_LABELS = ROOT / "data" / "eval" / "w7_a2_support_labels.jsonl"
DEFAULT_REVIEW = ROOT / "data" / "eval" / "w7_a2_support_review.md"
DEFAULT_JSON = ROOT / "data" / "eval" / "w7-a2-verifier.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w7-a2-verifier.md"
CONFIG_FILES = [
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "abstention.yaml",
    ROOT / "configs" / "aliases.yaml",
    ROOT / "configs" / "mcp.yaml",
]


def _profile(row: dict[str, Any]) -> UserProfile:
    return UserProfile.model_validate(row.get("user_profile") or {})


def _audience(row: dict[str, Any]) -> Audience | None:
    value = (row.get("user_profile") or {}).get("audience")
    return Audience(value) if value else None


def _metadata(
    rows: list[dict[str, Any]],
    *,
    warmup_ms: int,
    text_manifest: Path,
) -> dict[str, Any]:
    retrieval = load_retrieval_config(ROOT / "configs" / "retrieval.yaml")
    models = sorted({row.get("llm_model") for row in rows if row.get("llm_model")})
    return {
        **git_info(ROOT),
        "model": ", ".join(models) or "n/a",
        "advisor_prompt_version": PROMPT_VERSION,
        "embedding_model": retrieval.embedding_model,
        "vector_manifest": vector_manifest_info(ROOT / "data" / "vector_manifest.json"),
        "config_hash": hash_configs(CONFIG_FILES),
        "config_files": [path.relative_to(ROOT).as_posix() for path in CONFIG_FILES],
        "warmup_ms": warmup_ms,
        "latency_scope": "warm" if warmup_ms else "cold",
        "limit": None,
        "text_manifest": str(text_manifest),
        "a2_no_verifier": True,
    }


def _run_a2(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    service = create_mcp_qa_service(verifier=PassThroughVerifier())
    warmup_ms = 0
    rows: list[dict[str, Any]] = []
    try:
        started = time.perf_counter()
        service.warmup()
        warmup_ms = int((time.perf_counter() - started) * 1000)
        print(f"warmup: {warmup_ms}ms", flush=True)
        for index, item in enumerate(items, start=1):
            block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
            result = service.ask(
                item["question"],
                audience=_audience(item),
                profile=None if _profile(item).is_empty else _profile(item),
                block_types=block_types,
                as_of=date.fromisoformat(item["as_of"]),
            )
            row = row_from_result(
                {
                    "id": item["qid"],
                    "question": item["question"],
                    "category": item.get("category"),
                    "expected_outcome": item.get("expected_outcome"),
                    "expected_article_ids": item.get("gold_article_ids") or [],
                    "block_types": [block_type.value for block_type in block_types],
                },
                result,
            )
            row.update(
                {
                    "qid": item["qid"],
                    "category": item.get("category"),
                    "should_abstain": bool(item.get("should_abstain")),
                    "version_sensitive": bool(item.get("version_sensitive")),
                    "gold_answer": item.get("gold_answer", ""),
                    "required_facts": item.get("required_facts") or [],
                    "user_profile": item.get("user_profile") or {},
                }
            )
            rows.append(row)
            print(
                f"[{index}/{len(items)}] {item['qid']} {row['outcome']} "
                f"claims={len(row.get('claims') or [])} qa={row.get('qa_ms')}ms",
                flush=True,
            )
    finally:
        service.close()
    metadata = _metadata(rows, warmup_ms=warmup_ms, text_manifest=RuntimePaths().text_manifest)
    return rows, metadata


def _labels_from_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    """A2 support 标签以最终渲染 claim 为单位；pass-through 不产生 W3 记录。"""
    run_sha256 = run.get("run_sha256") or ""
    records: list[dict[str, Any]] = []
    for row in run.get("rows") or []:
        for claim in row.get("claims") or []:
            citations = claim.get("citations") or []
            quotes = [str(item.get("quote") or "") for item in citations]
            records.append(
                {
                    "run_sha256": run_sha256,
                    "question_id": row["id"],
                    "claim_id": claim["claim_id"],
                    "text": claim.get("text", ""),
                    "quotes": quotes,
                    "located_quotes": quotes,
                    "kept": True,
                    "issues": [],
                    "fact_uncovered": [],
                    "label": "unlabeled",
                    "annotator": "",
                    "labeled_at": None,
                    "note": "",
                }
            )
    return records


def _support_payload(labels: list[dict[str, Any]]) -> dict[str, Any]:
    summary = support_summary(labels)
    active = [item for item in labels if not item.get("excluded_from_support")]
    labeled = summary["drafts_labeled"]
    supported = summary["drafts_supported"]
    partial = sum(1 for item in active if item.get("label") == "partial")
    unsupported = sum(1 for item in active if item.get("label") == "unsupported")
    return {
        **summary,
        "supported_claims": supported,
        "partial_claims": partial,
        "unsupported_claims": unsupported,
        "claim_support_rate": summary["draft_support_rate"],
        "claim_unsupported_rate": {
            "successes": unsupported,
            "total": labeled,
            "rate": round(unsupported / labeled, 4) if labeled else None,
        },
    }


def _refusal_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = refusal_summary(rows)
    return {
        "refusal_recall": summary["refusal_recall"],
        "false_refusal": summary["false_refusal"],
        "reason_code_accuracy": summary["reason_code_accuracy"],
    }


def _render(report: dict[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    baseline = report["baseline"]
    a2 = report["a2"]
    lines = [
        "# W7 A2 Verifier 消融",
        "",
        f"- 数据集：`{report['dataset']}`（{report['dataset_sha256'][:12]}…）",
        f"- 冻结测试集 hash：`{report['testset_sha256'][:12]}…`",
        f"- baseline run：`{report['baseline_run_sha256'][:12]}…`",
        f"- A2 run：`{report['a2_run_sha256'][:12]}…`",
        "- 变体：pass-through verifier（不执行 W3 核验，结构门禁不适用）",
        "",
        "## 支持率",
        "",
        "| 变体 | supported/已标 | partial | unsupported | support rate | unsupported rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, payload in (("baseline（有 Verifier）", baseline), ("A2（无 Verifier）", a2)):
        support = payload["support"]
        lines.append(
            f"| {name} | {support['supported_claims']}/{support['drafts_labeled']} | "
            f"{support['partial_claims']} | {support['unsupported_claims']} | "
            f"{fmt(support['claim_support_rate']['rate'])} | "
            f"{fmt(support['claim_unsupported_rate']['rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 拒答与误拒",
            "",
            "| 变体 | 拒答召回 | 误拒率 | 原因码正确率 |",
            "|---|---:|---:|---:|",
        ]
    )
    for name, payload in (("baseline（有 Verifier）", baseline), ("A2（无 Verifier）", a2)):
        refusal = payload["refusal"]
        lines.append(
            f"| {name} | {fmt(refusal['refusal_recall']['rate'])} | "
            f"{fmt(refusal['false_refusal']['rate'])} | "
            f"{fmt(refusal['reason_code_accuracy']['rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- A2 仅用于评测，不进入生产；结构门禁（引用 ID/quote）不适用。",
            "- pass-through 只丢弃没有合法 citation 的 draft，不做 quote/fact/scope 核验。",
            f"- A2 结构性丢弃 claim：{report['a2']['structural_dropped_claims']} 条。",
            "- support 必须全量人工标注；当前 A2 若仍有 unlabeled，报告不声称最终通过。",
            "- A1 固定分块、A3 流程 DAG 为 N/A。",
            "",
        ]
    )
    return "\n".join(lines)


def _build_report(
    *,
    run: dict[str, Any],
    baseline_run: dict[str, Any],
    testset_sha256: str,
    dataset_path: Path,
    baseline_labels: list[dict[str, Any]],
    a2_labels: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline_support = _support_payload(baseline_labels)
    a2_support = _support_payload(a2_labels)
    a2_rows = run.get("rows") or []
    structural_dropped = sum(
        len([item for item in row.get("dropped_claim_ids") or []]) for row in a2_rows
    )
    return {
        "schema_version": "w7-a2-v1",
        "dataset": dataset_path.name,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "testset_sha256": testset_sha256,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "baseline_run_sha256": baseline_run.get("run_sha256"),
        "a2_run_sha256": run.get("run_sha256"),
        "baseline": {
            "support": baseline_support,
            "refusal": _refusal_payload(baseline_run.get("rows") or []),
        },
        "a2": {
            "support": a2_support,
            "refusal": _refusal_payload(a2_rows),
            "structural_dropped_claims": structural_dropped,
        },
        "metadata": {
            **git_info(ROOT),
            "index_version": (run.get("metadata") or {})
            .get("vector_manifest", {})
            .get("content_hash", ""),
            "model": (run.get("metadata") or {}).get("model", ""),
            "advisor_prompt_version": (run.get("metadata") or {}).get("advisor_prompt_version", ""),
        },
    }


def _write_report(
    *,
    run: dict[str, Any],
    baseline_run: dict[str, Any],
    testset_sha256: str,
    dataset_path: Path,
    labels_path: Path,
    baseline_labels_path: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, Any]:
    labels = load_jsonl(labels_path) if labels_path.exists() else []
    baseline_labels = load_jsonl(baseline_labels_path) if baseline_labels_path.exists() else []
    report = _build_report(
        run=run,
        baseline_run=baseline_run,
        testset_sha256=testset_sha256,
        dataset_path=dataset_path,
        baseline_labels=baseline_labels,
        a2_labels=labels,
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_md.write_text(_render(report), encoding="utf-8")
    return report


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--baseline-run", default=str(DEFAULT_BASELINE))
    parser.add_argument("--baseline-labels", default=str(DEFAULT_BASELINE_LABELS))
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--review", default=str(DEFAULT_REVIEW))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--labels-only", action="store_true")
    args = parser.parse_args()

    testset_path = Path(args.testset)
    items, frozen_manifest = verify_frozen(
        testset_path=testset_path,
        manifest_path=Path(args.manifest),
    )
    baseline_run = load_run(args.baseline_run)
    run_path = Path(args.run)
    labels_path = Path(args.labels)
    if args.labels_only:
        run = load_run(run_path)
        template = _labels_from_run(run)
        write_labels(template, labels_path)
        questions = {str(row["id"]): str(row.get("question", "")) for row in run["rows"]}
        Path(args.review).write_text(
            render_label_sheet(
                template,
                questions,
                title="W7 A2 pass-through support 核对表（全量）",
                allow_excluded=True,
            ),
            encoding="utf-8",
        )
        print(f"labels: {labels_path}（{len(template)} 条，待人工）")
        print(f"review: {args.review}")
        return 0
    if args.report_only:
        run = load_run(run_path)
    else:
        rows, metadata = _run_a2(items)
        run = build_run_snapshot(
            dataset=testset_path.name,
            dataset_path=testset_path,
            rows=rows,
            metadata=metadata,
        )
        run["frozen"] = True
        run["testset_sha256"] = frozen_manifest["sha256"]
        run = finalize_run(run)
        save_run(run, run_path)
        print(f"run: {run_path} sha256={run['run_sha256']}", flush=True)

        template = _labels_from_run(run)
        write_labels(template, labels_path)
        questions = {str(row["id"]): str(row.get("question", "")) for row in run["rows"]}
        Path(args.review).write_text(
            render_label_sheet(
                template,
                questions,
                title="W7 A2 pass-through support 核对表（全量）",
                allow_excluded=True,
            ),
            encoding="utf-8",
        )
        print(f"labels: {labels_path}（{len(template)} 条，待人工）", flush=True)
        print(f"review: {args.review}", flush=True)

    report = _write_report(
        run=run,
        baseline_run=baseline_run,
        testset_sha256=frozen_manifest["sha256"],
        dataset_path=testset_path,
        labels_path=labels_path,
        baseline_labels_path=Path(args.baseline_labels),
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
    )
    print(f"report: {args.output_json}")
    print(f"report: {args.output_md}")
    print(
        f"A2 support labeled={report['a2']['support']['drafts_labeled']}/"
        f"{report['a2']['support']['drafts_total']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
