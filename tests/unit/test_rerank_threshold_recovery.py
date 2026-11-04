from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import ArticleStatus
from campusguide.domain.models import PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.retrieval.budget import evidence_cost
from campusguide.retrieval.corpus_views import CorpusViews
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.hybrid import HybridRetriever
from campusguide.retrieval.indexer import build_vector_index, embedding_text
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.rerank import BrokenReranker, RerankUnavailable
from campusguide.retrieval.service import RetrievalError
from campusguide.retrieval.sparse import BM25SparseEncoder
from tests.w2_helpers import make_article


def _config(
    *,
    min_rerank_score: float = 0.30,
    fallback_min: float | None = None,
    fallback_max: int = 3,
) -> RetrievalConfig:
    return RetrievalConfig(
        collection="w4_hybrid",
        alias="w4_hybrid_current",
        embedding_model="fake",
        dense_vector_size=64,
        token_budget=4000,
        min_rerank_score=min_rerank_score,
        rerank_fallback_min_score=fallback_min,
        rerank_fallback_max_articles=fallback_max,
    )


class TextScoreReranker:
    """Map each article's original text to a fixed model score."""

    def __init__(self, scores: dict[str, float], default: float = 0.0) -> None:
        self.scores = scores
        self.default = default
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "text-score-reranker"

    @property
    def device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        return None

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls += 1
        values: list[float] = []
        for passage in passages:
            values.append(
                next(
                    (score for text, score in self.scores.items() if text in passage),
                    self.default,
                )
            )
        return values


def _article(
    article_id: str,
    *,
    doc_id: str,
    text: str,
    status: ArticleStatus = ArticleStatus.ACTIVE,
    effective_from: date = date(2021, 9, 1),
    detail: AudienceDetail | None = None,
    version_id: str = "v2021",
):
    article = make_article(
        article_id=article_id,
        doc_id=doc_id,
        version_id=version_id,
        text=text,
        status=status,
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
    strong: float = 0.0,
    margin: float = 0.0,
    fallback_min: float | None = None,
    fallback_max: int = 3,
    token_budget: int | None = None,
    corpus_views: CorpusViews | None = None,
) -> HybridRetriever:
    return HybridRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        sparse_encoder=encoder,
        reranker=reranker or TextScoreReranker({}),
        config=_config(
            min_rerank_score=strong,
            fallback_min=fallback_min,
            fallback_max=fallback_max,
        ),
        index_version="w4-test-index",
        top_k=30,
        rerank_top_n=8,
        max_articles=8,
        min_rerank_score=strong,
        min_score_margin=margin,
        token_budget=token_budget,
        use_rerank=use_rerank,
        corpus_views=corpus_views,
    )


def _decision(result) -> str:
    return next(
        warning for warning in result.warnings if warning.startswith("RERANK_FALLBACK:")
    )


def test_fallback_disabled_keeps_normal_reject() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("low@v2021#第一条", doc_id="low", text="奖学金申请条件甲。"),
        _article("high@v2021#第一条", doc_id="high", text="奖学金申请条件乙。"),
    ]
    encoder = _build(client, articles)
    reranker = TextScoreReranker(
        {"奖学金申请条件甲。": 0.817, "奖学金申请条件乙。": 0.838}
    )

    result = _retriever(
        client, encoder, reranker=reranker, strong=0.85, margin=0.0
    ).search_articles("奖学金申请条件")

    assert result.evidence == []
    assert any("RERANK_BELOW_MARGIN" in warning for warning in result.warnings)
    assert not any("RERANK_FALLBACK" in warning for warning in result.warnings)


def test_fallback_floor_admits_weak_scores_unchanged() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("low@v2021#第一条", doc_id="low", text="奖学金申请条件甲。"),
        _article("high@v2021#第一条", doc_id="high", text="奖学金申请条件乙。"),
    ]
    encoder = _build(client, articles)
    reranker = TextScoreReranker(
        {"奖学金申请条件甲。": 0.817, "奖学金申请条件乙。": 0.838}
    )

    result = _retriever(
        client,
        encoder,
        reranker=reranker,
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    ).search_articles("奖学金申请条件")

    by_id = {item.article.article_id: item for item in result.evidence}
    assert set(by_id) == {"low@v2021#第一条", "high@v2021#第一条"}
    assert by_id["high@v2021#第一条"].rerank_score == pytest.approx(0.838)
    assert by_id["low@v2021#第一条"].rerank_score == pytest.approx(0.817)
    assert [item.article.article_id for item in result.evidence] == [
        "high@v2021#第一条",
        "low@v2021#第一条",
    ]
    assert [item.rank for item in result.evidence] == [1, 2]
    decision = _decision(result)
    assert "applied=true" in decision
    assert "strong=0.8500" in decision
    assert "floor=0.8000" in decision
    assert "accepted=2" in decision
    assert any(
        "RERANK_FALLBACK_ACCEPT:high@v2021#第一条:p1:score=0.8380" in warning
        for warning in result.warnings
    )


