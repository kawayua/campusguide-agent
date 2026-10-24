"""QAService 的本地组装：配置、MCP client、provider 与 trace。

生产路径（CLI/UI）通过 stdio 拉起 ``policy-search`` / ``policy-meta``；
``create_qa_service`` 保留为 W2/W3 评测的 dense-only 离线组装，
``create_hybrid_qa_service`` 供 W4 评测在进程内复用混合检索。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import OpenAICompatibleChat
from campusguide.config import (
    MCPConfig,
    RetrievalConfig,
    load_abstention_config,
    load_mcp_config,
    load_retrieval_config,
)
from campusguide.env import load_dotenv
from campusguide.mcp.client import MCPClientConfig, MCPPolicyMetaClient, MCPPolicySearchClient
from campusguide.observability.trace import TraceWriter
from campusguide.qa.service import QAService, QASettings
from campusguide.verification.claims import ClaimValidator
from campusguide.verification.verifier import Verifier

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

    from campusguide.retrieval.embedder import EmbeddingProvider
    from campusguide.retrieval.rerank import Reranker
    from campusguide.retrieval.service import DenseRetriever

logger = logging.getLogger("campusguide.qa.runtime")

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RETRIEVAL_CONFIG = ROOT / "configs" / "retrieval.yaml"
DEFAULT_ABSTENTION_CONFIG = ROOT / "configs" / "abstention.yaml"
DEFAULT_ALIASES = ROOT / "configs" / "aliases.yaml"
DEFAULT_MCP_CONFIG = ROOT / "configs" / "mcp.yaml"
DEFAULT_VECTOR_MANIFEST = ROOT / "data" / "vector_manifest.json"
DEFAULT_TEXT_MANIFEST = ROOT / "data" / "manifest.json"
DEFAULT_SPARSE_VOCAB = ROOT / "data" / "sparse_vocab.json"
DEFAULT_TRACE_DIR = ROOT / "data" / "traces"


class IndexUnavailableError(RuntimeError):
    """索引缺失或与 manifest 不一致。"""


@dataclass
class RuntimePaths:
    retrieval_config: Path = DEFAULT_RETRIEVAL_CONFIG
    abstention_config: Path = DEFAULT_ABSTENTION_CONFIG
    aliases: Path = DEFAULT_ALIASES
    mcp_config: Path = DEFAULT_MCP_CONFIG
    vector_manifest: Path = DEFAULT_VECTOR_MANIFEST
    text_manifest: Path = DEFAULT_TEXT_MANIFEST
    sparse_vocab: Path = DEFAULT_SPARSE_VOCAB
    trace_dir: Path = DEFAULT_TRACE_DIR


def create_qdrant_client() -> QdrantClient:
    from qdrant_client import QdrantClient

    location = os.getenv("QDRANT_LOCATION", "").strip()
    if location:
        Path(location).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=location)
    return QdrantClient(
        url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
        timeout=10,
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise IndexUnavailableError(f"缺少向量 manifest：{path}，请先运行 build_vectors.py")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IndexUnavailableError(f"向量 manifest 不是合法 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise IndexUnavailableError(f"向量 manifest 顶层必须是对象：{path}")
    return payload


def _read_text_manifest_hash(path: Path) -> str:
    if not path.exists():
        raise IndexUnavailableError(f"缺少文本 manifest：{path}，请先运行 build_index.py")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IndexUnavailableError(f"文本 manifest 不是合法 JSON：{path}") from exc
    content_hash = payload.get("content_hash") if isinstance(payload, dict) else None
    if not isinstance(content_hash, str) or not content_hash:
        raise IndexUnavailableError(f"文本 manifest 缺少 content_hash：{path}")
    return content_hash


def verify_or_raise(
    *,
    client: QdrantClient,
    manifest_path: Path,
    text_manifest_path: Path = DEFAULT_TEXT_MANIFEST,
) -> dict:
    from campusguide.retrieval.indexer import read_vector_manifest, verify_vector_manifest

    manifest = read_vector_manifest(manifest_path)
    text_hash = _read_text_manifest_hash(text_manifest_path)
    problems = verify_vector_manifest(
        client=client,
        manifest=manifest,
        source_content_hash=text_hash,
    )
    if problems:
        raise IndexUnavailableError(
            "向量索引与 manifest 不一致，请重新运行 build_vectors.py：" + "；".join(problems)
        )
    return manifest


def build_retriever(
    *,
    client: QdrantClient,
    config: RetrievalConfig,
    embedder: EmbeddingProvider,
    aliases_path: str | Path = DEFAULT_ALIASES,
) -> DenseRetriever:
    from campusguide.retrieval.normalize import load_aliases
    from campusguide.retrieval.service import DenseRetriever

    settings = QASettings.from_config(config)
    return DenseRetriever(
        client=client,
        embedder=embedder,
        config=config,
        top_k=settings.top_k,
        max_evidence=settings.max_evidence,
        max_chars_per_evidence=settings.max_chars_per_evidence,
        min_dense_score=settings.min_dense_score,
        token_budget=settings.token_budget,
        aliases=load_aliases(aliases_path),
    )


def create_qa_service(
    *,
    client: QdrantClient,
    paths: RuntimePaths | None = None,
    embedder: EmbeddingProvider | None = None,
    provider=None,
    verify_index: bool = True,
) -> QAService:
    """W2/W3 dense-only 离线组装；W4 生产入口见 ``create_mcp_qa_service``。"""
    from campusguide.retrieval.embedder import BgeM3Embedder

    paths = paths or RuntimePaths()
    config = load_retrieval_config(paths.retrieval_config)
    abstention = load_abstention_config(paths.abstention_config)
    if verify_index:
        verify_or_raise(
            client=client,
            manifest_path=paths.vector_manifest,
            text_manifest_path=paths.text_manifest,
        )
    embedder = embedder or BgeM3Embedder(
        config.embedding_model or os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        device=os.getenv("EMBEDDING_DEVICE", "auto"),
    )
    retriever = build_retriever(
        client=client,
        config=config,
        embedder=embedder,
        aliases_path=paths.aliases,
    )
    settings = QASettings.from_config(config, abstention)
    return _assemble_service(
        search_client=_RetrieverAdapter(retriever),
        advisor=_advisor(provider),
        settings=settings,
        paths=paths,
    )


class _RetrieverAdapter:
    """让 dense retriever 继续以 SearchClient 形态工作（仅评测兼容）。"""

    def __init__(self, retriever: DenseRetriever) -> None:
        self._retriever = retriever

    def warmup(self) -> None:
        self._retriever.warmup()

    def search_articles(self, request, *, timeout: float | None = None):
        from campusguide.qa.service import _RetrieverSearchClient

        return _RetrieverSearchClient(self._retriever).search_articles(
            request,
            timeout=timeout,
        )

    def get_article(self, article_id: str, part_no: int | None = None):
        raise NotImplementedError

    def close(self) -> None:
        return None


def _advisor(provider=None) -> Advisor:
    provider = provider or OpenAICompatibleChat.from_env()
    # 推理型模型的思考 token 也计入 completion 预算，可按厂商调大。
    return Advisor(provider, max_tokens=int(os.getenv("ADVISOR_MAX_TOKENS", "1200")))


def _assemble_service(
    *,
    search_client,
    advisor: Advisor,
    settings: QASettings,
    paths: RuntimePaths,
    meta_client=None,
) -> QAService:
    validator = ClaimValidator(
        min_quote_chars=settings.min_quote_chars,
        max_claims=settings.max_claims,
    )
    return QAService(
        search_client=search_client,
        meta_client=meta_client,
        advisor=advisor,
        settings=settings,
        verifier=Verifier(validator),
        trace_writer=TraceWriter(paths.trace_dir),
    )


def _client_config(server) -> MCPClientConfig:
    command = list(server.command)
    if command and command[0] in {"python", "python3"}:
        command[0] = sys.executable
    return MCPClientConfig(
        command=command,
        timeout_seconds=server.timeout_seconds,
        retries=server.retries,
        startup_timeout_seconds=server.startup_timeout_seconds,
        cwd=str(ROOT),
    )


def create_mcp_qa_service(
    *,
    paths: RuntimePaths | None = None,
    provider=None,
    start: bool = True,
) -> QAService:
    """生产入口：启动 policy-search/policy-meta stdio server 并装配 QAService。"""
    from campusguide.qa.search_clients import UnavailableSearchClient

    load_dotenv(ROOT / ".env")
    paths = paths or RuntimePaths()
    config = load_retrieval_config(paths.retrieval_config)
    abstention = load_abstention_config(paths.abstention_config)
    mcp_config: MCPConfig = load_mcp_config(paths.mcp_config)
    manifest = _read_manifest(paths.vector_manifest)
    expected_index_version = str(manifest.get("index_version", ""))

    search_client = MCPPolicySearchClient(_client_config(mcp_config.search_server))
    meta_client = MCPPolicyMetaClient(_client_config(mcp_config.meta_server))
    if start:
        try:
            search_client.start()
        except Exception as exc:
            logger.error("policy-search 启动失败，问答将明确拒答：%s", exc)
            search_client = UnavailableSearchClient(str(exc))
        try:
            meta_client.start()
        except Exception as exc:
            logger.error("policy-meta 启动失败：%s", exc)

    settings = QASettings.from_config(config, abstention)
    settings.expected_index_version = expected_index_version
    return _assemble_service(
        search_client=search_client,
        meta_client=meta_client,
        advisor=_advisor(provider),
        settings=settings,
        paths=paths,
    )


def create_hybrid_qa_service(
    *,
    client: QdrantClient,
    paths: RuntimePaths | None = None,
    embedder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    provider=None,
    verify_index: bool = True,
    use_rerank: bool = True,
) -> QAService:
    """W4 评测入口：进程内 HybridRetriever + 同一 SearchClient 契约。"""
    from campusguide.qa.search_clients import LocalHybridSearchClient
    from campusguide.retrieval.embedder import BgeM3Embedder
    from campusguide.retrieval.hybrid import HybridRetriever
    from campusguide.retrieval.normalize import load_aliases
    from campusguide.retrieval.rerank import BgeReranker
    from campusguide.retrieval.sparse import BM25SparseEncoder

    load_dotenv(ROOT / ".env")
    paths = paths or RuntimePaths()
    config = load_retrieval_config(paths.retrieval_config)
    abstention = load_abstention_config(paths.abstention_config)
    manifest = _read_manifest(paths.vector_manifest)
    if verify_index:
        verify_or_raise(
            client=client,
            manifest_path=paths.vector_manifest,
            text_manifest_path=paths.text_manifest,
        )
    embedder = embedder or BgeM3Embedder(
        config.embedding_model or os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        device=os.getenv("EMBEDDING_DEVICE", "auto"),
    )
    sparse_path = Path(manifest.get("sparse", {}).get("vocab_file") or paths.sparse_vocab)
    if not sparse_path.is_absolute():
        sparse_path = ROOT / sparse_path
    sparse_encoder = BM25SparseEncoder.load(sparse_path)
    if reranker is None and use_rerank:
        reranker = BgeReranker(
            config.reranker_model,
            device=os.getenv("RERANKER_DEVICE", os.getenv("EMBEDDING_DEVICE", "auto")),
        )
    retriever = HybridRetriever(
        client=client,
        embedder=embedder,
        sparse_encoder=sparse_encoder,
        reranker=reranker,
        config=config,
        index_version=str(manifest.get("index_version", "")),
        top_k=config.recall_top_k,
        rerank_top_n=config.rerank_top_n,
        max_articles=config.max_articles,
        token_budget=config.token_budget,
        min_rerank_score=config.min_rerank_score,
        min_score_margin=config.min_score_margin,
        rrf_k=config.rrf_k,
        aliases=load_aliases(paths.aliases),
        use_rerank=use_rerank,
    )
    settings = QASettings.from_config(config, abstention)
    settings.expected_index_version = str(manifest.get("index_version", ""))
    return _assemble_service(
        search_client=LocalHybridSearchClient(retriever),
        advisor=_advisor(provider),
        settings=settings,
        paths=paths,
    )
