"""W4 检索评测：dense-only / hybrid RRF / hybrid+rerank。

只评估检索层（Recall@8、MRR），不调用 LLM；答案支持率仍由 W3/W6-A
的 run 与人工标注决定。article 与 section 指标分开统计。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campusguide.eval.w3 import git_info, percentile

SCHEMA_VERSION = "w4-retrieval-v1"


def recall_at_k(retrieved: list[str], expected: list[str], *, k: int = 8) -> float | None:
    if not expected:
        return None
    top = set(retrieved[:k])
    return len(top & set(expected)) / len(set(expected))


def reciprocal_rank(retrieved: list[str], expected: list[str]) -> float | None:
    if not expected:
        return None
    expected_set = set(expected)
    for rank, article_id in enumerate(retrieved, start=1):
        if article_id in expected_set:
            return 1.0 / rank
    return 0.0


def _group(row: dict[str, Any]) -> str:
    if row.get("category") == "section" or "section" in (row.get("block_types") or []):
        return "section"
    return "article"


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row.get("recall_at_8") is not None]
    groups = {"article": [], "section": []}
    for row in scored:
        groups[_group(row)].append(row)

    summary: dict[str, Any] = {
        "questions": len(rows),
        "scored_questions": len(scored),
        "skipped_no_expected": len(rows) - len(scored),
    }
    for name, items in groups.items():
        summary[name] = {
            "questions": len(items),
            "recall_at_8": _mean([item["recall_at_8"] for item in items]),
            "mrr": _mean([item["reciprocal_rank"] for item in items]),
            "no_hit": sum(1 for item in items if item["reciprocal_rank"] == 0.0),
        }
    summary["overall"] = {
        "recall_at_8": _mean([item["recall_at_8"] for item in scored]),
        "mrr": _mean([item["reciprocal_rank"] for item in scored]),
    }
    latencies = [int(row.get("latency_ms", 0) or 0) for row in rows]
    rerank_latencies = [
        int((row.get("timing") or {}).get("rerank_ms", 0))
        for row in rows
        if int((row.get("timing") or {}).get("rerank_ms", 0)) > 0
    ]
    summary["latency_ms"] = {
        "p50": percentile(latencies, 0.50),
        "p95": percentile(latencies, 0.95),
    }
    summary["rerank_latency_ms"] = {
        "count": len(rerank_latencies),
        "p50": percentile(rerank_latencies, 0.50) if rerank_latencies else None,
        "p95": percentile(rerank_latencies, 0.95) if rerank_latencies else None,
    }
    return summary


def build_report(
    *,
    dataset: str,
    dataset_path: Path,
    modes: dict[str, dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metadata": metadata,
        "modes": modes,
    }


def render_markdown(report: dict[str, Any]) -> str:
    metadata = report.get("metadata") or {}
    lines = [
        "# W4 检索评测报告",
        "",
        f"- 数据集：`{report['dataset']}`（{report['dataset_sha256'][:12]}…）",
        f"- 生成时间：{report['generated_at']}",
        f"- git：`{metadata.get('git_commit', 'n/a')}`（dirty={metadata.get('git_dirty')}）",
        f"- 索引版本：`{metadata.get('index_version', 'n/a')}`",
        f"- 向量 manifest：`{metadata.get('vector_manifest_sha256', 'n/a')}`",
        "",
        "## 指标（Recall@8 / MRR）",
        "",
        "| 模式 | 说明 | article Recall@8 | article MRR | section Recall@8 | section MRR | overall Recall@8 | overall MRR |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "dense": "dense-only（W3 基线）",
        "hybrid_rrf": "dense+sparse RRF",
        "hybrid_rerank": "RRF + bge-reranker-v2-m3",
    }
    for mode, payload in report["modes"].items():
        summary = payload["summary"]
        article = summary.get("article") or {}
        section = summary.get("section") or {}
        overall = summary.get("overall") or {}

        def fmt(value):
            return "n/a" if value is None else f"{value:.3f}"

        lines.append(
            f"| `{mode}` | {labels.get(mode, mode)} | "
            f"{fmt(article.get('recall_at_8'))} | {fmt(article.get('mrr'))} | "
            f"{fmt(section.get('recall_at_8'))} | {fmt(section.get('mrr'))} | "
            f"{fmt(overall.get('recall_at_8'))} | {fmt(overall.get('mrr'))} |"
        )
    lines.extend(
        [
            "",
            "## 延迟",
            "",
            "| 模式 | p50 | p95 | rerank p50 | rerank p95 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for mode, payload in report["modes"].items():
        summary = payload["summary"]
        latency = summary["latency_ms"]
        rerank = summary["rerank_latency_ms"]
        rerank_p50 = "n/a" if rerank["p50"] is None else f"{rerank['p50']:.1f} ms"
        rerank_p95 = "n/a" if rerank["p95"] is None else f"{rerank['p95']:.1f} ms"
        lines.append(
            f"| `{mode}` | {latency['p50']:.1f} ms | {latency['p95']:.1f} ms | "
            f"{rerank_p50} | {rerank_p95} |"
        )
    lines.extend(
        [
            "",
            "## 口径与限制",
            "",
            "- 只评估检索层，不调用 LLM；Recall@8/MRR 基于 dev_v0 的 `expected_article_ids`。",
            "- 非可答题（无 expected ids）不计入检索指标。",
            "- dev_v0 不是冻结测试集；W7 才报冻结区间。",
            "- W4 不宣称答案质量提升，除非有 freeze 的评测数据。",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], *, json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")


def metadata(
    *,
    root: Path,
    index_version: str,
    vector_manifest_path: Path,
    config_hash: str,
) -> dict[str, Any]:
    return {
        **git_info(root),
        "index_version": index_version,
        "vector_manifest_sha256": hashlib.sha256(vector_manifest_path.read_bytes()).hexdigest(),
        "config_hash": config_hash,
    }
