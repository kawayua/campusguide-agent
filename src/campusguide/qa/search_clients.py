"""本地 SearchClient 适配器（评测/离线组装使用）。

生产应用只使用 ``MCPPolicySearchClient``；这些类让 W3/W4 评测脚本可以在
不启动 MCP 子进程的情况下复用同一 SearchClient 契约。
"""

from __future__ import annotations

from typing import Any

from campusguide.domain.models import ArticlePublic
from campusguide.mcp.client import SearchResponse
from campusguide.mcp.schemas import (
    GetArticleData,
    SearchArticlesData,
    SearchArticlesRequest,
    SearchHit,
    policy_filters_from_request,
)
from campusguide.retrieval.hybrid import HybridRetriever


class LocalHybridSearchClient:
    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever

    @property
    def index_version(self) -> str:
        return self._retriever.index_version

    def warmup(self) -> None:
        self._retriever.warmup()

    def search_articles(
        self,
        request: SearchArticlesRequest,
        *,
        timeout: float | None = None,
    ) -> SearchResponse:
        filters = policy_filters_from_request(request)
        result = self._retriever.search_articles(
            request.query,
            filters=filters,
            top_k=request.top_k,
            rerank_top_n=request.rerank_top_n,
        )
        hits = [
            SearchHit(
                article_id=item.article.article_id,
                part_no=item.article.part_no,
                block_type=item.article.block_type.value,
                article=ArticlePublic.from_article(item.article),
                dense_score=item.dense_score,
                sparse_score=item.sparse_score,
                rrf_score=item.rrf_score,
                rerank_score=item.rerank_score,
                matched_spans=list(item.matched_spans),
                retrieval_query=item.retrieval_query,
                rank=item.rank,
                structured=item.structured,
                structured_rank=item.structured_rank,
            )
            for item in result.evidence
        ]
        data = SearchArticlesData(
            query=request.query,
            queries=list(result.queries),
            articles=hits,
            index_version=result.index_version,
            degraded=result.degraded,
            candidates=result.candidates,
            applied_filters=result.applied_filters,
            warnings=list(result.warnings),
            timing=dict(result.timing),
        )
        return SearchResponse(
            data=data,
            server="local-hybrid",
            latency_ms=result.latency_ms,
        )

    def get_article(self, article_id: str, part_no: int | None = None) -> GetArticleData:
        article = self._retriever.get_article(article_id, part_no)
        if article is None:
            raise LookupError(article_id)
        parent = None
        if article.parent_article_id and article.parent_article_id != article.article_id:
            parent_article = self._retriever.get_article(article.parent_article_id)
            if parent_article is not None:
                parent = ArticlePublic.from_article(parent_article)
        return GetArticleData(
            article=ArticlePublic.from_article(article),
            parent=parent,
            metadata={
                "article_id": article.article_id,
                "part_no": article.part_no,
                "block_kind": article.block_kind,
                "parent_article_id": article.parent_article_id,
                "table_id": article.table_id,
                "row_id": article.row_id,
                "row_index": article.row_index,
                "source_cell_ids": list(article.source_cell_ids),
            },
        )

    def close(self) -> None:
        return None


class UnavailableSearchClient:
    """模拟 MCP 不可用，用于启动失败时的明确拒答。"""

    def __init__(self, message: str) -> None:
        self.message = message

    def warmup(self) -> None:
        return None

    def search_articles(
        self,
        request: SearchArticlesRequest,
        *,
        timeout: float | None = None,
    ) -> SearchResponse:
        from campusguide.mcp.schemas import ErrorCode, ToolError

        raise ToolError(ErrorCode.TOOL_UNAVAILABLE, self.message)

    def get_article(self, article_id: str, part_no: int | None = None) -> Any:
        return None
