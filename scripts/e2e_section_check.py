"""P1 section 端到端验收：培养方案宽问题/聚焦问题/版本意图。

固定 4 个 case，通过完整 MCP QAService + section 块跑问答：
- p004-broad / p004-focused / p005-2024 期望 ANSWERED；
- p005-old 期望 VERSION_MISMATCH（入学年份与问题版本不一致）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from campusguide.domain.enums import Audience, BlockType, OutcomeCode
from campusguide.domain.profiles import UserProfile
from campusguide.qa.runtime import create_mcp_qa_service

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "eval" / "p1-e2e.json"


def _cases():
    return [
        (
            "p004-broad",
            "农学专业培养方案",
            UserProfile(colleges=["agri"], majors=["seed-science"], intake_year=2024),
            OutcomeCode.ANSWERED,
        ),
        (
            "p004-focused",
            "种子科学与工程专业 2024 版培养方案的培养目标是什么？",
            UserProfile(colleges=["agri"], majors=["seed-science"], intake_year=2024),
            OutcomeCode.ANSWERED,
        ),
        (
            "p005-old",
            "农学专业 2022 版培养方案",
            UserProfile(colleges=["agri"], majors=["agronomy"], intake_year=2024),
            OutcomeCode.VERSION_MISMATCH,
        ),
        (
            "p005-2024",
            "农学专业 2024 版培养方案",
            UserProfile(colleges=["agri"], majors=["agronomy"], intake_year=2024),
            OutcomeCode.ANSWERED,
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    output_path = Path(args.output)
    service = create_mcp_qa_service()
    rows = []
    failed = False
    try:
        for case_id, question, profile, expected in _cases():
            result = service.ask(
                question,
                audience=Audience.UNDERGRADUATE,
                profile=profile,
                block_types=[BlockType.SECTION],
            )
            row = {
                "id": case_id,
                "question": question,
                "expected_outcome": expected.value,
                "outcome": result.outcome.value,
                "decision": result.decision.value if result.decision else None,
                "partial": result.partial,
                "message": result.message,
                "answer_markdown": result.answer_markdown,
                "quote_repair": result.quote_repair,
                "evidence": [
                    {
                        "doc_id": item.article.doc_id,
                        "article_no": item.article.article_no,
                        "block_type": item.article.block_type.value,
                        "rank": item.rank,
                        "rerank": item.rerank_score,
                    }
                    for item in result.evidence
                ],
                "citations": [
                    {
                        "doc_id": citation.article_id.split("@", 1)[0],
                        "article_id": citation.article_id,
                        "article_no": citation.article_no,
                        "part_no": citation.part_no,
                        "block_type": citation.block_type.value,
                        "quote": citation.quote,
                    }
                    for citation in result.citations
                ],
                "claims": [
                    {"claim_id": claim.claim_id, "text": claim.text} for claim in result.claims
                ],
                "issues": [
                    issue.model_dump(mode="json", exclude_none=True) for issue in result.issues
                ],
                "passed": result.outcome is expected,
            }
            rows.append(row)
            failed = failed or not row["passed"]
            print(
                f"[{'PASS' if row['passed'] else 'FAIL'}] {case_id} "
                f"outcome={row['outcome']} expected={row['expected_outcome']} "
                f"citations={len(row['citations'])} evidence={len(row['evidence'])}"
            )
    finally:
        service.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "p1-e2e-v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cases": rows,
        "summary": {
            "total": len(rows),
            "passed": sum(1 for row in rows if row["passed"]),
            "failed": sum(1 for row in rows if not row["passed"]),
        },
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"e2e: {payload['summary']['passed']}/{payload['summary']['total']}")
    print(f"report: {output_path}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
