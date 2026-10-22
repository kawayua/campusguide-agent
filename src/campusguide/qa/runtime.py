"""QAService 的本地组装：配置、Qdrant、embedding、provider 与 trace。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import OpenAICompatibleChat
from campusguide.config import RetrievalConfig, load_retrieval_config
from campusguide.observability.trace import TraceWriter
from campusguide.qa.service import QAService, QASettings
from campusguide.retrieval.embedder import BgeM3Embedder, EmbeddingProvider
from campusguide.retrieval.indexer import (
    read_vector_manifest,
    verify_vector_manifest,
)
from campusguide.retrieval.normalize import load_aliases
from campusguide.retrieval.service import DenseRetriever
from campusguide.verification.claims import ClaimValidator

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RETRIEVAL_CONFIG = ROOT / "configs" / "retrieval.yaml"
DEFAULT_ALIASES = ROOT / "configs" / "aliases.yaml"
DEFAULT_VECTOR_MANIFEST = ROOT / "data" / "vector_manifest.json"
DEFAULT_TEXT_MANIFEST = ROOT / "data" / "manifest.json"
DEFAULT_TRACE_DIR = ROOT / "data" / "traces"


class IndexUnavailableError(RuntimeError):
    """索引缺失或与 manifest 不一致。"""


@dataclass
class RuntimePaths:
    retrieval_config: Path = DEFAULT_RETRIEVAL_CONFIG
    aliases: Path = DEFAULT_ALIASES
    vector_manifest: Path = DEFAULT_VECTOR_MANIFEST
    text_manifest: Path = DEFAULT_TEXT_MANIFEST
    trace_dir: Path = DEFAULT_TRACE_DIR


def create_qdrant_client() -> QdrantClient:
    location = os.getenv("QDRANT_LOCATION", "").strip()
    if location:
        Path(location).mkdir(parents=True, exist_ok=True)
        return QdrantClient(location=location)
    return QdrantClient(
        url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
        timeout=10,
    )


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
    paths = paths or RuntimePaths()
    config = load_retrieval_config(paths.retrieval_config)
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
    provider = provider or OpenAICompatibleChat.from_env()
    # 推理型模型的思考 token 也计入 completion 预算，可按厂商调大。
    advisor = Advisor(
        provider,
        max_tokens=int(os.getenv("ADVISOR_MAX_TOKENS", "1200")),
    )
    return QAService(
        retriever=retriever,
        advisor=advisor,
        settings=QASettings.from_config(config),
        validator=ClaimValidator(),
        trace_writer=TraceWriter(paths.trace_dir),
    )
