"""W4 人群错配验收：通过真实 policy-search MCP 跑 5 个 case。

每个 case 给出“必须不出现”的 doc_id 前缀/集合；任意命中即失败。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from campusguide.config import load_mcp_config
from campusguide.env import load_dotenv
from campusguide.mcp.client import MCPClientConfig, MCPPolicySearchClient
from campusguide.mcp.schemas import SearchArticlesRequest

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Case:
    name: str
    query: str
    filters: dict
    forbidden_doc_ids: set[str]
    required_doc_ids: set[str] = frozenset()


CASES = [
    Case(
        name="1 undergraduate asks graduate-only scholarship",
        query="研究生国家奖学金申请条件",
        filters={"audience": ["undergraduate"], "as_of": "2026-10-24"},
        forbidden_doc_ids={"national-aid-grad", "academic-misconduct"},
    ),
    Case(
        name="2 graduate asks undergraduate course rule",
        query="本科生课程考核与成绩管理办法",
        filters={"audience": ["graduate"], "as_of": "2026-10-24"},
        forbidden_doc_ids={"course-study", "general-education-elective"},
    ),
    Case(
        name="3 agri student asks info-college plan",
        query="计算机科学与技术专业人才培养方案",
        filters={
            "audience": ["undergraduate"],
            "as_of": "2026-10-24",
            "profile": {"colleges": ["agri"]},
        },
        forbidden_doc_ids={
            "info-ug-cs-plan",
            "info-ug-se-plan",
            "info-ug-ds-plan",
            "info-ug-math-plan",
        },
    ),
    Case(
        name="4 non-agronomy major asks agronomy plan",
        query="农学专业培养方案",
        filters={
            "audience": ["undergraduate"],
            "as_of": "2026-10-24",
            "profile": {"colleges": ["agri"], "majors": ["seed-science"]},
        },
        forbidden_doc_ids={
            "agri-ug-plan-agronomy-2024",
            "agri-ug-plan-new-agronomy-2024",
            "agri-ug-plan-agronomy-dingying-2024",
        },
        required_doc_ids={"agri-ug-plan-seed-2024"},
    ),
    Case(
        name="5 2024 cohort asks 2022-only plan",
        query="农学专业 2022 版培养方案",
        filters={
            "audience": ["undergraduate"],
            "as_of": "2026-10-24",
            "profile": {
                "colleges": ["agri"],
                "majors": ["agronomy"],
                "intake_year": 2024,
            },
        },
        forbidden_doc_ids={
            "agri-ug-plan-2022",
            "agri-ug-plan-new-agronomy-2022",
        },
        required_doc_ids={
            "agri-ug-plan-agronomy-2024",
            "agri-ug-plan-new-agronomy-2024",
        },
    ),
]


def _config(server) -> MCPClientConfig:
    command = list(server.command)
    if command and command[0] in {"python", "python3"}:
        command[0] = sys.executable
    return MCPClientConfig(
        command=command,
        timeout_seconds=server.timeout_seconds,
        retries=server.retries,
        startup_timeout_seconds=server.startup_timeout_seconds,
        cwd=str(ROOT),
    )


def main() -> int:
    load_dotenv(ROOT / ".env")
    config = load_mcp_config(ROOT / "configs" / "mcp.yaml")
    client = MCPPolicySearchClient(_config(config.search_server))
    failures: list[str] = []
    try:
        started = time.perf_counter()
        client.start()
        print(f"policy-search ready in {int((time.perf_counter() - started) * 1000)}ms")
        for case in CASES:
            response = client.search_articles(
                SearchArticlesRequest(
                    query=case.query,
                    filters=case.filters,
                    top_k=30,
                    rerank_top_n=8,
                )
            )
            doc_ids = [hit.article.doc_id for hit in response.data.articles]
            unique = sorted(set(doc_ids))
            forbidden = sorted(set(unique) & case.forbidden_doc_ids)
            quality_hits = sorted(case.required_doc_ids & set(unique))
            # 设计验收只看“不返回不匹配条款”；required 仅作为检索质量观测。
            passed = not forbidden
            print(
                f"[{'PASS' if passed else 'FAIL'}] {case.name}\n"
                f"   docs={unique}\n"
                f"   forbidden={forbidden or '-'} quality_hits={quality_hits or '-'}"
            )
            if not passed:
                failures.append(case.name)
    finally:
        client.close()
    total = len(CASES)
    print(f"\npopulation cases: {total - len(failures)}/{total}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
