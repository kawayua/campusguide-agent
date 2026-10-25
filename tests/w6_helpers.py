"""W6 离线测试共用：真实语料 article + fake search/LLM。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from campusguide.agents.advisor import Advisor
from campusguide.agents.graph import GraphNodes, GraphRunner
from campusguide.agents.planner import Planner
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.retriever import GraphRetriever
from campusguide.agents.verifier import GraphVerifier
from campusguide.config import load_handoff_config
from campusguide.domain.models import Article, ArticlePublic
from campusguide.flows.calendar import load_calendar
from campusguide.flows.catalog import LocalFlowCatalog
from campusguide.flows.registry import FlowRegistry
from campusguide.mcp.client import SearchResponse
from campusguide.mcp.schemas import (
    ErrorCode,
    GetArticleData,
    SearchArticlesData,
    SearchHit,
    ToolError,
)
from campusguide.verification.semantic import SemanticVerifier

ROOT = Path(__file__).resolve().parents[1]
ARTICLES_PATH = ROOT / "data" / "processed" / "articles.jsonl"


def load_articles() -> dict[str, Article]:
    articles: dict[str, Article] = {}
    with ARTICLES_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            article = Article.model_validate(json.loads(line))
            articles.setdefault(article.article_id, article)
    return articles


ARTICLES = load_articles()


def article(article_id: str) -> Article:
    return ARTICLES[article_id]


def catalog() -> LocalFlowCatalog:
    registry = FlowRegistry.from_directory(ROOT / "configs" / "flows")
    calendar = {
        event.event_id: event for event in load_calendar(ROOT / "configs" / "calendar.yaml")
    }
    return LocalFlowCatalog(registry, calendar=calendar)


def hit(item: Article, *, score: float = 0.9, rank: int = 1, query: str = "") -> SearchHit:
    return SearchHit(
        article_id=item.article_id,
        part_no=item.part_no,
        block_type=item.block_type.value,
        article=ArticlePublic.from_article(item),
        rerank_score=score,
        rank=rank,
        retrieval_query=query,
    )


class FakeSearchClient:
    def __init__(
        self,
        *,
        by_query: dict[str, list[str]] | None = None,
        default: list[str] | None = None,
        index_version: str = "test-index",
        fail_queries: set[str] | None = None,
        fail_code: ErrorCode = ErrorCode.TOOL_UNAVAILABLE,
    ) -> None:
        self.by_query = by_query or {}
        self.default = default if default is not None else ["national-aid-undergrad@v2021#第九条"]
        self.index_version = index_version
        self.fail_queries = fail_queries or set()
        self.fail_code = fail_code
        self.calls: list[str] = []

    def warmup(self) -> None:
        return None

    def search_articles(self, request, *, timeout=None) -> SearchResponse:
        query = request.query
        self.calls.append(query)
        if query in self.fail_queries:
            raise ToolError(self.fail_code, "search failed", retryable=True)
        ids = self.by_query.get(query, self.default)
        hits = [hit(article(item), rank=index + 1, query=query) for index, item in enumerate(ids)]
        return SearchResponse(
            data=SearchArticlesData(
                query=query,
                queries=[query],
                articles=hits,
                index_version=self.index_version,
            )
        )

    def get_article(self, article_id: str, part_no: int | None = None) -> GetArticleData:
        item = ARTICLES.get(article_id)
        if item is None or (part_no is not None and item.part_no != part_no):
            raise ToolError(ErrorCode.NOT_FOUND, "not found")
        return GetArticleData(article=ArticlePublic.from_article(item))

    def close(self) -> None:
        return None


def make_nodes(
    *,
    search_client=None,
    advisor_responses: list[str | Exception] | None = None,
    semantic_responses: list[str | Exception] | None = None,
    max_revise: int = 2,
    semantic_mode: str = "strict",
    flow_catalog: LocalFlowCatalog | None = None,
    advisor_max_tokens: int = 3000,
) -> GraphNodes:
    flow_catalog = flow_catalog or catalog()
    advisor = Advisor(
        FakeChatProvider(advisor_responses or []),
        max_tokens=advisor_max_tokens,
        enable_quote_repair=False,
    )
    semantic = SemanticVerifier(
        FakeChatProvider(semantic_responses or []),
    )
    return GraphNodes(
        planner=Planner(flow_catalog, llm_enabled=False),
        retriever=GraphRetriever(
            search_client or FakeSearchClient(), expected_index_version="test-index"
        ),
        advisor=advisor,
        verifier=GraphVerifier(
            semantic_verifier=semantic,
            max_revise=max_revise,
            semantic_mode=semantic_mode,
        ),
        catalog=flow_catalog,
        handoff_config=load_handoff_config(ROOT / "configs" / "handoff.yaml"),
        max_clarify=3,
        max_revise=max_revise,
    )


def make_runner(**kwargs) -> GraphRunner:
    return GraphRunner(make_nodes(**kwargs))


def supported_json(claim_id: str = "c1") -> str:
    return json.dumps(
        {"labels": [{"claim_id": claim_id, "label": "supported", "detail": "ok"}]},
        ensure_ascii=False,
    )


def claims_json(article_id: str, quote: str, text: str = "这是一条可核对的结论。") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": text,
                    "kind": "eligibility",
                    "citations": [{"article_id": article_id, "quote": quote}],
                }
            ]
        },
        ensure_ascii=False,
    )


DEFAULT_AS_OF = date(2026, 10, 25)


__all__ = [
    "ARTICLES",
    "DEFAULT_AS_OF",
    "FakeSearchClient",
    "article",
    "catalog",
    "claims_json",
    "hit",
    "make_nodes",
    "make_runner",
    "supported_json",
]
