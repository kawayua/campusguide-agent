"""policy-search MCP server：只读条款检索。

server 进程内持有 Qdrant / bge-m3 / BM25 sparse / bge-reranker；应用侧只
通过 MCP 调用。所有日志写 stderr，stdout 留给 MCP JSON-RPC。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastmcp import FastMCP
from qdrant_client import QdrantClient

from campusguide.config import (
    OrganizationConfig,
    RetrievalConfig,
    load_organization,
    load_retrieval_config,
)
from campusguide.domain.models import Article, ArticlePublic, PolicyFilters
from campusguide.env import load_dotenv
from campusguide.mcp.schemas import (
    ErrorCode,
    GetArticleData,
    SearchArticlesData,
    SearchArticlesRequest,
    SearchHit,
    ToolError,
    policy_filters_from_request,
    success,
)
from campusguide.retrieval.embedder import BgeM3Embedder
from campusguide.retrieval.hybrid import HybridRetriever, SearchResult, VersionConflictError
from campusguide.retrieval.normalize import load_aliases
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.rerank import BgeReranker, RerankUnavailable
from campusguide.retrieval.service import RetrievalError
from campusguide.retrieval.sparse import BM25SparseEncoder

logger = logging.getLogger("campusguide.mcp.policy_search")

ROOT = Path(os.getenv("CAMPUSGUIDE_ROOT", Path(__file__).resolve().parents[3]))


class SearchBackend(Protocol):
    @property
    def index_version(self) -> str: ...

    def search_articles(
        self,
        query: str,
        *,
        filters: PolicyFilters,
        top_k: int,
        rerank_top_n: int,
    ) -> SearchResult: ...

    def get_article(self, article_id: str, part_no: int | None = None) -> Article | None: ...


@dataclass
class SearchRegistries:
    doc_ids: set[str]
    flow_ids: set[str]
    organization: OrganizationConfig | None = None


def _load_doc_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            doc_id = row.get("doc_id")
            if isinstance(doc_id, str):
                ids.add(doc_id)
    return ids


def _load_flow_ids(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    import yaml

    flow_ids: set[str] = set()
    for path in directory.glob("*.yaml"):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("flow_id"), str):
            flow_ids.add(payload["flow_id"])
    return flow_ids


class PolicySearchService:
    """参数校验 + backend 调用 + 信封映射。"""

    def __init__(
        self,
        backend: SearchBackend,
        *,
        registries: SearchRegistries | None = None,
    ) -> None:
        self._backend = backend
        self._registries = registries or SearchRegistries(doc_ids=set(), flow_ids=set())

    def warmup(self) -> None:
        warmup = getattr(self._backend, "warmup", None)
        if warmup is not None:
            warmup()

    def _build_filters(self, request: SearchArticlesRequest) -> PolicyFilters:
        return policy_filters_from_request(
            request,
            known_doc_ids=self._registries.doc_ids,
            known_flow_ids=self._registries.flow_ids,
            organization=self._registries.organization,
        )

    def search_articles(self, request: SearchArticlesRequest) -> dict[str, Any]:
        filters = self._build_filters(request)
        try:
            result = self._backend.search_articles(
                request.query,
                filters=filters,
                top_k=request.top_k,
                rerank_top_n=request.rerank_top_n,
            )
        except ToolError:
            raise
        except VersionConflictError as exc:
            raise ToolError(ErrorCode.CONFLICT, str(exc)) from exc
        except RerankUnavailable as exc:
            raise ToolError(ErrorCode.RERANK_UNAVAILABLE, str(exc)) from exc
        except (RetrievalError, FileNotFoundError, FileExistsError) as exc:
            raise ToolError(ErrorCode.INDEX_UNAVAILABLE, str(exc)) from exc
        except Exception as exc:
            logger.exception("search_articles failed")
            raise ToolError(
                ErrorCode.INTERNAL,
                f"检索内部错误：{type(exc).__name__}",
            ) from exc

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
            )
            for item in result.evidence
        ]
        data = SearchArticlesData(
            query=request.query,
            queries=result.queries,
            articles=hits,
            index_version=result.index_version,
            degraded=result.degraded,
            candidates=result.candidates,
            applied_filters=result.applied_filters,
            warnings=result.warnings,
            timing=result.timing,
        )
        return success(data.model_dump(mode="json"), warnings=result.warnings)

    def get_article(self, article_id: str, part_no: int | None = None) -> dict[str, Any]:
        if not article_id.strip():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "article_id 不能为空")
        if part_no is not None and part_no < 1:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "part_no 必须 >= 1")
        try:
            article = self._backend.get_article(article_id, part_no)
        except Exception as exc:
            raise ToolError(ErrorCode.INDEX_UNAVAILABLE, str(exc)) from exc
        if article is None:
            raise ToolError(
                ErrorCode.NOT_FOUND,
                f"未找到 article_id={article_id!r} part_no={part_no}",
            )
        data = GetArticleData(
            article=ArticlePublic.from_article(article),
            metadata={
                "article_id": article.article_id,
                "doc_id": article.doc_id,
                "version_id": article.version_id,
                "part_no": article.part_no,
                "block_type": article.block_type.value,
                "status": article.status.value,
                "effective_from": article.effective_from.isoformat(),
                "effective_to": (
                    article.effective_to.isoformat() if article.effective_to else None
                ),
                "date_confidence": article.date_confidence,
                "source_url": str(article.source_url),
            },
        )
        return success(data.model_dump(mode="json"))


def build_search_server(service: PolicySearchService) -> FastMCP:
    mcp = FastMCP("policy-search")

    @mcp.tool(name="search_articles")
    def search_articles(request: SearchArticlesRequest) -> dict[str, Any]:
        """按问题检索条款；返回 dense/sparse/RRF/rerank 分数与 index_version。"""
        try:
            return service.search_articles(request)
        except ToolError as exc:
            return exc.envelope()

    @mcp.tool(name="get_article")
    def get_article(article_id: str, part_no: int | None = None) -> dict[str, Any]:
        """按 article_id（可选 part_no）读取单条公开条款。"""
        try:
            return service.get_article(article_id, part_no)
        except ToolError as exc:
            return exc.envelope()

    return mcp


def create_qdrant_client() -> QdrantClient:
    location = os.getenv("QDRANT_LOCATION", "").strip()
    if location:
        Path(location).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=location)
    return QdrantClient(
        url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
        timeout=10,
    )


def create_service(
    *,
    root: Path = ROOT,
) -> PolicySearchService:
    load_dotenv(root / ".env")
    retrieval: RetrievalConfig = load_retrieval_config(root / "configs" / "retrieval.yaml")
    manifest_path = root / "data" / "vector_manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    index_version = str(manifest.get("index_version", ""))
    sparse_info = manifest.get("sparse") or {}
    sparse_file = sparse_info.get("vocab_file") or str(root / "data" / "sparse_vocab.json")
    sparse_path = Path(sparse_file)
    if not sparse_path.is_absolute():
        sparse_path = root / sparse_path

    client = create_qdrant_client()
    if manifest:
        from campusguide.retrieval.indexer import verify_vector_manifest

        problems = verify_vector_manifest(client=client, manifest=manifest)
        if problems:
            raise RetrievalError(
                "Qdrant 索引与 vector manifest 不一致，请重新运行 build_vectors.py："
                + "；".join(problems)
            )
    embedder = BgeM3Embedder(
        retrieval.embedding_model or os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        device=os.getenv("EMBEDDING_DEVICE", "auto"),
    )
    sparse_encoder = BM25SparseEncoder.load(sparse_path)
    reranker = BgeReranker(
        os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        device=os.getenv("RERANKER_DEVICE", os.getenv("EMBEDDING_DEVICE", "auto")),
    )
    aliases_path = root / "configs" / "aliases.yaml"
    aliases = load_aliases(aliases_path) if aliases_path.exists() else {}
    organization = load_organization(root / "configs" / "organization.yaml")
    profile_lexicon = ProfileLexicon.from_organization(organization)

    retriever = HybridRetriever(
        client=client,
        embedder=embedder,
        sparse_encoder=sparse_encoder,
        reranker=reranker,
        config=retrieval,
        index_version=index_version,
        top_k=retrieval.recall_top_k,
        rerank_candidates=retrieval.rerank_candidates,
        rerank_top_n=retrieval.rerank_top_n,
        max_articles=retrieval.max_articles,
        token_budget=retrieval.token_budget,
        min_rerank_score=retrieval.min_rerank_score,
        min_score_margin=retrieval.min_score_margin,
        aliases=aliases,
        profile_lexicon=profile_lexicon,
    )
    registries = SearchRegistries(
        doc_ids=_load_doc_ids(root / "data" / "processed" / "documents.jsonl"),
        flow_ids=_load_flow_ids(root / "configs" / "flows"),
        organization=organization,
    )
    return PolicySearchService(retriever, registries=registries)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = create_service()
    try:
        service.warmup()
    except RerankUnavailable as exc:
        logger.error("reranker 预热失败，检索将返回 RERANK_UNAVAILABLE：%s", exc)
    except Exception:
        logger.exception("检索后端预热失败；工具调用将返回 INDEX_UNAVAILABLE")
    mcp = build_search_server(service)
    logger.info("policy-search MCP server starting (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