def test_fallback_below_floor_stays_empty_with_reject_diagnostics() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("one@v2021#第一条", doc_id="one", text="奖学金申请条件甲。"),
        _article("two@v2021#第一条", doc_id="two", text="奖学金申请条件乙。"),
    ]
    encoder = _build(client, articles)
    reranker = TextScoreReranker(
        {"奖学金申请条件甲。": 0.79, "奖学金申请条件乙。": 0.70}
    )

    result = _retriever(
        client,
        encoder,
        reranker=reranker,
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    ).search_articles("奖学金申请条件")

    assert result.evidence == []
    decision = _decision(result)
    assert "applied=false" in decision
    assert "accepted=0" in decision
    rejected = [
        warning
        for warning in result.warnings
        if warning.startswith("RERANK_FALLBACK_REJECT:")
    ]
    assert rejected == [
        "RERANK_FALLBACK_REJECT:one@v2021#第一条:p1:score=0.7900",
        "RERANK_FALLBACK_REJECT:two@v2021#第一条:p1:score=0.7000",
    ]


def test_existing_strong_match_does_not_append_weak_fallback_rows() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("strong@v2021#第一条", doc_id="strong", text="奖学金申请条件甲。"),
        _article("weak@v2021#第一条", doc_id="weak", text="奖学金申请条件乙。"),
    ]
    encoder = _build(client, articles)
    reranker = TextScoreReranker(
        {"奖学金申请条件甲。": 0.90, "奖学金申请条件乙。": 0.81}
    )

    result = _retriever(
        client,
        encoder,
        reranker=reranker,
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    ).search_articles("奖学金申请条件")

    assert [item.article.article_id for item in result.evidence] == [
        "strong@v2021#第一条"
    ]
    assert any(
        "RERANK_DROP:weak@v2021#第一条" in warning for warning in result.warnings
    )
    decision = _decision(result)
    assert "applied=false" in decision
    assert "reason=normal_evidence_present" in decision
    assert not any(
        "RERANK_FALLBACK_ACCEPT" in warning for warning in result.warnings
    )


def test_fallback_cap_is_three() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(f"a{index}@v2021#第一条", doc_id=f"a{index}", text=f"奖学金条件{index}。")
        for index in range(5)
    ]
    encoder = _build(client, articles)
    scores = {f"奖学金条件{index}。": 0.84 - index * 0.01 for index in range(5)}
    result = _retriever(
        client,
        encoder,
        reranker=TextScoreReranker(scores),
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    ).search_articles("奖学金条件")

    assert len(result.evidence) == 3
    assert [item.article.article_id for item in result.evidence] == [
        "a0@v2021#第一条",
        "a1@v2021#第一条",
        "a2@v2021#第一条",
    ]
    assert "accepted=3" in _decision(result)


def test_fallback_respects_rerank_top_n() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(f"b{index}@v2021#第一条", doc_id=f"b{index}", text=f"奖学金评审{index}。")
        for index in range(4)
    ]
    encoder = _build(client, articles)
    scores = {f"奖学金评审{index}。": 0.84 - index * 0.01 for index in range(4)}
    result = _retriever(
        client,
        encoder,
        reranker=TextScoreReranker(scores),
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    ).search_articles("奖学金评审", rerank_top_n=2)

    assert len(result.evidence) == 2
    assert "accepted=3" in _decision(result)


def test_fallback_respects_token_budget() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("first@v2021#第一条", doc_id="first", text="奖学金发放标准甲。"),
        _article("second@v2021#第一条", doc_id="second", text="奖学金发放标准乙。"),
    ]
    encoder = _build(client, articles)
    budget = evidence_cost(articles[0])
    result = _retriever(
        client,
        encoder,
        reranker=TextScoreReranker(
            {"奖学金发放标准甲。": 0.84, "奖学金发放标准乙。": 0.83}
        ),
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
        token_budget=budget,
    ).search_articles("奖学金发放标准")

    assert [item.article.article_id for item in result.evidence] == [
        "first@v2021#第一条"
    ]
    assert "accepted=2" in _decision(result)
    assert any("TOKEN_BUDGET" in warning for warning in result.warnings)


def test_fallback_respects_margin_rejection() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article("lean@v2021#第一条", doc_id="lean", text="奖学金评审程序甲。"),
        _article("other@v2021#第一条", doc_id="other", text="奖学金评审程序乙。"),
    ]
    encoder = _build(client, articles)
    reranker = TextScoreReranker(
        {"奖学金评审程序甲。": 0.86, "奖学金评审程序乙。": 0.81}
    )

    result = _retriever(
        client,
        encoder,
        reranker=reranker,
        strong=0.85,
        margin=0.03,
        fallback_min=0.80,
    ).search_articles("奖学金评审程序")

    assert {item.article.article_id for item in result.evidence} == {
        "lean@v2021#第一条",
        "other@v2021#第一条",
    }
    assert any("RERANK_BELOW_MARGIN" in warning for warning in result.warnings)
    assert "applied=true" in _decision(result)


