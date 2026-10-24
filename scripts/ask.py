"""单题 CLI：便于调试检索、生成、校验与 trace。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from campusguide.domain.enums import Audience, AudienceStage
from campusguide.domain.profiles import UserProfile
from campusguide.env import load_dotenv
from campusguide.qa.runtime import create_mcp_qa_service

ROOT = Path(__file__).resolve().parents[1]
AUDIENCE_MAP = {
    "undergraduate": Audience.UNDERGRADUATE,
    "graduate": Audience.GRADUATE,
    "faculty": Audience.FACULTY,
}


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", help="单轮问题")
    parser.add_argument("--audience", choices=sorted(AUDIENCE_MAP), default=None)
    parser.add_argument("--college", action="append", default=[], help="学院 ID，可重复")
    parser.add_argument("--major", action="append", default=[], help="专业 ID，可重复")
    parser.add_argument("--intake-year", type=int, default=None)
    parser.add_argument("--stage", choices=[item.value for item in AudienceStage], default=None)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="不等待检索模型加载，用于测量冷启动首问延迟",
    )
    args = parser.parse_args()
    question = args.question or input("问题：").strip()
    if not question:
        print("error: 问题为空", file=sys.stderr)
        return 2

    profile = UserProfile(
        colleges=args.college,
        majors=args.major,
        intake_year=args.intake_year,
        stage=AudienceStage(args.stage) if args.stage else None,
    )
    try:
        service = create_mcp_qa_service(start=not args.no_warmup)
    except Exception as exc:
        print(
            f"error: 服务未就绪（{type(exc).__name__}）：{exc}\n"
            "请检查 .env 的 LLM_BASE_URL / LLM_API_KEY / ADVISOR_MODEL、"
            "Qdrant 是否启动，以及 data/vector_manifest.json 是否已生成。",
            file=sys.stderr,
        )
        return 2
    warmup_ms = 0
    if not args.no_warmup:
        warmup_started = time.perf_counter()
        service.warmup()
        warmup_ms = int((time.perf_counter() - warmup_started) * 1000)
        print(f"-- warmup={warmup_ms}ms", file=sys.stderr)
    try:
        result = service.ask(
            question,
            audience=AUDIENCE_MAP.get(args.audience) if args.audience else None,
            profile=profile if not profile.is_empty else None,
        )
    finally:
        service.close()

    if args.json:
        payload = {
            "trace_id": result.trace_id,
            "outcome": result.outcome.value,
            "decision": result.decision.value if result.decision else None,
            "partial": result.partial,
            "answer_markdown": result.answer_markdown,
            "message": result.message,
            "warmup_ms": warmup_ms,
            "latency_scope": "cold" if args.no_warmup else "warm",
            "duration_ms": result.duration_ms,
            "probe_ms": result.probe_ms,
            "qa_ms": result.qa_ms,
            "queries": result.queries,
            "index_version": result.index_version,
            "mcp": result.mcp,
            "retrieval_timing": result.retrieval_timing,
            "retrieval_warnings": result.retrieval_warnings,
            "version_metadata": result.version_metadata,
            "quote_repair": result.quote_repair,
            "issues": [issue.model_dump(mode="json", exclude_none=True) for issue in result.issues],
            "scope_issues": [
                issue.model_dump(mode="json", exclude_none=True) for issue in result.scope_issues
            ],
            "dropped_claim_ids": result.dropped_claim_ids,
            "rendered_claim_ids": result.rendered_claim_ids,
            "repealed_probe": result.probe,
            "citations": [
                {
                    "article_id": citation.article_id,
                    "part_no": citation.part_no,
                    "block_type": citation.block_type.value,
                    "doc_title": citation.doc_title,
                    "article_no": citation.article_no,
                    "quote": citation.quote,
                    "quote_start": citation.quote_start,
                    "quote_end": citation.quote_end,
                    "source_url": str(citation.source_url),
                    "date_confidence": citation.date_confidence,
                }
                for citation in result.citations
            ],
            "retrieval": result.retrieval_scores,
            "dropped_claims": result.dropped_claims,
            "dropped_citations": result.dropped_citations,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if result.renders_answer:
        print(result.answer_markdown)
    else:
        print(f"[{result.outcome.value}] {result.message}")
    print(
        f"\n-- trace_id={result.trace_id} duration={result.duration_ms}ms "
        f"probe={result.probe_ms}ms qa={result.qa_ms}ms "
        f"decision={result.decision.value if result.decision else 'n/a'} "
        f"warmup={warmup_ms}ms scope={'cold' if args.no_warmup else 'warm'}"
    )
    if result.retrieval_scores:
        print("-- retrieval:")
        for item in result.retrieval_scores:
            print(
                f"   #{item['rank']} {item['article_id']}#part{item['part_no']} "
                f"{item['score']:.4f} [{item['block_type']}]"
            )
    for issue in result.issues:
        claim = f" claim={issue.claim_id}" if issue.claim_id else ""
        print(f"-- issue: [{issue.severity.value}/{issue.scope.value}] {issue.code}{claim}")
    for dropped in result.dropped_claims:
        print(f"-- dropped claim: {dropped}")
    for dropped in result.dropped_citations:
        print(f"-- dropped citation: {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
