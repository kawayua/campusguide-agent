"""W7 A4 profile 过滤消融。

同一 50 题冻结集，关闭 profile 的学院/专业/入学年份等元数据过滤与查询
扩展（保留 audience 硬过滤），比较人群错配率与拒答/误拒。A4 只用于评测，
不改变生产默认行为。
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
from campusguide.domain.models import Article
from campusguide.domain.profiles import UserProfile
from campusguide.env import load_dotenv
from campusguide.eval.w3 import (
    build_run_snapshot,
    finalize_run,
    git_info,
    hash_configs,
    load_run,
    row_from_result,
    save_run,
    vector_manifest_info,
)
from campusguide.eval.w7 import load_jsonl, refusal_summary, verify_frozen
from campusguide.qa.runtime import RuntimePaths, create_mcp_qa_service
from campusguide.retrieval.filters import article_matches_profile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTSET = ROOT / "data" / "eval" / "test_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "eval" / "test_v1.manifest.json"
DEFAULT_BASELINE = ROOT / "data" / "eval" / "w7_remediation2_run.json"
DEFAULT_RUN = ROOT / "data" / "eval" / "w7_a4_profile_run.json"
DEFAULT_JSON = ROOT / "data" / "eval" / "w7-a4-profile.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w7-a4-profile.md"
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


def _load_articles(path: Path) -> dict[str, Article]:
    articles: dict[str, Article] = {}
    for item in load_jsonl(path):
        article = Article.model_validate(item)
        articles[article.article_id] = article
    return articles


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
        "a4_no_profile_metadata": True,
    }


def _run_a4(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    service = create_mcp_qa_service()
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
                use_profile_metadata=False,
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
                f"[{index}/{len(items)}] {item['qid']} {row['outcome']} qa={row.get('qa_ms')}ms",
                flush=True,
            )
    finally:
        service.close()
    metadata = _metadata(rows, warmup_ms=warmup_ms, text_manifest=RuntimePaths().text_manifest)
    return rows, metadata


def _mismatch(
    row: dict[str, Any],
    item: dict[str, Any],
    articles: dict[str, Article],
) -> dict[str, Any] | None:
    profile = _profile(item)
    if profile.is_empty:
        return None
    as_of = date.fromisoformat(item["as_of"])
    retrieved = [entry.get("article_id") for entry in row.get("evidence") or []]
    cited = [entry.get("article_id") for entry in row.get("citations") or []]
    retrieved_hits: list[dict[str, Any]] = []
    cited_hits: list[dict[str, Any]] = []
    for article_id in retrieved:
        article = articles.get(str(article_id))
        if article is None:
            continue
        ok, reasons = article_matches_profile(article, profile, as_of=as_of)
        if not ok:
            retrieved_hits.append({"article_id": article_id, "reasons": reasons})
    for article_id in cited:
        article = articles.get(str(article_id))
        if article is None:
            continue
        ok, reasons = article_matches_profile(article, profile, as_of=as_of)
        if not ok:
            cited_hits.append({"article_id": article_id, "reasons": reasons})
    return {
        "qid": item["qid"],
        "profile": item.get("user_profile") or {},
        "retrieved_total": len(retrieved),
        "retrieved_mismatched": len(retrieved_hits),
        "cited_total": len(cited),
        "cited_mismatched": len(cited_hits),
        "retrieved_hits": retrieved_hits,
        "cited_hits": cited_hits,
    }


def _summarize_mismatch(
    rows: list[dict[str, Any]],
    items_by_qid: dict[str, dict[str, Any]],
    articles: dict[str, Article],
) -> dict[str, Any]:
    details = []
    for row in rows:
        item = items_by_qid.get(str(row.get("qid")))
        if item is None:
            continue
        result = _mismatch(row, item, articles)
        if result is not None:
            details.append(result)
    return {
        "profile_questions": len(details),
        "questions_with_retrieved_mismatch": sum(
            1 for item in details if item["retrieved_mismatched"]
        ),
        "retrieved_evidence": sum(item["retrieved_total"] for item in details),
        "retrieved_mismatched": sum(item["retrieved_mismatched"] for item in details),
        "questions_with_cited_mismatch": sum(1 for item in details if item["cited_mismatched"]),
        "cited_evidence": sum(item["cited_total"] for item in details),
        "cited_mismatched": sum(item["cited_mismatched"] for item in details),
        "details": details,
    }


def _refusal(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
    a4 = report["a4"]
    lines = [
        "# W7 A4 profile 过滤消融",
        "",
        f"- 数据集：`{report['dataset']}`（{report['dataset_sha256'][:12]}…）",
        f"- 冻结测试集 hash：`{report['testset_sha256'][:12]}…`",
        f"- profile 题：{report['profile_questions']} 道",
        f"- baseline run：`{report['baseline_run_sha256'][:12]}…`",
        f"- A4 run：`{report['a4_run_sha256'][:12]}…`",
        "",
        "## 人群错配",
        "",
        "| 变体 | 有 retrieved 错配的题 | retrieved 错配/总数 | 有 cited 错配的题 | cited 错配/总数 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, payload in (("baseline（有 profile）", baseline), ("A4（无 profile 元数据）", a4)):
        mismatch = payload["mismatch"]
        lines.append(
            f"| {name} | {mismatch['questions_with_retrieved_mismatch']}/"
            f"{mismatch['profile_questions']} | "
            f"{mismatch['retrieved_mismatched']}/{mismatch['retrieved_evidence']} | "
            f"{mismatch['questions_with_cited_mismatch']}/"
            f"{mismatch['profile_questions']} | "
            f"{mismatch['cited_mismatched']}/{mismatch['cited_evidence']} |"
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
    for name, payload in (("baseline（有 profile）", baseline), ("A4（无 profile 元数据）", a4)):
        refusal = payload["refusal"]
        lines.append(
            f"| {name} | {fmt(refusal['refusal_recall']['rate'])} | "
            f"{fmt(refusal['false_refusal']['rate'])} | "
            f"{fmt(refusal['reason_code_accuracy']['rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 逐题错配",
            "",
            "| 题目 | 变体 | retrieved 错配 | cited 错配 |",
            "|---|---|---:|---:|",
        ]
    )
    for name, payload in (("baseline", baseline), ("a4", a4)):
        for item in payload["mismatch"]["details"]:
            if item["retrieved_mismatched"] or item["cited_mismatched"]:
                lines.append(
                    f"| {item['qid']} | {name} | {item['retrieved_mismatched']} | "
                    f"{item['cited_mismatched']} |"
                )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- A4 关闭 profile 的学院/专业/入学年份等元数据过滤与查询扩展，"
            "保留 audience 硬过滤和版本意图 guard。",
            "- 错配判定复用 `article_matches_profile`：只在文档有明确约束、"
            "且用户已知值与约束冲突时计为错配。",
            "- A4 只比较错配与拒答，不使用 answer correctness/support 标签；"
            "关闭 profile 后的答案变化不由本消融评价。",
            "- W7 冻结集只有 5 道 profile 题，A4 比例只作方向性证据。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--baseline-run", default=str(DEFAULT_BASELINE))
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    testset_path = Path(args.testset)
    items, frozen_manifest = verify_frozen(
        testset_path=testset_path,
        manifest_path=Path(args.manifest),
    )
    items_by_qid = {str(item["qid"]): item for item in items}
    articles = _load_articles(ROOT / "data" / "processed" / "articles.jsonl")
    baseline_run = load_run(args.baseline_run)

    if args.report_only:
        run = load_run(args.run)
        metadata = run.get("metadata") or {}
    else:
        rows, metadata = _run_a4(items)
        run = build_run_snapshot(
            dataset=testset_path.name,
            dataset_path=testset_path,
            rows=rows,
            metadata=metadata,
        )
        run["frozen"] = True
        run["testset_sha256"] = frozen_manifest["sha256"]
        run = finalize_run(run)
        save_run(run, Path(args.run))
        print(f"run: {args.run} sha256={run['run_sha256']}", flush=True)

    baseline_mismatch = _summarize_mismatch(baseline_run.get("rows") or [], items_by_qid, articles)
    a4_mismatch = _summarize_mismatch(run.get("rows") or [], items_by_qid, articles)
    baseline_refusal = _refusal(baseline_run.get("rows") or [])
    a4_refusal = _refusal(run.get("rows") or [])

    report = {
        "schema_version": "w7-a4-v1",
        "dataset": testset_path.name,
        "dataset_sha256": hashlib.sha256(testset_path.read_bytes()).hexdigest(),
        "testset_sha256": frozen_manifest["sha256"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile_questions": baseline_mismatch["profile_questions"],
        "baseline_run_sha256": baseline_run.get("run_sha256"),
        "a4_run_sha256": run.get("run_sha256"),
        "baseline": {"mismatch": baseline_mismatch, "refusal": baseline_refusal},
        "a4": {"mismatch": a4_mismatch, "refusal": a4_refusal},
        "metadata": {
            **git_info(ROOT),
            "index_version": metadata["vector_manifest"].get("content_hash", ""),
            "config_hash": metadata["config_hash"],
            "model": metadata["model"],
            "advisor_prompt_version": metadata["advisor_prompt_version"],
        },
    }
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render(report), encoding="utf-8")
    print(f"report: {json_path}")
    print(f"report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
