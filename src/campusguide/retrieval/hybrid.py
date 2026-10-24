"""W4 混合检索：dense + BM25 sparse -> RRF -> 聚合 -> bge-reranker。

流程与设计文档 5.3 一致：

1. 查询变体最多 2 个；每个变体分别取 dense/sparse top_k；
2. RRF(k=60) 融合，按 ``(article_id, part_no)`` 去重；
3. 按 ``article_id`` 聚合，保留最高 RRF 片段；
4. 对聚合后的候选做 bge-reranker，保留 top_n；
5. ``min_rerank_score`` + margin + token budget 截断。

W4 默认失败关闭：reranker 不可用时抛 ``RerankUnavailable``，不返回
未重排结果。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, SparseVector

from campusguide.config import RetrievalConfig
from campusguide.domain.enums import Audience, VersionMode
from campusguide.domain.models import Article, ArticlePublic, Evidence, PolicyFilters
from campusguide.retrieval.embedder import EmbeddingProvider
from campusguide.retrieval.filters import (
    article_matches_profile,
    build_qdrant_filter,
    effective_statuses,
    filter_explanations,
    profile_undetermined_warnings,
)
from campusguide.retrieval.normalize import expand_query
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.rerank import Reranker
from campusguide.retrieval.service import RetrievalError, article_from_payload
from campusguide.retrieval.sparse import SparseEncoder, tokenize


class VersionConflictError(RetrievalError):
    """同一文档的多个 active 版本共享生效日。"""


@dataclass
class SearchResult:
    evidence: list[Evidence]
    queries: list[str]
    candidates: int = 0
    index_version: str = ""
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    applied_filters: list[str] = field(default_factory=list)
    timing: dict[str, float] = field(default_factory=dict)
    stats: list[dict[str, Any]] = field(default_factory=list)

    @property
    def latency_ms(self) -> int:
        return int(sum(self.timing.values()))


@dataclass
class _Candidate:
    article: Article
    rrf_score: float = 0.0
    dense_score: float | None = None
    sparse_score: float | None = None
    retrieval_query: str = ""
    ranks: list[int] = field(default_factory=list)
    rerank_score: float | None = None


def _passage(article: Article, *, max_chars: int = 1200) -> str:
    chapter = " / ".join(article.chapter_path) if article.chapter_path else "无"
    text = article.text if len(article.text) <= max_chars else article.text[:max_chars]
    return f"标题：{article.doc_title}\n章节：{chapter}\n位置：{article.article_no}\n正文：{text}"


def _matched_spans(text: str, query: str, *, limit: int = 5) -> list[tuple[int, int]]:
    terms = {
        token
        for token in tokenize(query)
        if len(token) >= 2 and any("\u3400" <= char <= "\u9fff" for char in token)
    }
    spans: list[tuple[int, int]] = []
    for term in sorted(terms):
        start = 0
        while len(spans) < limit:
            index = text.find(term, start)
            if index < 0:
                break
            spans.append((index, index + len(term)))
            start = index + len(term)
            if len(spans) >= limit:
                break
        if len(spans) >= limit:
            break
    return sorted(spans)


def _within_effective(article: Article, as_of: date) -> bool:
    if article.effective_from > as_of:
        return False
    return article.effective_to is None or as_of <= article.effective_to


class HybridRetriever:
    def __init__(
        self,
        *,
        client: QdrantClient,
        embedder: EmbeddingProvider,
        sparse_encoder: SparseEncoder,
        reranker: Reranker | None,
        config: RetrievalConfig,
        index_version: str = "",
        top_k: int = 30,
        rerank_candidates: int = 20,
        rerank_top_n: int = 8,
        max_articles: int = 8,
        max_chars_per_evidence: int = 800,
        token_budget: int | None = None,
        min_rerank_score: float | None = None,
        min_score_margin: float | None = None,
        rrf_k: int = 60,
        aliases: dict[str, list[str]] | None = None,
        profile_lexicon: ProfileLexicon | None = None,
        max_query_variants: int = 2,
        use_rerank: bool = True,
    ) -> None:
        if not 1 <= rerank_candidates <= 50:
            raise ValueError("rerank_candidates 必须在 1..50")
        self._client = client
        self._embedder = embedder
        self._sparse = sparse_encoder
        self._reranker = reranker
        self._config = config
        self._index_version = index_version
        self._top_k = top_k
        self._rerank_candidates = rerank_candidates
        self._rerank_top_n = rerank_top_n
        self._max_articles = max_articles
        self._max_chars_per_evidence = max_chars_per_evidence
        self._token_budget = token_budget if token_budget is not None else config.token_budget
        self._min_rerank_score = (
            min_rerank_score if min_rerank_score is not None else config.min_rerank_score
        )
        self._min_score_margin = (
            min_score_margin if min_score_margin is not None else config.min_score_margin
        )
        self._rrf_k = rrf_k
        self._aliases = aliases or {}
        self._profile_lexicon = profile_lexicon or ProfileLexicon({}, {})
        self._max_query_variants = max_query_variants
        self._use_rerank = use_rerank

    @property
    def index_version(self) -> str:
        return self._index_version

    def _expand_queries(self, query: str, filters: PolicyFilters) -> list[str]:
        base = expand_query(query, self._aliases, max_variants=self._max_query_variants)
        if not base:
            return []
        profile_variants = self._profile_lexicon.query_variants(
            query,
            filters.profile,
            max_variants=self._max_query_variants,
        )
        combined: list[str] = []
        for candidate in [base[0], *profile_variants, *base[1:]]:
            if candidate not in combined:
                combined.append(candidate)
        return combined[: self._max_query_variants]

    def warmup(self) -> None:
        self._embedder.embed_query("CampusGuide 预热")
        if self._use_rerank:
            if self._reranker is None:
                raise RetrievalError("未配置 reranker，无法在 W4 默认模式下检索")
            self._reranker.warmup()

    def _search_dense(self, vector: list[float], query_filter: Filter, limit: int) -> list[Any]:
        try:
            response = self._client.query_points(
                collection_name=self._config.collection,
                query=vector,
                using=self._config.dense_vector_name,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise RetrievalError(f"Qdrant dense 检索失败：{exc}") from exc
        return list(response.points)

    def _search_sparse(
        self,
        vector: SparseVector,
        query_filter: Filter,
        limit: int,
    ) -> list[Any]:
        if not vector.indices:
            return []
        try:
            response = self._client.query_points(
                collection_name=self._config.collection,
                query=vector,
                using=self._config.sparse_vector_name,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise RetrievalError(f"Qdrant sparse 检索失败：{exc}") from exc
        return list(response.points)

    @staticmethod
    def _accumulate(
        candidates: dict[tuple[str, int], _Candidate],
        points: list[Any],
        *,
        kind: str,
        query: str,
        rrf_k: int,
    ) -> None:
        for rank, point in enumerate(points, start=1):
            payload = point.payload or {}
            try:
                article = article_from_payload(payload)
            except RetrievalError:
                continue
            key = (article.article_id, article.part_no)
            candidate = candidates.get(key)
            if candidate is None:
                candidate = _Candidate(article=article, retrieval_query=query)
                candidates[key] = candidate
            candidate.rrf_score += 1.0 / (rrf_k + rank)
            candidate.ranks.append(rank)
            score = float(point.score)
            if kind == "dense":
                if candidate.dense_score is None or score > candidate.dense_score:
                    candidate.dense_score = score
            elif candidate.sparse_score is None or score > candidate.sparse_score:
                candidate.sparse_score = score

    def _post_filter(
        self,
        candidates: dict[tuple[str, int], _Candidate],
        filters: PolicyFilters,
    ) -> tuple[dict[tuple[str, int], _Candidate], list[str]]:
        warnings: list[str] = []
        kept: dict[tuple[str, int], _Candidate] = {}
        statuses = effective_statuses(filters)
        for key, candidate in candidates.items():
            article = candidate.article
            if article.status.value == "draft":
                warnings.append(f"DROP_DRAFT: {article.article_id}")
                continue
            if statuses and article.status not in statuses:
                warnings.append(
                    f"STATUS_MISMATCH: {article.article_id} status={article.status.value}"
                )
                continue
            if not filters.ignore_effective_dates and not _within_effective(article, filters.as_of):
                warnings.append(f"VERSION_MISMATCH: {article.article_id}")
                continue
            if filters.audience:
                audiences = set(filters.audience)
                if (
                    Audience.ALL not in audiences
                    and article.audience
                    and (
                        Audience.ALL not in article.audience
                        and not set(article.audience) & audiences
                    )
                ):
                    warnings.append(f"AUDIENCE_MISMATCH: {article.article_id}")
                    continue
            matches, reasons = article_matches_profile(
                article,
                filters.profile,
                as_of=filters.as_of,
            )
            if not matches:
                warnings.append(f"PROFILE_MISMATCH: {article.article_id} " + "；".join(reasons))
                continue
            kept[key] = candidate
        return kept, warnings

    def _aggregate_and_check_versions(
        self,
        candidates: dict[tuple[str, int], _Candidate],
        filters: PolicyFilters,
    ) -> tuple[list[_Candidate], list[str]]:
        by_article: dict[str, _Candidate] = {}
        for candidate in candidates.values():
            existing = by_article.get(candidate.article.article_id)
            if existing is None or candidate.rrf_score > existing.rrf_score:
                by_article[candidate.article.article_id] = candidate

        warnings: list[str] = []
        by_doc: dict[str, list[_Candidate]] = {}
        for candidate in by_article.values():
            by_doc.setdefault(candidate.article.doc_id, []).append(candidate)

        selected: list[_Candidate] = []
        for doc_id, items in by_doc.items():
            versions_by_date: dict[date, set[str]] = {}
            for item in items:
                versions_by_date.setdefault(item.article.effective_from, set()).add(
                    item.article.version_id
                )
            conflicted = [
                moment for moment, versions in versions_by_date.items() if len(versions) > 1
            ]
            if conflicted:
                raise VersionConflictError(
                    f"{doc_id} 在 {conflicted[0].isoformat()} 有多个 active 版本"
                )
            version_dates = {item.article.version_id: item.article.effective_from for item in items}
            version_ids = sorted(version_dates)
            if len(version_ids) > 1:
                warnings.append(
                    f"MULTIPLE_VERSIONS: {doc_id} 返回 {len(version_ids)} 个届别版本，"
                    "按最新生效日保留"
                )
            latest_from = max(version_dates.values())
            # 只裁掉旧版本，保留最新版本下的全部条款；不能按 doc_id 只留一条，
            # 否则多条款问题（如“条件 + 标准”）会丢掉其他 article。
            selected.extend(
                item for item in items if version_dates[item.article.version_id] == latest_from
            )
        return selected, warnings

    def search_articles(
        self,
        query: str,
        *,
        filters: PolicyFilters | None = None,
        top_k: int | None = None,
        rerank_top_n: int | None = None,
    ) -> SearchResult:
        filters = filters or PolicyFilters()
        top_k = top_k if top_k is not None else self._top_k
        rerank_top_n = rerank_top_n if rerank_top_n is not None else self._rerank_top_n
        if not 1 <= top_k <= 50:
            raise ValueError("top_k 必须在 1..50")
        if not 1 <= rerank_top_n <= 20:
            raise ValueError("rerank_top_n 必须在 1..20")

        queries = self._expand_queries(query, filters)
        applied = filter_explanations(filters)
        if not queries:
            return SearchResult(
                evidence=[],
                queries=[],
                index_version=self._index_version,
                applied_filters=applied,
            )

        query_filter = build_qdrant_filter(filters)
        candidates: dict[tuple[str, int], _Candidate] = {}
        candidate_count = 0
        dense_ms = 0.0
        sparse_ms = 0.0
        for retrieval_query in queries:
            started = time.perf_counter()
            dense_vector = self._embedder.embed_query(retrieval_query)
            points = self._search_dense(dense_vector, query_filter, top_k)
            dense_ms += (time.perf_counter() - started) * 1000
            candidate_count += len(points)
            self._accumulate(
                candidates,
                points,
                kind="dense",
                query=retrieval_query,
                rrf_k=self._rrf_k,
            )

            started = time.perf_counter()
            sparse_vector = self._sparse.encode_query(retrieval_query)
            sparse_points = self._search_sparse(sparse_vector, query_filter, top_k)
            sparse_ms += (time.perf_counter() - started) * 1000
            candidate_count += len(sparse_points)
            self._accumulate(
                candidates,
                sparse_points,
                kind="sparse",
                query=retrieval_query,
                rrf_k=self._rrf_k,
            )

        candidates, warnings = self._post_filter(candidates, filters)
        aggregated, version_warnings = self._aggregate_and_check_versions(candidates, filters)
        warnings.extend(version_warnings)
        aggregated.sort(key=lambda item: item.rrf_score, reverse=True)
        candidate_limit = min(top_k, self._rerank_candidates) if self._use_rerank else top_k
        shortlist = aggregated[:candidate_limit]

        rerank_ms = 0.0
        if self._use_rerank and shortlist:
            if self._reranker is None:
                raise RetrievalError("未配置 reranker；W4 默认失败关闭")
            started = time.perf_counter()
            scores = self._reranker.score(
                queries[0],
                [_passage(candidate.article) for candidate in shortlist],
            )
            rerank_ms = (time.perf_counter() - started) * 1000
            if len(scores) != len(shortlist):
                raise RetrievalError("reranker 返回分数数量与候选数不一致")
            for candidate, score in zip(shortlist, scores, strict=True):
                candidate.rerank_score = float(score)
            shortlist.sort(key=lambda item: item.rerank_score or 0.0, reverse=True)
            top_score = shortlist[0].rerank_score or 0.0
            if top_score < self._min_rerank_score + self._min_score_margin:
                warnings.append(
                    f"RERANK_BELOW_MARGIN: top={top_score:.4f} < "
                    f"{self._min_rerank_score + self._min_score_margin:.4f}"
                )
                shortlist = []
            else:
                shortlist = [
                    item
                    for item in shortlist
                    if (item.rerank_score or 0.0) >= self._min_rerank_score
                ]

        kept: list[Evidence] = []
        used_chars = 0
        for candidate in shortlist:
            if len(kept) >= min(self._max_articles, rerank_top_n):
                break
            article = candidate.article
            if len(article.text) > self._max_chars_per_evidence:
                article = article.model_copy(
                    update={"text": article.text[: self._max_chars_per_evidence]}
                )
            cost = len(article.text) + 80
            if kept and used_chars + cost > self._token_budget:
                warnings.append(f"TOKEN_BUDGET: 跳过 {article.article_id}")
                continue
            used_chars += cost
            evidence = Evidence(
                article=article,
                dense_score=candidate.dense_score,
                sparse_score=candidate.sparse_score,
                rrf_score=candidate.rrf_score,
                rerank_score=candidate.rerank_score,
                retrieval_query=candidate.retrieval_query,
                matched_spans=_matched_spans(article.text, queries[0]),
            )
            kept.append(evidence)

        for rank, evidence in enumerate(kept, start=1):
            evidence.rank = rank

        if filters.profile is not None or not filters.audience:
            warnings.extend(
                profile_undetermined_warnings(
                    [item.article for item in kept],
                    filters.profile,
                    audience=filters.audience,
                )
            )
        warnings.append("FILTERS_APPLIED: " + "；".join(applied))
        if filters.version_mode is VersionMode.INCLUDE_REPEALED:
            for evidence in kept:
                if evidence.article.status.value == "repealed":
                    warnings.append(f"REPEALED_EVIDENCE: {evidence.article.article_id}")

        return SearchResult(
            evidence=kept,
            queries=queries,
            candidates=candidate_count,
            index_version=self._index_version,
            warnings=warnings,
            applied_filters=applied,
            timing={
                "dense_ms": round(dense_ms, 2),
                "sparse_ms": round(sparse_ms, 2),
                "rerank_ms": round(rerank_ms, 2),
            },
            stats=[
                {
                    "article_id": item.article.article_id,
                    "part_no": item.article.part_no,
                    "dense_score": item.dense_score,
                    "sparse_score": item.sparse_score,
                    "rerank_score": item.rerank_score,
                }
                for item in kept
            ],
        )

    def get_article(self, article_id: str, part_no: int | None = None) -> Article | None:
        conditions: list[Any] = [
            FieldCondition(key="article_id", match=MatchValue(value=article_id))
        ]
        if part_no is not None:
            conditions.append(FieldCondition(key="part_no", match=MatchValue(value=part_no)))
        try:
            points, _ = self._client.scroll(
                collection_name=self._config.collection,
                scroll_filter=Filter(must=conditions),
                limit=10,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise RetrievalError(f"Qdrant scroll 失败：{exc}") from exc
        if not points:
            return None
        points.sort(key=lambda point: int((point.payload or {}).get("part_no", 1)))
        return article_from_payload(points[0].payload or {})

    @staticmethod
    def public_article(article: Article) -> ArticlePublic:
        return ArticlePublic.from_article(article)
