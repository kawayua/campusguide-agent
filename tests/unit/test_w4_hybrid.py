from __future__ import annotations

from datetime import date

import pytest
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.models import PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.hybrid import HybridRetriever, VersionConflictError
from campusguide.retrieval.indexer import build_vector_index, embedding_text
from campusguide.retrieval.rerank import BrokenReranker, RerankUnavailable
from campusguide.retrieval.service import DenseRetriever
from campusguide.retrieval.sparse import BM25SparseEncoder
from tests.w2_helpers import make_article


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        collection="w4_hybrid",
        alias="w4_hybrid_current",
        embedding_model="fake",
        dense_vector_size=64,
        token_budget=4000,
    )


class FixedReranker:
    def __init__(self, mapping: dict[str, float] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "fixed-reranker"

    @property
    def device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        return None

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls += 1
        return [self.mapping.get(passage, 0.5) for passage in passages]


def _article(
    article_id: str,
    *,
    doc_id: str,
    text: str,
    audience: list[Audience] | None = None,
    detail: AudienceDetail | None = None,
    effective_from: date = date(2021, 9, 1),
    version_id: str = "v2021",
):
    article = make_article(
        article_id=article_id,
        doc_id=doc_id,
        version_id=version_id,
        text=text,
        audience=audience,
        effective_from=effective_from,
        article_no=article_id.rsplit("#", 1)[-1],
    )
    return article.model_copy(update={"audience_detail": detail})


def _build(client: QdrantClient, articles):
    encoder = BM25SparseEncoder.fit([embedding_text(article) for article in articles])
    build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=_config(),
        sparse_encoder=encoder,
        recreate=True,
    )
    return encoder


def _retriever(
    client: QdrantClient,
    encoder: BM25SparseEncoder,
    *,
    reranker=None,
    use_rerank: bool = True,
) -> HybridRetriever:
    return HybridRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        sparse_encoder=encoder,
        reranker=reranker or FixedReranker(),
        config=_config(),
        index_version="w4-test-index",
        top_k=30,
        rerank_top_n=8,
        max_articles=8,
        min_rerank_score=0.0,
        min_score_margin=0.0,
        use_rerank=use_rerank,
    )


def test_hybrid_rrf_dedups_and_keeps_scores() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(
            "aid@v2021#第一条",
            doc_id="aid",
            text="国家奖学金申请条件包括学习成绩排名前百分之十。",
        ),
        _article(
            "aid@v2021#第二条",
            doc_id="aid",
            text="国家奖学金奖励标准为每人每年八千元。",
        ),
        _article(
            "loan@v2018#第一条",
            doc_id="loan",
            text="国家助学贷款用于支付学费和住宿费。",
        ),
    ]
    encoder = _build(client, articles)
    result = _retriever(client, encoder).search_articles("国家奖学金申请条件")

    ids = [item.article.article_id for item in result.evidence]
    assert ids
    assert ids.count(ids[0]) == 1
    top = result.evidence[0]
    assert top.dense_score is not None
    assert top.sparse_score is not None
    assert top.rrf_score is not None
    assert top.rerank_score is not None
    assert result.index_version == "w4-test-index"
    assert result.timing["dense_ms"] >= 0
    assert result.timing["sparse_ms"] >= 0
    assert result.timing["rerank_ms"] >= 0


def test_hybrid_without_rerank_uses_rrf_order() -> None:
    client = QdrantClient(location=":memory:")
    encoder = _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="本科生转专业需要满足基本条件。")],
    )
    result = _retriever(client, encoder, use_rerank=False).search_articles("转专业条件")
    assert result.evidence
    assert result.evidence[0].rerank_score is None
    assert result.timing["rerank_ms"] == 0


def test_reranker_reorders_and_applies_top_n() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。"),
        _article("b@v2021#第一条", doc_id="b", text="奖学金评审程序。"),
        _article("c@v2021#第一条", doc_id="c", text="奖学金发放时间。"),
    ]
    encoder = _build(client, articles)
    retriever = _retriever(client, encoder, reranker=FixedReranker())
    # 用 reranker 分数决定顺序：c > a > b。
    retriever._reranker = FixedReranker(
        {
            "标题：演示办法\n章节：第一章 总则\n位置：第一条\n正文：奖学金发放时间。": 0.95,
            "标题：演示办法\n章节：第一章 总则\n位置：第一条\n正文：奖学金申请条件。": 0.80,
            "标题：演示办法\n章节：第一章 总则\n位置：第一条\n正文：奖学金评审程序。": 0.40,
        }
    )
    result = retriever.search_articles("奖学金", rerank_top_n=2)
    assert [item.article.doc_id for item in result.evidence] == ["c", "a"]
    assert result.evidence[0].rerank_score == pytest.approx(0.95)


