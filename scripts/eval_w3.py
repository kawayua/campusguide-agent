"""W3 20 题开发集 runner：run 快照 + 人工支持标注 + 拒答混淆矩阵。

流程（设计 10.2/11.3）：
1. 逐题调用 QAService，保存冻结 run 快照 data/eval/w3_run.json；
2. 读取/生成 data/eval/w3_support_labels.jsonl 人工标注（--write-labels 生成脚手架）；
3. 输出 data/eval/w3-dev-report.{json,md}。

p95 沿用 W2 已知限制，W3 不新增 LLM 调用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from campusguide.domain.enums import BlockType
from campusguide.domain.text import normalize_text
from campusguide.env import load_dotenv
from campusguide.eval.w3 import build_report, default_metadata, load_support_labels, render_markdown
from campusguide.qa.runtime import create_qa_service, create_qdrant_client
from campusguide.qa.service import QAResult

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
DEFAULT_RUN = ROOT / "data" / "eval" / "w3_run.json"
DEFAULT_LABELS = ROOT / "data" / "eval" / "w3_support_labels.jsonl"
DEFAULT_JSON = ROOT / "data" / "eval" / "w3-dev-report.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w3-dev-report.md"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return ""


def _quote_span_ok(result: QAResult, citation) -> bool:
    evidence_by_key = {
        (item.article.article_id, item.article.part_no): item for item in result.evidence
    }
    item = evidence_by_key.get((citation.article_id, citation.part_no))
    if item is None:
        return False
    span = item.article.text[citation.quote_start : citation.quote_end]
    return normalize_text(span) == normalize_text(citation.quote)


def build_row(item: dict[str, Any], result: QAResult) -> dict[str, Any]:
    evidence_keys = {
        (evidence.article.article_id, evidence.article.part_no) for evidence in result.evidence
    }
    evidence_ids = [evidence.article.article_id for evidence in result.evidence]
    expected_ids = list(item.get("expected_article_ids") or [])
    hits = set(expected_ids) & set(evidence_ids[:8])
    claims = [
        {
            "claim_id": claim.get("claim_id", ""),
            "text": claim.get("text", ""),
            "citations": claim.get("citations", []),
            "rendered": False,
            "label": "unlabeled",
        }
        for claim in result.draft_claims
    ]
    # rendered claim_id 由 Verifier 分配，与 draft 可能不同；按归一化文本逐条消耗对齐
    rendered_remaining = Counter(normalize_text(claim.text) for claim in result.claims)
    for claim in claims:
        text_key = normalize_text(claim["text"])
        if rendered_remaining[text_key] > 0:
            claim["rendered"] = True
            rendered_remaining[text_key] -= 1
    citations = [
        {
            "article_id": citation.article_id,
            "part_no": citation.part_no,
            "quote": citation.quote,
            "quote_start": citation.quote_start,
            "quote_end": citation.quote_end,
            "quote_match": citation.quote_match,
            "span_ok": _quote_span_ok(result, citation),
        }
        for citation in result.citations
    ]
    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "expected_outcome": item["expected_outcome"],
        "outcome": result.outcome.value,
        "partial": result.partial,
        "recall_at_8": (len(hits) / len(set(expected_ids))) if expected_ids else None,
        "claims": claims,
        "citations": citations,
        "issues": [issue.model_dump(mode="json") for issue in result.issues],
        "citations_total": len(citations),
        "valid_citations": sum(
            1
            for citation in citations
            if (citation["article_id"], citation["part_no"]) in evidence_keys
        ),
        "fabricated_id_count": sum(
            1 for citation in citations if citation["article_id"] not in set(evidence_ids)
        ),
        "quotes_total": len(citations),
        "quotes_located": sum(1 for citation in citations if citation["span_ok"]),
        "duration_ms": result.duration_ms,
        "probe_ms": result.probe_ms,
        "trace_id": result.trace_id,
    }


def write_label_scaffold(rows: list[dict[str, Any]], path: Path, run_path: Path) -> int:
    """为每个 draft claim 生成一条 unlabeled 记录，供人工改标。

    设计 10.2：记录标注人、日期与 run hash；claim 文本重复时按出现顺序区分。
    """
    run_hash = _sha256(run_path)
    today = date.today().isoformat()
    written = 0
    lines: list[str] = []
    counters: Counter[tuple[str, str]] = Counter()
    for row in rows:
        for claim in row["claims"]:
            key = (row["id"], normalize_text(claim["text"]))
            occurrence = counters[key]
            counters[key] += 1
            lines.append(
                json.dumps(
                    {
                        "question_id": row["id"],
                        "claim_text_normalized": claim["text"],
                        "occurrence": occurrence,
                        "label": "unlabeled",
                        "note": "",
                        "annotator": "",
                        "labeled_at": today,
                        "run_sha256": run_hash,
                    },
                    ensure_ascii=False,
                )
            )
            written += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return written


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", default=str(DEFAULT_DEV))
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--write-labels", action="store_true", help="生成标注脚手架后退出")
    parser.add_argument(
        "--reuse-run",
        action="store_true",
        help="不重新调用模型，复用已有 run 快照（用于标注/报告重算）",
    )
    args = parser.parse_args()

    items = load_dev(Path(args.dev))
    if args.limit is not None:
        items = items[: args.limit]

    run_path = Path(args.run)
    if args.reuse_run:
        if not run_path.exists():
            print(f"error: run 快照不存在：{run_path}", file=sys.stderr)
            return 2
        run_snapshot = json.loads(run_path.read_text(encoding="utf-8"))
        rows = run_snapshot["rows"]
        print(f"reuse run: {run_path} ({len(rows)} rows)")
    else:
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
        service.warmup()

        rows = []
        models: set[str] = set()
        for index, item in enumerate(items, start=1):
            block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
            result = service.ask(item["question"], block_types=block_types)
            if result.llm_model:
                models.add(result.llm_model)
            rows.append(build_row(item, result))
            print(
                f"[{index}/{len(items)}] {item['id']} {result.outcome.value} "
                f"partial={result.partial} {result.duration_ms}ms",
                flush=True,
            )

        run_snapshot = {
            **default_metadata(
                dataset=Path(args.dev).name,
                git_commit=_git_commit(),
                advisor_model="; ".join(sorted(models)),
                vector_manifest_sha256=_sha256(ROOT / "data" / "vector_manifest.json"),
                config_hash=_sha256(ROOT / "configs" / "sources.yaml"),
            ),
            "rows": rows,
        }
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(
            json.dumps(run_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"run snapshot: {run_path}")

    if args.write_labels:
        count = write_label_scaffold(rows, Path(args.labels), run_path)
        print(f"label scaffold: {Path(args.labels)} ({count} claims)")
        return 0

    labels = load_support_labels(Path(args.labels))
    if labels:
        print(f"labels: {len(labels)} loaded")
        expected_hash = labels[0].get("run_sha256")
        actual_hash = _sha256(run_path)
        if expected_hash and expected_hash != actual_hash:
            print(
                "warning: 标注文件的 run_sha256 与当前 run 快照不一致，"
                "标注可能来自其他运行，支持率仅供参照。",
                file=sys.stderr,
            )
    else:
        print(
            "warning: 未找到人工 support 标注，支持率将记为 n/a；"
            "可先运行 --write-labels 生成脚手架。",
            file=sys.stderr,
        )

    report = build_report(
        rows,
        labels=labels,
        metadata={
            key: run_snapshot.get(key)
            for key in (
                "dataset",
                "generated_at",
                "git_commit",
                "advisor_model",
                "vector_manifest_sha256",
                "config_hash",
                "warm",
            )
        },
    )
    json_path = Path(args.output_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path = Path(args.output_md)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    summary = report["summary"]
    print(
        "summary: "
        f"struct_gate={'PASS' if report['struct_gate_ok'] else 'FAIL'} "
        f"abstention_recall={summary['abstention_recall']} "
        f"over_refusal={summary['over_refusal_rate']} "
        f"partial={summary['partial_rate']} "
        f"rendered_support={summary['rendered_support']['support_rate']} "
        f"p50={summary['p50_latency_ms']:.0f}ms p95={summary['p95_latency_ms']:.0f}ms"
    )
    print(f"report: {json_path}")
    print(f"report: {md_path}")
    return 0 if report["w3_gate_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
