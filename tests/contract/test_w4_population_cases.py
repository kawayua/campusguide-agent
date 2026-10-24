from __future__ import annotations

import asyncio
from datetime import date

from fastmcp import Client
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import Audience
from campusguide.mcp.policy_search_server import (
    PolicySearchService,
    SearchRegistries,
    build_search_server,
)
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.hybrid import HybridRetriever
from campusguide.retrieval.indexer import build_vector_index, embedding_text
from campusguide.retrieval.sparse import BM25SparseEncoder
from tests.w2_helpers import make_article


class FixedReranker:
    @property
    def model_name(self) -> str:
        return "fixed"

    @property
    def device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        return None

    def score(self, query: str, passages: list[str]) -> list[float]:
        return [0.9 for _ in passages]


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        collection="w4_population",
        alias="w4_population_current",
        embedding_model="fake",
        dense_vector_size=64,
        min_rerank_score=0.0,
        min_score_margin=0.0,
    )


def _article(
    article_id: str,
    *,
    doc_id: str,
    text: str,
    audience,
    detail: AudienceDetail | None = None,
    effective_from: date = date(2021, 9, 1),
):
    article = make_article(
        article_id=article_id,
        doc_id=doc_id,
        text=text,
        audience=audience,
        effective_from=effective_from,
        article_no=article_id.rsplit("#", 1)[-1],
    )
    return article.model_copy(update={"audience_detail": detail})


def _build() -> tuple[PolicySearchService, QdrantClient]:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(
            "grad-scholarship@v2025#第一条",
            doc_id="grad-scholarship",
            text="研究生学业奖学金评审办法与申请条件。",
            audience=[Audience.GRADUATE],
        ),
        _article(
            "ug-scholarship@v2021#第一条",
            doc_id="ug-scholarship",
            text="本科生奖学金评审办法与申请条件。",
            audience=[Audience.UNDERGRADUATE],
        ),
        _article(
            "ug-course@v2016#第六条",
            doc_id="ug-course",
            text="本科生课程考核与成绩管理办法。",
            audience=[Audience.UNDERGRADUATE],
        ),
        _article(
            "grad-course@v2024#第六条",
            doc_id="grad-course",
            text="研究生课程考核与成绩管理办法。",
            audience=[Audience.GRADUATE],
        ),
        _article(
            "info-plan@v2024#二、培养目标",
            doc_id="info-plan",
            text="计算机科学与技术专业培养方案培养目标。",
            audience=[Audience.UNDERGRADUATE],
            detail=AudienceDetail(colleges=["info"], majors=["computer-science"]),
            effective_from=date(2024, 9, 1),
        ),
        _article(
            "agri-agronomy-2022@v2022#二、培养目标",
            doc_id="agri-agronomy-2022",
            text="农学专业 2022 版培养方案培养目标。",
            audience=[Audience.UNDERGRADUATE],
            detail=AudienceDetail(colleges=["agri"], majors=["agronomy"], intake_years=[2022]),
            effective_from=date(2022, 9, 1),
        ),
        _article(
            "agri-agronomy-2024@v2024#二、培养目标",
            doc_id="agri-agronomy-2024",
            text="农学专业 2024 版培养方案培养目标。",
            audience=[Audience.UNDERGRADUATE],
            detail=AudienceDetail(colleges=["agri"], majors=["agronomy"], intake_years=[2024]),
            effective_from=date(2024, 9, 1),
        ),
        _article(
            "agri-seed-2024@v2024#二、培养目标",
            doc_id="agri-seed-2024",
            text="种子科学与工程专业培养方案培养目标。",
            audience=[Audience.UNDERGRADUATE],
            detail=AudienceDetail(colleges=["agri"], majors=["seed-science"], intake_years=[2024]),
            effective_from=date(2024, 9, 1),
        ),
    ]
    encoder = BM25SparseEncoder.fit([embedding_text(article) for article in articles])
    config = _config()
    build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=config,
        sparse_encoder=encoder,
        recreate=True,
    )
    retriever = HybridRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        sparse_encoder=encoder,
        reranker=FixedReranker(),
        config=config,
        index_version="articles_v1@population-test",
        top_k=30,
        rerank_top_n=8,
        min_rerank_score=0.0,
        min_score_margin=0.0,
    )
    service = PolicySearchService(
        retriever,
        registries=SearchRegistries(doc_ids=set(), flow_ids=set()),
    )
    return service, client


def _search(service: PolicySearchService, request: dict) -> tuple[str, list[str], bool]:
    server = build_search_server(service)

    async def run():
        async with Client(server) as client:
            return await client.call_tool(
                "search_articles", {"request": request}, raise_on_error=False
            )

    async def _payload():
        result = await run()
        payload = result.structured_content
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        return payload

    payload = asyncio.run(_payload())
    assert payload["ok"] is True, payload
    data = payload["data"]
    return (
        data["index_version"],
        [item["article_id"] for item in data["articles"]],
        bool(data["warnings"]),
    )


def test_case1_undergraduate_does_not_receive_graduate_only_clause() -> None:
    service, _ = _build()
    _, ids, _ = _search(
        service,
        {
            "query": "研究生学业奖学金评审办法",
            "filters": {"audience": ["undergraduate"], "as_of": "2026-10-24"},
        },
    )
    assert not any("grad-scholarship" in article_id for article_id in ids)


def test_case2_graduate_does_not_receive_undergraduate_course_clause() -> None:
    service, _ = _build()
    _, ids, _ = _search(
        service,
        {
            "query": "本科生课程考核与成绩管理办法",
            "filters": {"audience": ["graduate"], "as_of": "2026-10-24"},
        },
    )
    assert not any("ug-course" in article_id for article_id in ids)


def test_case3_agri_student_does_not_receive_info_college_plan() -> None:
    service, _ = _build()
    _, ids, _ = _search(
        service,
        {
            "query": "计算机科学与技术专业培养方案",
            "filters": {
                "audience": ["undergraduate"],
                "as_of": "2026-10-24",
                "profile": {"colleges": ["agri"]},
            },
        },
    )
    assert not any("info-plan" in article_id for article_id in ids)


def test_case4_non_agronomy_major_does_not_receive_agronomy_plan() -> None:
    service, _ = _build()
    _, ids, _ = _search(
        service,
        {
            "query": "专业培养方案",
            "filters": {
                "audience": ["undergraduate"],
                "as_of": "2026-10-24",
                "profile": {"colleges": ["agri"], "majors": ["seed-science"]},
            },
        },
    )
    assert not any("agri-agronomy" in article_id for article_id in ids)
    assert any("agri-seed-2024" in article_id for article_id in ids)


def test_case5_2024_cohort_does_not_receive_2022_only_plan() -> None:
    service, _ = _build()
    _, ids, _ = _search(
        service,
        {
            "query": "农学专业培养方案",
            "filters": {
                "audience": ["undergraduate"],
                "as_of": "2026-10-24",
                "profile": {
                    "colleges": ["agri"],
                    "majors": ["agronomy"],
                    "intake_year": 2024,
                },
            },
        },
    )
    assert not any("agri-agronomy-2022" in article_id for article_id in ids)
    assert any("agri-agronomy-2024" in article_id for article_id in ids)