def test_profile_college_filter_excludes_other_college() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(
            "info-plan@v2024#二、培养目标",
            doc_id="info-plan",
            text="计算机科学与技术专业培养目标。",
            detail=AudienceDetail(colleges=["info"], majors=["computer-science"]),
        ),
        _article(
            "agri-plan@v2024#二、培养目标",
            doc_id="agri-plan",
            text="农学专业培养目标。",
            detail=AudienceDetail(colleges=["agri"], majors=["agronomy"]),
        ),
        _article(
            "university-plan@v2024#二、培养目标",
            doc_id="university-plan",
            text="全校培养目标。",
        ),
    ]
    encoder = _build(client, articles)
    result = _retriever(client, encoder).search_articles(
        "培养目标",
        filters=PolicyFilters(
            as_of=date(2026, 10, 24),
            block_types=[BlockType.ARTICLE],
            profile=UserProfile(colleges=["agri"]),
        ),
    )
    ids = {item.article.doc_id for item in result.evidence}
    assert "info-plan" not in ids
    assert "agri-plan" in ids
    assert "university-plan" in ids
    assert any("colleges=agri" in item for item in result.applied_filters)


def test_audience_filter_excludes_graduate_only_for_undergraduate() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(
            "grad-rule@v2024#第一条",
            doc_id="grad-rule",
            text="研究生奖学金评审办法。",
            audience=[Audience.GRADUATE],
        ),
        _article(
            "ug-rule@v2021#第一条",
            doc_id="ug-rule",
            text="本科生奖学金评审办法。",
            audience=[Audience.UNDERGRADUATE],
        ),
    ]
    encoder = _build(client, articles)
    result = _retriever(client, encoder).search_articles(
        "奖学金评审办法",
        filters=PolicyFilters(as_of=date(2026, 10, 24), audience=[Audience.UNDERGRADUATE]),
    )
    assert {item.article.doc_id for item in result.evidence} == {"ug-rule"}


def test_multiple_articles_of_same_document_are_both_kept() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(
            "aid@v2021#第七条",
            doc_id="aid",
            text="国家奖学金奖励标准为每人每年八千元。",
        ),
        _article(
            "aid@v2021#第九条",
            doc_id="aid",
            text="国家奖学金申请条件包括学习成绩优异。",
        ),
    ]
    encoder = _build(client, articles)
    result = _retriever(client, encoder).search_articles("国家奖学金申请条件与奖励标准")
    assert {item.article.article_id for item in result.evidence} == {
        "aid@v2021#第七条",
        "aid@v2021#第九条",
    }


def test_version_conflict_raises() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("conflict@v1#第一条", doc_id="conflict", text="奖学金版本一。", version_id="v1"),
        _article("conflict@v2#第一条", doc_id="conflict", text="奖学金版本二。", version_id="v2"),
    ]
    encoder = _build(client, articles)
    with pytest.raises(VersionConflictError):
        _retriever(client, encoder).search_articles(
            "奖学金",
            filters=PolicyFilters(as_of=date(2026, 10, 24)),
        )


def test_reranker_unavailable_fails_closed() -> None:
    client = QdrantClient(location=":memory:")
    encoder = _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。")],
    )
    with pytest.raises(RerankUnavailable):
        _retriever(client, encoder, reranker=BrokenReranker()).search_articles("奖学金")


def test_dense_only_retriever_still_reads_hybrid_collection() -> None:
    client = QdrantClient(location=":memory:")
    _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。")],
    )
    dense = DenseRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        config=_config(),
        top_k=10,
        max_evidence=8,
        min_dense_score=0.0,
    )
    result = dense.retrieve("奖学金申请条件")
    assert result.evidence
    assert result.evidence[0].article.article_id == "a@v2021#第一条"


def test_get_article_returns_none_for_missing() -> None:
    client = QdrantClient(location=":memory:")
    encoder = _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。")],
    )
    retriever = _retriever(client, encoder)
    assert retriever.get_article("a@v2021#第一条") is not None
    assert retriever.get_article("missing@v1#第一条") is None
