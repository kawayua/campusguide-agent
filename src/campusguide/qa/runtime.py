"""QAService 的本地组装：配置、MCP client、provider 与 trace。

生产路径（CLI/UI）通过 stdio 拉起 ``policy-search`` / ``policy-meta``；
``create_qa_service`` 保留为 W2/W3 评测的 dense-only 离线组装，
``create_hybrid_qa_service`` 供 W4 评测在进程内复用混合检索。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import OpenAICompatibleChat
from campusguide.config import (
    MCPConfig,
    RetrievalConfig,
    load_abstention_config,
    load_mcp_config,
    load_organization,
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
    verifier: Any | None = None,
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
        verifier=verifier or Verifier(validator),
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
    verifier: Any | None = None,
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
        verifier=verifier,
    )


def create_local_hybrid_search_client(
    *,
    client: QdrantClient,
    paths: RuntimePaths | None = None,
    embedder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    verify_index: bool = True,
    use_rerank: bool = True,
):
    """构造进程内 Hybrid Retriever 的 SearchClient（W6 图评测复用）。"""
    from campusguide.qa.search_clients import LocalHybridSearchClient
    from campusguide.retrieval.embedder import BgeM3Embedder
    from campusguide.retrieval.hybrid import HybridRetriever
    from campusguide.retrieval.normalize import load_aliases
    from campusguide.retrieval.profile_expand import ProfileLexicon
    from campusguide.retrieval.rerank import BgeReranker
    from campusguide.retrieval.sparse import BM25SparseEncoder

    load_dotenv(ROOT / ".env")
    paths = paths or RuntimePaths()
    config = load_retrieval_config(paths.retrieval_config)
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
    organization = load_organization(ROOT / "configs" / "organization.yaml")
    retriever = HybridRetriever(
        client=client,
        embedder=embedder,
        sparse_encoder=sparse_encoder,
        reranker=reranker,
        config=config,
        index_version=str(manifest.get("index_version", "")),
        top_k=config.recall_top_k,
        rerank_candidates=config.rerank_candidates,
        rerank_top_n=config.rerank_top_n,
        max_articles=config.max_articles,
        max_table_sections=config.max_table_sections,
        token_budget=config.token_budget,
        min_rerank_score=config.min_rerank_score,
        min_score_margin=config.min_score_margin,
        rrf_k=config.rrf_k,
        aliases=load_aliases(paths.aliases),
        profile_lexicon=ProfileLexicon.from_organization(organization),
        use_rerank=use_rerank,
    )
    return LocalHybridSearchClient(retriever)


def create_hybrid_qa_service(
    *,
    client: QdrantClient,
    paths: RuntimePaths | None = None,
    embedder: EmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    provider=None,
    verify_index: bool = True,
    use_rerank: bool = True,
    verifier: Any | None = None,
) -> QAService:
    """W4 评测入口：进程内 HybridRetriever + 同一 SearchClient 契约。"""
    load_dotenv(ROOT / ".env")
    paths = paths or RuntimePaths()
    search_client = create_local_hybrid_search_client(
        client=client,
        paths=paths,
        embedder=embedder,
        reranker=reranker,
        verify_index=verify_index,
        use_rerank=use_rerank,
    )
    config = load_retrieval_config(paths.retrieval_config)
    abstention = load_abstention_config(paths.abstention_config)
    manifest = _read_manifest(paths.vector_manifest)
    settings = QASettings.from_config(config, abstention)
    settings.expected_index_version = str(manifest.get("index_version", ""))
    return _assemble_service(
        search_client=search_client,
        advisor=_advisor(provider),
        settings=settings,
        paths=paths,
        verifier=verifier,
    )


DEFAULT_GRAPH_CONFIG = ROOT / "configs" / "graph.yaml"
DEFAULT_HANDOFF_CONFIG = ROOT / "configs" / "handoff.yaml"


@dataclass
class GraphRuntime:
    runner: Any
    service: Any
    search_client: Any = None
    meta_client: Any = None
    clients: list[Any] = field(default_factory=list)
    model_profile: str = "vpro"
    models: dict[str, str] = field(default_factory=dict)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    index_version: str = ""

    def close(self) -> None:
        for client in [*self.clients, self.search_client, self.meta_client]:
            close = getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


def _role_provider(model_env: str, timeout: float):
    from campusguide.agents.providers import OpenAICompatibleChat

    return OpenAICompatibleChat(
        base_url=os.getenv("LLM_BASE_URL", ""),
        api_key=os.getenv("LLM_API_KEY", ""),
        model=os.getenv(model_env, ""),
        timeout=timeout,
    )


def create_graph_runtime(
    *,
    paths: RuntimePaths | None = None,
    search_client: Any | None = None,
    meta_client: Any | None = None,
    flow_catalog: Any | None = None,
    planner_provider: Any | None = None,
    advisor_provider: Any | None = None,
    semantic_provider: Any | None = None,
    start: bool = True,
    today: Any | None = None,
    trace_writer: Any | None = None,
) -> GraphRuntime:
    """装配 W6 图。生产默认启动 MCP；评测可注入 local search/flow catalog。"""
    from campusguide.agents.advisor import (
        REVISE_PROMPT_VERSION,
        W6B_ADVISOR_PROMPT_VERSION,
        Advisor,
    )
    from campusguide.agents.graph import GraphNodes, GraphRunner
    from campusguide.agents.planner import PLANNER_PROMPT_VERSION, Planner
    from campusguide.agents.retriever import GraphRetriever
    from campusguide.agents.verifier import GraphVerifier
    from campusguide.config import load_graph_config, load_handoff_config
    from campusguide.conversation.service import ConversationService
    from campusguide.flows.catalog import MCPFlowCatalog
    from campusguide.verification.claims import ClaimValidator
    from campusguide.verification.semantic import (
        SEMANTIC_PROMPT_VERSION,
        SemanticVerifier,
    )

    load_dotenv(ROOT / ".env")
    paths = paths or RuntimePaths()
    retrieval = load_retrieval_config(paths.retrieval_config)
    abstention = load_abstention_config(paths.abstention_config)
    graph_config = load_graph_config(DEFAULT_GRAPH_CONFIG)
    if os.getenv("W6_GRAPH_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        raise RuntimeError("W6 图已通过 W6_GRAPH_ENABLED=false 关闭")
    profile = os.getenv("W6_MODEL_PROFILE", "").strip()
    semantic_mode = os.getenv("W6_SEMANTIC_MODE", "").strip()
    overrides: dict[str, object] = {}
    if profile:
        overrides["model_profile"] = profile
    if semantic_mode:
        overrides["semantic"] = {
            **graph_config.semantic.model_dump(),
            "on_unavailable": semantic_mode,
        }
    if overrides:
        from campusguide.config import GraphConfig

        graph_config = GraphConfig.model_validate({**graph_config.model_dump(), **overrides})
    handoff_config = load_handoff_config(DEFAULT_HANDOFF_CONFIG)
    mcp_config = load_mcp_config(paths.mcp_config)
    manifest = _read_manifest(paths.vector_manifest)
    expected_index_version = str(manifest.get("index_version", ""))
    driven_clients: list[Any] = []

    if search_client is None:
        from campusguide.qa.search_clients import UnavailableSearchClient

        mcp_search = MCPPolicySearchClient(_client_config(mcp_config.search_server))
        driven_clients.append(mcp_search)
        if start:
            try:
                mcp_search.start()
            except Exception as exc:
                logger.error("policy-search 启动失败，W6 将明确拒答：%s", exc)
                search_client = UnavailableSearchClient(str(exc))
            else:
                search_client = mcp_search
        else:
            search_client = mcp_search
    if meta_client is None:
        meta_client = MCPPolicyMetaClient(_client_config(mcp_config.meta_server))
        driven_clients.append(meta_client)
        if start:
            try:
                meta_client.start()
            except Exception as exc:
                logger.error("policy-meta 启动失败：%s", exc)
    if flow_catalog is None:
        flow_catalog = MCPFlowCatalog(meta_client)

    planner_provider = planner_provider or _role_provider("PLANNER_MODEL", 20.0)
    advisor_provider = advisor_provider or _role_provider("ADVISOR_MODEL", 40.0)
    semantic_provider = semantic_provider or _role_provider(
        "VERIFIER_MODEL", graph_config.semantic.timeout_seconds
    )

    retriever = GraphRetriever(
        search_client,
        top_k=retrieval.recall_top_k,
        rerank_top_n=retrieval.rerank_top_n,
        max_evidence=graph_config.max_evidence,
        max_per_doc=graph_config.max_per_doc,
        token_budget=graph_config.token_budget,
        expected_index_version=expected_index_version,
        enable_repealed_probe=abstention.enable_repealed_probe,
    )
    planner = Planner(
        flow_catalog,
        provider=planner_provider,
        llm_enabled=graph_config.planner.llm_enabled,
        max_queries=graph_config.max_queries,
        max_clarify=graph_config.max_clarify,
    )
    advisor = Advisor(
        advisor_provider,
        max_tokens=int(os.getenv("ADVISOR_MAX_TOKENS", "3000")),
    )
    semantic = SemanticVerifier(
        semantic_provider,
        enabled=graph_config.semantic.enabled,
        batch_size=graph_config.semantic.batch_size,
        timeout_seconds=graph_config.semantic.timeout_seconds,
    )
    validator = ClaimValidator(
        min_quote_chars=abstention.min_quote_chars,
        max_claims=abstention.max_claims,
    )
    verifier = GraphVerifier(
        validator,
        semantic,
        max_revise=graph_config.max_revise,
        semantic_mode=graph_config.semantic.on_unavailable,
    )
    nodes = GraphNodes(
        planner=planner,
        retriever=retriever,
        advisor=advisor,
        verifier=verifier,
        catalog=flow_catalog,
        handoff_config=handoff_config,
        trace_writer=trace_writer or TraceWriter(paths.trace_dir),
        max_clarify=graph_config.max_clarify,
        max_revise=graph_config.max_revise,
        today=today,
    )
    runner = GraphRunner(nodes, ttl_hours=graph_config.checkpointer.ttl_hours)
    service = ConversationService(
        None,
        graph_runner=runner,
        today_provider=(lambda: today) if today else None,
    )
    return GraphRuntime(
        runner=runner,
        service=service,
        search_client=search_client,
        meta_client=meta_client,
        clients=driven_clients,
        model_profile=graph_config.model_profile,
        models={
            "planner": getattr(planner_provider, "model", os.getenv("PLANNER_MODEL", "")),
            "advisor": getattr(advisor_provider, "model", os.getenv("ADVISOR_MODEL", "")),
            "semantic": getattr(semantic_provider, "model", os.getenv("VERIFIER_MODEL", "")),
        },
        prompt_versions={
            "planner": PLANNER_PROMPT_VERSION,
            "advisor": W6B_ADVISOR_PROMPT_VERSION,
            "revise": REVISE_PROMPT_VERSION,
            "semantic": SEMANTIC_PROMPT_VERSION,
        },
        index_version=expected_index_version,
    )
