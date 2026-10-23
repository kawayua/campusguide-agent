"""Dense 检索服务：查询变体 -> Qdrant -> 去重聚合 -> Evidence。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.enums import BlockType
from campusguide.domain.models import Article, Evidence, PolicyFilters
from campusguide.retrieval.embedder import EmbeddingProvider
from campusguide.retrieval.filters import build_qdrant_filter
from campusguide.retrieval.normalize import expand_query


class RetrievalError(RuntimeError):
    """检索后端不可用或返回不可解析结果。"""


@dataclass
class RetrievalResult:
    evidence: list[Evidence]
    queries: list[str]
    candidates: int = 0
    truncated_by_budget: int = 0
    notes: list[str] = field(default_factory=list)


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise RetrievalError(f"payload 日期不合法：{value!r}") from exc


def article_from_payload(payload: dict[str, Any]) -> Article:
    """从 Qdrant payload 还原最小 Article；不依赖本地 JSONL。"""
    data = dict(payload)
    data.pop("audience_colleges", None)
    data.pop("audience_majors", None)
    data["effective_from"] = _as_date(data.get("effective_from"))
    data["effective_to"] = _as_date(data.get("effective_to"))
    data["published_at"] = _as_date(data.get("published_at"))
    data["audience_detail"] = None
    try:
        return Article.model_validate(data)
    except Exception as exc:
        raise RetrievalError(f"Qdrant payload 无法还原 Article：{exc}") from exc


class DenseRetriever:
    def __init__(
        self,
        *,
        client: QdrantClient,
        embedder: EmbeddingProvider,
        config: RetrievalConfig,
        top_k: int = 20,
        max_evidence: int = 8,
        max_chars_per_evidence: int = 800,
        min_dense_score: float = 0.35,
        token_budget: int | None = None,
        aliases: dict[str, list[str]] | None = None,
        max_query_variants: int = 2,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._config = config
        self._top_k = top_k
        self._max_evidence = max_evidence
        self._max_chars_per_evidence = max_chars_per_evidence
        self._min_dense_score = min_dense_score
        self._token_budget = token_budget if token_budget is not None else config.token_budget
        self._aliases = aliases or {}
        self._max_query_variants = max_query_variants

    def warmup(self) -> None:
        """预加载 embedding 模型，避免首个请求把冷启动算进问答延迟。"""
        self._embedder.embed_query("CampusGuide 预热")

    def _search(self, vector: list[float], query_filter) -> list[Any]:
        try:
            response = self._client.query_points(
                collection_name=self._config.collection,
                query=vector,
                using=self._config.dense_vector_name,
                query_filter=query_filter,
                limit=self._top_k,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise RetrievalError(f"Qdrant 检索失败：{exc}") from exc
        return list(response.points)

    def retrieve(
        self,
        query: str,
        *,
        filters: PolicyFilters | None = None,
    ) -> RetrievalResult:
        filters = filters or PolicyFilters()
        queries = expand_query(
            query,
            self._aliases,
            max_variants=self._max_query_variants,
        )
        if not queries:
            return RetrievalResult(evidence=[], queries=[])

        query_filter = build_qdrant_filter(filters)
        best_by_fragment: dict[tuple[str, int], Evidence] = {}
        candidate_count = 0

        for retrieval_query in queries:
            vector = self._embedder.embed_query(retrieval_query)
            for point in self._search(vector, query_filter):
                candidate_count += 1
                payload = point.payload or {}
                article = article_from_payload(payload)
                score = float(point.score)
                key = (article.article_id, article.part_no)
                current = best_by_fragment.get(key)
                if current is None or score > current.score:
                    best_by_fragment[key] = Evidence(
                        article=article,
                        dense_score=score,
                        retrieval_query=retrieval_query,
                    )

        notes: list[str] = []
        best_by_article: dict[str, Evidence] = {}
        for evidence in best_by_fragment.values():
            existing = best_by_article.get(evidence.article.article_id)
            if existing is None or evidence.score > existing.score:
                best_by_article[evidence.article.article_id] = evidence

        ranked = sorted(best_by_article.values(), key=lambda item: item.score, reverse=True)
        ranked = [item for item in ranked if item.score >= self._min_dense_score]

        kept: list[Evidence] = []
        used_chars = 0
        truncated = 0
        for evidence in ranked:
            if len(kept) >= self._max_evidence:
                break
            article = evidence.article
            if len(article.text) > self._max_chars_per_evidence:
                article = article.model_copy(
                    update={"text": article.text[: self._max_chars_per_evidence]}
                )
                evidence = evidence.model_copy(update={"article": article})
                truncated += 1
            cost = len(article.text) + 80
            if kept and used_chars + cost > self._token_budget:
                notes.append(
                    f"证据预算触顶：保留 {len(kept)} 条，跳过 {evidence.article.article_id}"
                )
                continue
            used_chars += cost
            kept.append(evidence)

        for rank, evidence in enumerate(kept, start=1):
            evidence.rank = rank
            if evidence.article.block_type is not BlockType.ARTICLE:
                notes.append(
                    f"{evidence.article.article_id} 的块类型为 "
                    f"{evidence.article.block_type.value}，按 section 口径记录"
                )

        return RetrievalResult(
            evidence=kept,
            queries=queries,
            candidates=candidate_count,
            truncated_by_budget=truncated,
            notes=notes,
        )
