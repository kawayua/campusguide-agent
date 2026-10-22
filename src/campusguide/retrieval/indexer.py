"""articles.jsonl -> Qdrant 向量点与 vector_manifest。

W2 只写 dense 向量；collection 保留 W1 的 sparse 配置，W4 再补 sparse。
点 ID 使用 UUID5，保证同一 ``article_id + part_no`` 重复构建幂等。
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from campusguide.config import RetrievalConfig
from campusguide.domain.models import Article
from campusguide.ingest.build_index import init_qdrant_collection
from campusguide.retrieval.embedder import EmbeddingProvider

VECTOR_MANIFEST_SCHEMA_VERSION = "1.0"
POINT_NAMESPACE = "campusguide"


class VectorIndexError(RuntimeError):
    """向量索引构建或校验失败。"""


def point_id(article_id: str, part_no: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{POINT_NAMESPACE}:{article_id}:part{part_no}"))


def _rfc3339(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return (
        datetime.combine(value, datetime.min.time(), tzinfo=UTC).isoformat().replace("+00:00", "Z")
    )


def embedding_text(article: Article) -> str:
    chapter = " / ".join(article.chapter_path) if article.chapter_path else "无"
    return (
        f"标题：{article.doc_title}\n"
        f"章节：{chapter}\n"
        f"位置：{article.article_no}\n"
        f"正文：{article.text}"
    )


def article_payload(article: Article) -> dict[str, Any]:
    detail = article.audience_detail
    return {
        "article_id": article.article_id,
        "doc_id": article.doc_id,
        "version_id": article.version_id,
        "doc_title": article.doc_title,
        "doc_type": article.doc_type.value,
        "authority_level": article.authority_level.value,
        "block_type": article.block_type.value,
        "chapter_path": list(article.chapter_path),
        "article_no": article.article_no,
        "article_index": article.article_index,
        "part_no": article.part_no,
        "text": article.text,
        "text_normalized": article.text_normalized,
        "audience": [audience.value for audience in article.audience],
        "audience_colleges": list(detail.colleges) if detail else [],
        "audience_majors": list(detail.majors) if detail else [],
        "status": article.status.value,
        "effective_from": _rfc3339(article.effective_from),
        "effective_to": _rfc3339(article.effective_to),
        "date_confidence": article.date_confidence,
        "source_url": str(article.source_url),
        "published_at": _rfc3339(article.published_at),
        "issuing_department": article.issuing_department,
        "topic_tags": list(article.topic_tags),
        "flow_ids": list(article.flow_ids),
        "source_sha256": article.source_sha256,
    }


def load_articles(path: str | Path) -> list[Article]:
    path = Path(path)
    articles: list[Article] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                articles.append(Article.model_validate_json(line))
            except Exception as exc:
                raise VectorIndexError(f"{path}:{line_no} 不是合法 Article：{exc}") from exc
    return articles


def build_vector_index(
    *,
    client: QdrantClient,
    articles: Sequence[Article],
    embedder: EmbeddingProvider,
    retrieval: RetrievalConfig,
    batch_size: int = 16,
    recreate: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """写入 dense 向量并返回 vector manifest 内容（不落盘）。"""
    indexable = [article for article in articles if article.is_indexable]
    if not indexable:
        raise VectorIndexError("没有可索引块，拒绝构建空向量集合")

    if embedder.dimension != retrieval.dense_vector_size:
        raise VectorIndexError(
            f"embedding 维度 {embedder.dimension} 与 collection 配置 "
            f"{retrieval.dense_vector_size} 不一致"
        )

    warnings: list[str] = []
    details, init_warnings = init_qdrant_collection(
        client,
        retrieval,
        recreate=recreate,
    )
    warnings.extend(init_warnings)
    if details.get("dense_vector_size") != embedder.dimension:
        raise VectorIndexError(
            f"collection 维度 {details.get('dense_vector_size')} 与 embedding "
            f"{embedder.dimension} 不一致，请用 --recreate 重建"
        )

    total = len(indexable)
    written = 0
    for start in range(0, total, batch_size):
        batch = indexable[start : start + batch_size]
        vectors = embedder.embed_documents([embedding_text(article) for article in batch])
        if len(vectors) != len(batch):
            raise VectorIndexError("embedding 返回数量与输入块数不一致")
        points = [
            PointStruct(
                id=point_id(article.article_id, article.part_no),
                vector={retrieval.dense_vector_name: vector},
                payload=article_payload(article),
            )
            for article, vector in zip(batch, vectors, strict=True)
        ]
        client.upsert(
            collection_name=retrieval.collection,
            points=points,
            wait=True,
        )
        written += len(points)
        if progress is not None:
            progress(written, total)

    counts = Counter(article.block_type.value for article in indexable)
    manifest = {
        "schema_version": VECTOR_MANIFEST_SCHEMA_VERSION,
        "collection": retrieval.collection,
        "alias": retrieval.alias,
        "embedding_model": embedder.model_name,
        "dimension": embedder.dimension,
        "dense_vector_name": retrieval.dense_vector_name,
        "points": written,
        "block_types": dict(sorted(counts.items())),
        "device": embedder.device,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "embedding_text": "标题/章节/位置/正文",
        "warnings": warnings,
    }
    return manifest


def read_vector_manifest(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise VectorIndexError(f"缺少向量 manifest：{path}，请先运行 build_vectors.py")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VectorIndexError(f"向量 manifest 不是合法 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise VectorIndexError(f"向量 manifest 顶层必须是对象：{path}")
    return payload


def write_vector_manifest(path: str | Path, payload: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def verify_vector_manifest(
    *,
    client: QdrantClient,
    manifest: dict[str, Any],
    source_content_hash: str | None = None,
) -> list[str]:
    """返回不一致项；空列表表示 manifest 与 Qdrant 实际状态一致。

    ``source_content_hash`` 由文本 manifest 提供，用于防止“文本索引已重建、
    向量索引仍是旧版”但点数/维度恰好相同的情况。
    """
    problems: list[str] = []
    collection = manifest.get("collection")
    if not collection:
        return ["manifest 缺少 collection"]

    if source_content_hash is not None:
        vector_source = manifest.get("source_content_hash")
        if not vector_source:
            problems.append("向量 manifest 缺少 source_content_hash，无法确认与文本索引同源")
        elif vector_source != source_content_hash:
            problems.append(
                "文本索引与向量索引不同源："
                f"text={source_content_hash[:12]}…, vector={str(vector_source)[:12]}…；"
                "请重新运行 build_vectors.py"
            )

    if not client.collection_exists(collection):
        return [f"Qdrant 集合不存在：{collection}"]

    info = client.get_collection(collection)
    points = info.points_count or 0
    expected = int(manifest.get("points", 0))
    if points != expected:
        problems.append(f"点数不一致：manifest={expected}, qdrant={points}")

    vectors = info.config.params.vectors
    if isinstance(vectors, dict):
        configured = vectors.get(manifest.get("dense_vector_name", "dense"))
        actual_size = getattr(configured, "size", None)
        expected_size = int(manifest.get("dimension", 0))
        if actual_size != expected_size:
            problems.append(f"维度不一致：manifest={expected_size}, qdrant={actual_size}")

    return problems