def _filtered_result(article, filters: PolicyFilters):
    client = QdrantClient(location=":memory:")
    encoder = _build(client, [article])
    return _retriever(
        client,
        encoder,
        reranker=TextScoreReranker({article.text: 0.90}),
        strong=0.95,
        margin=0.0,
        fallback_min=0.80,
    ).search_articles("奖学金", filters=filters)


def test_repealed_candidate_not_admitted_by_fallback() -> None:
    article = _article(
        "old@v2021#第一条",
        doc_id="old",
        text="奖学金旧办法。",
        status=ArticleStatus.REPEALED,
    )
    result = _filtered_result(article, PolicyFilters(as_of=date(2026, 10, 24)))
    assert result.evidence == []
    assert not any(
        "RERANK_FALLBACK_ACCEPT" in warning for warning in result.warnings
    )


def test_future_candidate_not_admitted_by_fallback() -> None:
    article = _article(
        "future@v2027#第一条",
        doc_id="future",
        text="奖学金新办法。",
        effective_from=date(2027, 1, 1),
        version_id="v2027",
    )
    result = _filtered_result(article, PolicyFilters(as_of=date(2026, 10, 24)))
    assert result.evidence == []
    assert not any(
        "RERANK_FALLBACK_ACCEPT" in warning for warning in result.warnings
    )


def test_wrong_profile_candidate_not_admitted_by_fallback() -> None:
    article = _article(
        "agri@v2021#第一条",
        doc_id="agri",
        text="奖学金评定办法。",
        detail=AudienceDetail(colleges=["agri"]),
    )
    result = _filtered_result(
        article,
        PolicyFilters(
            as_of=date(2026, 10, 24),
            profile=UserProfile(colleges=["info"]),
        ),
    )
    assert result.evidence == []
    assert not any(
        "RERANK_FALLBACK_ACCEPT" in warning for warning in result.warnings
    )


def test_structured_bypass_is_unchanged_and_blocks_fallback() -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        _article(
            "course@v2021#第一条",
            doc_id="course",
            text="123456 | 课程名称 | 学分",
        ),
        _article("weak@v2021#第一条", doc_id="weak", text="课程安排另行通知。"),
    ]
    encoder = _build(client, articles)
    views = CorpusViews(articles, ProfileLexicon({}, {}))
    reranker = TextScoreReranker(
        {"123456 | 课程名称 | 学分": 0.40, "课程安排另行通知。": 0.82}
    )

    result = _retriever(
        client,
        encoder,
        reranker=reranker,
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
        corpus_views=views,
    ).search_articles("123456课程安排")

    assert [item.article.article_id for item in result.evidence] == [
        "course@v2021#第一条"
    ]
    assert result.evidence[0].structured is True
    assert result.evidence[0].rerank_score == pytest.approx(0.40)
    assert any(
        "STRUCTURED_THRESHOLD_BYPASS:course@v2021#第一条" in warning
        for warning in result.warnings
    )
    decision = _decision(result)
    assert "applied=false" in decision
    assert "reason=normal_evidence_present" in decision
    assert not any(
        "RERANK_FALLBACK_ACCEPT" in warning for warning in result.warnings
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_rerank_score": 0.85, "fallback_min": 0.85},
        {"min_rerank_score": 0.85, "fallback_min": -0.01},
        {"min_rerank_score": 0.85, "fallback_min": 1.01},
        {"min_rerank_score": 0.85, "fallback_min": float("nan")},
        {"min_rerank_score": 0.85, "fallback_min": 0.80, "fallback_max": 0},
        {"min_rerank_score": 0.85, "fallback_min": 0.80, "fallback_max": 4},
    ],
)
def test_invalid_fallback_config_rejected(kwargs) -> None:
    with pytest.raises(ValidationError):
        _config(**kwargs)


def test_constructor_guard_uses_effective_strong_threshold() -> None:
    client = QdrantClient(location=":memory:")
    encoder = _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。")],
    )
    with pytest.raises(ValueError, match="rerank_fallback_min_score"):
        HybridRetriever(
            client=client,
            embedder=FakeEmbedder(dimension=64),
            sparse_encoder=encoder,
            reranker=TextScoreReranker({"奖学金申请条件。": 0.90}),
            config=_config(min_rerank_score=0.85, fallback_min=0.80),
            min_rerank_score=0.80,
        )


def test_nonfinite_reranker_scores_fail_closed() -> None:
    client = QdrantClient(location=":memory:")
    encoder = _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。")],
    )
    retriever = _retriever(
        client,
        encoder,
        reranker=TextScoreReranker({"奖学金申请条件。": float("nan")}),
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    )

    with pytest.raises(RetrievalError, match="非有限分数"):
        retriever.search_articles("奖学金申请条件")


def test_reranker_unavailable_with_fallback_enabled_fails_closed() -> None:
    client = QdrantClient(location=":memory:")
    encoder = _build(
        client,
        [_article("a@v2021#第一条", doc_id="a", text="奖学金申请条件。")],
    )
    retriever = _retriever(
        client,
        encoder,
        reranker=BrokenReranker(),
        strong=0.85,
        margin=0.0,
        fallback_min=0.80,
    )

    with pytest.raises(RerankUnavailable):
        retriever.search_articles("奖学金申请条件")
