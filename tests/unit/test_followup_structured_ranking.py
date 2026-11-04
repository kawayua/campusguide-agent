"""Follow-up 2026-10-30: structural priority is a signal, not a rerank score."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.enums import BlockType
from campusguide.retrieval.corpus_views import CorpusViews
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.hybrid import HybridRetriever, _passage
from campusguide.retrieval.indexer import build_vector_index, embedding_text
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.section_select import select_sections
from campusguide.retrieval.sparse import BM25SparseEncoder
from tests.w2_helpers import make_article


class FixedReranker:
    def __init__(self, mapping: dict[str, float]) -> None:
        self.mapping = mapping

    @property
    def model_name(self) -> str:
        return "fixed-reranker"

    @property
    def device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        return None

    def score(self, query: str, passages: list[str]) -> list[float]:
        return [self.mapping.get(passage, 0.5) for passage in passages]


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        collection="followup_structured",
        alias="followup_structured_current",
        embedding_model="fake",
        dense_vector_size=64,
        token_budget=4000,
    )


def _retriever(
    client: QdrantClient,
    encoder: BM25SparseEncoder,
    *,
    reranker: FixedReranker,
    corpus_views: CorpusViews | None = None,
    min_rerank_score: float = 0.3,
    min_score_margin: float = 0.1,
) -> HybridRetriever:
    return HybridRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        sparse_encoder=encoder,
        reranker=reranker,
        config=_config(),
        index_version="followup-test",
        top_k=20,
        rerank_top_n=8,
        max_articles=8,
        min_rerank_score=min_rerank_score,
        min_score_margin=min_score_margin,
        profile_lexicon=ProfileLexicon({}, {}),
        corpus_views=corpus_views,
    )


def _teacher() -> object:
    return make_article(
        article_id="teacher@v2026#教师名单",
        doc_id="teacher",
        version_id="v2026",
        doc_title="计算机科学与工程系教师名单",
        article_no="教师名单",
        text="教授 张三",
    )


def _index(client: QdrantClient, articles: list) -> BM25SparseEncoder:
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


def test_structured_match_keeps_real_score_and_is_audited() -> None:
    client = QdrantClient(location=":memory:")
    teacher = _teacher()
    encoder = _index(client, [teacher])
    views = CorpusViews([teacher], ProfileLexicon({}, {}))
    retriever = _retriever(
        client,
        encoder,
        reranker=FixedReranker({_passage(teacher): 0.10}),
        corpus_views=views,
    )
    result = retriever.search_articles("张三老师属于哪个系？")
    assert [item.article.article_id for item in result.evidence] == [teacher.article_id]
    # 真实模型分数保留，不再被结构匹配抬到 0.98。
    assert result.evidence[0].rerank_score == pytest.approx(0.10)
    assert any(w.startswith("STRUCTURED_MATCH:") for w in result.warnings)
    assert any(
        w.startswith("STRUCTURED_THRESHOLD_BYPASS:") and "score=0.1000" in w
        for w in result.warnings
    )
    assert result.stats[0]["structured"] is True
    assert result.stats[0]["structured_rank"] == 1


def test_semantic_threshold_still_drops_unstructured_low_scores() -> None:
    client = QdrantClient(location=":memory:")
    article = make_article(
        article_id="aid@v2021#第一条",
        doc_id="aid",
        text="国家奖学金申请条件包括学习成绩优异。",
    )
    encoder = _index(client, [article])
    retriever = _retriever(
        client,
        encoder,
        reranker=FixedReranker({_passage(article): 0.10}),
    )
    result = retriever.search_articles("国家奖学金申请条件")
    assert result.evidence == []
    assert any(w.startswith("RERANK_BELOW_MARGIN:") for w in result.warnings)
    # 阈值没有被全局降低：非结构候选仍被丢弃。
    assert not any(w.startswith("STRUCTURED_THRESHOLD_BYPASS:") for w in result.warnings)


def test_structural_hit_is_ordered_before_higher_model_score() -> None:
    client = QdrantClient(location=":memory:")
    teacher = _teacher()
    generic = make_article(
        article_id="notice@v2026#教师名单说明",
        doc_id="notice",
        version_id="v2026",
        doc_title="教师名单说明",
        article_no="教师名单说明",
        text="教师名单按规定公示。",
    )
    encoder = _index(client, [teacher, generic])
    views = CorpusViews([teacher, generic], ProfileLexicon({}, {}))
    retriever = _retriever(
        client,
        encoder,
        reranker=FixedReranker({_passage(teacher): 0.10, _passage(generic): 0.90}),
        corpus_views=views,
        min_rerank_score=0.0,
        min_score_margin=0.0,
    )
    result = retriever.search_articles("张三老师属于哪个系？")
    ids = [item.article.article_id for item in result.evidence]
    assert ids[0] == teacher.article_id
    assert ids.index(generic.article_id) > 0
    assert result.evidence[0].rerank_score == pytest.approx(0.10)
    assert result.evidence[1].rerank_score == pytest.approx(0.90)
    # 独立信号不改变普通候选的原始分数与分数排序。
    assert result.stats[0]["structured"] is True
    assert result.stats[1]["structured"] is False


@dataclass
class SectionCandidate:
    article: object
    rerank_score: float | None
    structured: bool = False
    structured_rank: int | None = None


def _table(article_id: str, no: str) -> SectionCandidate:
    article = make_article(
        article_id=article_id,
        article_no=no,
        block_type=BlockType.SECTION,
        text="课程代码 | 课程名称 | 学分 | 学时\n601001 | 高等数学 | 4 | 64",
    )
    return SectionCandidate(article=article, rerank_score=0.90)


def test_structured_table_section_survives_table_cap() -> None:
    structured = _table("plan@v2024#十、进程表", "十、进程表")
    structured.rerank_score = 0.20
    structured.structured = True
    structured.structured_rank = 1
    ordinary = _table("plan@v2024#八、课程结构", "八、课程结构")
    selection = select_sections(
        [ordinary, structured],
        query="学院概况",
        limit=2,
        max_table_sections=0,
    )
    assert [item.article.article_id for item in selection.candidates] == [
        "plan@v2024#十、进程表"
    ]
    assert any(w.startswith("STRUCTURED_TABLE_KEPT:") for w in selection.warnings)
    assert any(w.startswith("TABLE_SECTION_DEPRIORITIZED:") for w in selection.warnings)
