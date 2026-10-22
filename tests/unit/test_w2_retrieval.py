from __future__ import annotations

from datetime import date

from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.enums import BlockType
from campusguide.domain.models import PolicyFilters
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.indexer import build_vector_index
from campusguide.retrieval.service import DenseRetriever
from tests.w2_helpers import make_article


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        collection="w2_retrieval",
        alias="w2_retrieval_current",
        embedding_model="fake",
        dense_vector_size=64,
        token_budget=4000,
    )


def _retriever(client: QdrantClient) -> DenseRetriever:
    return DenseRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        config=_config(),
        top_k=10,
        max_evidence=8,
        min_dense_score=0.0,
    )


def _index(client: QdrantClient) -> None:
    articles = [
        make_article(
            article_id="aid@v2021#第一条",
            doc_id="aid",
            text="国家奖学金申请条件包括学习成绩排名前百分之十。",
        ),
        make_article(
            article_id="aid@v2021#第二条",
            doc_id="aid",
            article_no="第二条",
            article_index=2,
            text="国家奖学金奖励标准为每人每年八千元。",
        ),
        make_article(
            article_id="loan@v2018#第一条",
            doc_id="loan",
            doc_title="助学贷款办法",
            text="国家助学贷款用于支付学费和住宿费。",
        ),
        make_article(
            article_id="plan@v2024#二、培养目标",
            doc_id="plan",
            doc_title="培养方案",
            article_no="二、培养目标",
            article_index=2,
            block_type=BlockType.SECTION,
            text="本专业培养具有社会责任感的数学人才。",
        ),
    ]
    build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=_config(),
        recreate=True,
    )


def test_dense_retrieval_dedups_by_article_id_and_keeps_best_part() -> None:
    client = QdrantClient(location=":memory:")
    _index(client)
    result = _retriever(client).retrieve("国家奖学金申请条件")

    ids = [item.article.article_id for item in result.evidence]
    assert ids[0] == "aid@v2021#第一条"
    assert ids.count("aid@v2021#第一条") == 1
    assert len(ids) == len(set(ids))
    assert result.evidence[0].rank == 1
    assert result.evidence[0].retrieval_query == "国家奖学金申请条件"


def test_default_filter_excludes_section_blocks() -> None:
    client = QdrantClient(location=":memory:")
    _index(client)
    retriever = _retriever(client)
    result = retriever.retrieve(
        "培养目标",
        filters=PolicyFilters(as_of=date(2026, 10, 22)),
    )
    assert all(item.article.block_type is not BlockType.SECTION for item in result.evidence)

    section_result = retriever.retrieve(
        "培养目标",
        filters=PolicyFilters(
            as_of=date(2026, 10, 22),
            block_types=[BlockType.SECTION],
        ),
    )
    assert section_result.evidence
    assert section_result.evidence[0].article.block_type is BlockType.SECTION


def test_expired_version_is_filtered_out() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        make_article(
            article_id="old@v2019#第一条",
            doc_id="old",
            effective_to=date(2020, 12, 31),
            text="旧版奖学金办法已经废止。",
        ),
        make_article(
            article_id="new@v2021#第一条",
            doc_id="new",
            text="现行奖学金办法适用。",
        ),
    ]
    build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=_config(),
        recreate=True,
    )
    result = _retriever(client).retrieve("奖学金办法")
    ids = [item.article.article_id for item in result.evidence]
    assert "old@v2019#第一条" not in ids
    assert "new@v2021#第一条" in ids
