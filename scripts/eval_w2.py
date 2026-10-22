"""20 题开发集 v0 runner。

只用于调参与回归，不是冻结测试集；W2 不报告答案正确率。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campusguide.domain.enums import BlockType
from campusguide.domain.text import normalize_text
from campusguide.env import load_dotenv
from campusguide.qa.runtime import create_qa_service, create_qdrant_client
from campusguide.qa.service import QAResult

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
DEFAULT_JSON = ROOT / "data" / "eval" / "w2-dev-report.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w2-dev-report.md"


def load_dev(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
    return rows


def percentile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _quote_locatable(result: QAResult) -> tuple[int, int]:
    """严格按 (article_id, part_no) 定位，不回退到同 article 的其他 part。"""
    evidence_by_key = {
        (item.article.article_id, item.article.part_no): item for item in result.evidence
    }
    located = 0
    for citation in result.citations:
        item = evidence_by_key.get((citation.article_id, citation.part_no))
        if item is None:
            continue
        if normalize_text(citation.quote) in item.article.text_normalized:
            located += 1
    return located, len(result.citations)


def evaluate_one(
    item: dict[str, Any],
    result: QAResult,
) -> dict[str, Any]:
    expected_ids = list(item.get("expected_article_ids") or [])
    block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
    evidence_ids = [evidence.article.article_id for evidence in result.evidence]
    evidence_keys = {
        (evidence.article.article_id, evidence.article.part_no) for evidence in result.evidence
    }
    top8 = evidence_ids[:8]
    expected_set = set(expected_ids)
    hits = expected_set & set(top8)
    recall = (len(hits) / len(expected_set)) if expected_set else None

    valid_citations = [
        citation
        for citation in result.citations
        if (citation.article_id, citation.part_no) in evidence_keys
    ]
    fabricated = len(
        [citation for citation in result.citations if citation.article_id not in set(evidence_ids)]
    )
    wrong_part = len(result.citations) - len(valid_citations) - fabricated
    located, total_citations = _quote_locatable(result)

    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "expected_outcome": item["expected_outcome"],
        "outcome": result.outcome.value,
        "outcome_correct": result.outcome.value == item["expected_outcome"],
        "expected_article_ids": expected_ids,
        "retrieved_article_ids": evidence_ids,
        "recall_at_8": recall,
        "block_types": [block_type.value for block_type in block_types],
        "claims": len(result.claims),
        "citations": len(result.citations),
        "valid_citations": len(valid_citations),
        "fabricated_id_count": fabricated,
        "wrong_part_count": wrong_part,
        "quotes_located": located,
        "quotes_total": total_citations,
        "dropped_claims": result.dropped_claims,
        "dropped_citations": result.dropped_citations,
        "duration_ms": result.duration_ms,
        "trace_id": result.trace_id,
        "llm_model": result.llm_model,
        "message": result.message if not result.answered else "",
    }


def build_report(
    rows: list[dict[str, Any]],
    *,
    dataset: str,
    warmup_ms: int = 0,
) -> dict[str, Any]:
    answered_rows = [row for row in rows if row["expected_outcome"] == "ANSWERED"]
    article_rows = [
        row
        for row in answered_rows
        if "article" in row["block_types"] and row["recall_at_8"] is not None
    ]
    section_rows = [
        row
        for row in answered_rows
        if "section" in row["block_types"] and row["recall_at_8"] is not None
    ]
    non_answerable = [row for row in rows if row["expected_outcome"] != "ANSWERED"]

    total_citations = sum(row["citations"] for row in rows)
    valid_citations = sum(row["valid_citations"] for row in rows)
    fabricated = sum(row["fabricated_id_count"] for row in rows)
    quotes_total = sum(row["quotes_total"] for row in rows)
    quotes_located = sum(row["quotes_located"] for row in rows)
    wrong_parts = sum(row["wrong_part_count"] for row in rows)
    durations = [row["duration_ms"] for row in rows]

    def mean_recall(subset: list[dict[str, Any]]) -> float | None:
        if not subset:
            return None
        values = [row["recall_at_8"] for row in subset if row["recall_at_8"] is not None]
        return statistics.fmean(values) if values else None

    summary = {
        "questions": len(rows),
        "answered": sum(1 for row in rows if row["outcome"] == "ANSWERED"),
        "outcome_correct": sum(1 for row in rows if row["outcome_correct"]),
        "outcome_accuracy": (
            sum(1 for row in rows if row["outcome_correct"]) / len(rows) if rows else 0.0
        ),
        "non_answerable_accuracy": (
            sum(1 for row in non_answerable if row["outcome_correct"]) / len(non_answerable)
            if non_answerable
            else None
        ),
        "citation_id_valid_rate": (valid_citations / total_citations) if total_citations else None,
        "fabricated_id_count": fabricated,
        "wrong_part_count": wrong_parts,
        "quote_locatable_rate": (quotes_located / quotes_total) if quotes_total else None,
        "article_recall_at_8": mean_recall(article_rows),
        "section_recall_at_8": mean_recall(section_rows),
        "p50_latency_ms": percentile(durations, 0.50),
        "p95_latency_ms": percentile(durations, 0.95),
        "warmup_ms": warmup_ms,
        "latency_scope": "warm",
        "llm_models": sorted({row["llm_model"] for row in rows if row["llm_model"]}),
    }
    summary["latency_gate_ok"] = (
        summary["p50_latency_ms"] <= 6000 and summary["p95_latency_ms"] <= 10000
    )

    has_answerable = any(row["expected_outcome"] == "ANSWERED" for row in rows)
    hard_gate_ok = (
        fabricated == 0
        and (not has_answerable or (total_citations > 0 and valid_citations == total_citations))
        and (not has_answerable or (quotes_total > 0 and quotes_located == quotes_total))
    )
    return {
        "dataset": dataset,
        "frozen": False,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "W2 开发集 v0，仅用于调参，不是冻结测试集；不报告答案正确率。",
        "summary": summary,
        "hard_gate_ok": hard_gate_ok,
        "latency_gate_ok": summary["latency_gate_ok"],
        "w2_gate_ok": hard_gate_ok and summary["latency_gate_ok"],
        "latency_scope": "warm",
        "warmup_ms": warmup_ms,
        "rows": rows,
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# CampusGuide W2 开发集报告",
        "",
        f"- 数据集：`{report['dataset']}`（开发集 v0，**非冻结测试集**）",
        f"- 生成时间：{report['generated_at']}",
        f"- 模型：{', '.join(summary['llm_models']) or 'n/a'}",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 题数 | {summary['questions']} |",
        f"| 可答题数 | {summary['answered']} |",
        f"| outcome 正确率 | {summary['outcome_accuracy']:.1%} |",
        (
            f"| 非可答题原因码正确率 | {summary['non_answerable_accuracy']:.1%} |"
            if summary["non_answerable_accuracy"] is not None
            else "| 非可答题原因码正确率 | n/a |"
        ),
        (
            f"| 引用 ID 有效率 | {summary['citation_id_valid_rate']:.1%} |"
            if summary["citation_id_valid_rate"] is not None
            else "| 引用 ID 有效率 | n/a |"
        ),
        f"| 编造 ID 数 | {summary['fabricated_id_count']} |",
        f"| part_no 错误数 | {summary['wrong_part_count']} |",
        (
            f"| quote 可定位率 | {summary['quote_locatable_rate']:.1%} |"
            if summary["quote_locatable_rate"] is not None
            else "| quote 可定位率 | n/a |"
        ),
        (
            f"| article_recall@8 | {summary['article_recall_at_8']:.1%} |"
            if summary["article_recall_at_8"] is not None
            else "| article_recall@8 | n/a |"
        ),
        (
            f"| section_recall@8 | {summary['section_recall_at_8']:.1%} |"
            if summary["section_recall_at_8"] is not None
            else "| section_recall@8 | n/a |"
        ),
        f"| 冷启动预热（bge-m3） | {summary['warmup_ms']:.0f} ms |",
        f"| p50 延迟（warm） | {summary['p50_latency_ms']:.0f} ms |",
        f"| p95 延迟（warm） | {summary['p95_latency_ms']:.0f} ms |",
        f"| 延迟门禁（p50 ≤6s、p95 ≤10s） | {'通过' if report['latency_gate_ok'] else '未通过'} |",
        f"| 引用硬门禁 | {'通过' if report['hard_gate_ok'] else '未通过'} |",
        f"| W2 总门禁 | {'通过' if report['w2_gate_ok'] else '未通过'} |",
        "",
        "## 逐题结果",
        "",
        "| ID | 类别 | outcome | 期望 | 正确 | Recall@8 | 引用 | 编造 | 延迟(ms) |",
        "|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        recall = "—" if row["recall_at_8"] is None else f"{row['recall_at_8']:.0%}"
        lines.append(
            f"| {row['id']} | {row['category']} | {row['outcome']} | "
            f"{row['expected_outcome']} | {'是' if row['outcome_correct'] else '否'} | "
            f"{recall} | {row['citations']} | {row['fabricated_id_count']} | "
            f"{row['duration_ms']} |"
        )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- 本报告不评价答案正确性；W2 只证明引用 ID 与 quote 可定位。",
            "- 阈值 `min_dense_score` 仍需用本开发集继续校准。",
            "- `section` 结果单独记录，不计入条款指标。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", default=str(DEFAULT_DEV))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    items = load_dev(Path(args.dev))
    if args.limit is not None:
        items = items[: args.limit]

    try:
        client = create_qdrant_client()
        service = create_qa_service(client=client)
    except Exception as exc:
        print(
            f"error: 开发集未运行（{type(exc).__name__}）：{exc}\n"
            "请检查 .env、Qdrant 与 data/vector_manifest.json。",
            file=sys.stderr,
        )
        return 2
    warmup_started = time.perf_counter()
    service.warmup()
    warmup_ms = int((time.perf_counter() - warmup_started) * 1000)
    print(f"warmup: {warmup_ms}ms (p50/p95 均为 warm 状态)", flush=True)
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
        result = service.ask(item["question"], block_types=block_types)
        row = evaluate_one(item, result)
        rows.append(row)
        print(
            f"[{index}/{len(items)}] {item['id']} {result.outcome.value} {row['duration_ms']}ms",
            flush=True,
        )

    report = build_report(rows, dataset=Path(args.dev).name, warmup_ms=warmup_ms)
    json_path = Path(args.output_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path = Path(args.output_md)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    summary = report["summary"]
    print(
        "summary: "
        f"outcome_acc={summary['outcome_accuracy']:.1%} "
        f"cite_valid={summary['citation_id_valid_rate']} "
        f"fabricated={summary['fabricated_id_count']} "
        f"quote_locatable={summary['quote_locatable_rate']} "
        f"article_recall@8={summary['article_recall_at_8']} "
        f"p50={summary['p50_latency_ms']:.0f}ms p95={summary['p95_latency_ms']:.0f}ms "
        f"warmup={summary['warmup_ms']}ms "
        f"latency_gate={'PASS' if summary['latency_gate_ok'] else 'FAIL'}"
    )
    print(f"report: {json_path}")
    print(f"report: {md_path}")
    return 0 if report["w2_gate_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
