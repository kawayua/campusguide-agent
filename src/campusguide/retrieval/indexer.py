"""articles.jsonl -> Qdrant 向量点与 vector_manifest。

W4 同时写入 dense 与 BM25 sparse 向量。collection 使用命名向量，
点 ID 仍用 UUID5，保证同一 ``article_id + part_no`` 重复构建幂等。
"""

from __future__ import annotations

import hashlib
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
from campusguide.retrieval.sparse import SparseEncoder

VECTOR_MANIFEST_SCHEMA_VERSION = "1.2"
POINT_NAMESPACE = "campusguide"
TABLE_PARENT_KIND = "table_parent"
TABLE_ROW_KINDS = {"table_row", "matrix_row"}
PARENT_PROVENANCE_FIELDS = (
    "table_continuation_of",
    "table_header_source_id",
    "table_header_source_page",
    "table_column_bindings",
)
PARENT_SOURCE_FIELDS = ("source_sha256", "source_url")


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
        "block_kind": article.block_kind,
        "chapter_path": list(article.chapter_path),
        "article_no": article.article_no,
        "article_index": article.article_index,
        "part_no": article.part_no,
        "text": article.text,
        "text_normalized": article.text_normalized,
        "issuer_level": article.issuer_level.value,
        "issuer_college_id": article.issuer_college_id,
        "parent_doc_id": article.parent_doc_id,
        "parent_version_id": article.parent_version_id,
        "relation": article.relation.value,
        "parent_article_id": article.parent_article_id,
        "parent_table_id": article.parent_table_id,
        "table_id": article.table_id,
        "row_id": article.row_id,
        "row_index": article.row_index,
        "table_headers": list(article.table_headers),
        "header_paths": [list(path) for path in article.header_paths],
        "cell_provenance": [
            cell.model_dump(mode="json") for cell in article.cell_provenance
        ],
        "table_column_bindings": [
            binding.model_dump(mode="json")
            for binding in article.table_column_bindings
        ],
        "table_continuation_of": article.table_continuation_of,
        "table_header_source_id": article.table_header_source_id,
        "table_header_source_page": article.table_header_source_page,
        "is_indexable": article.is_indexable,
        "page": article.page,
        "bbox": list(article.bbox) if article.bbox else None,
        "dom_path": article.dom_path,
        "source_cell_ids": list(article.source_cell_ids),
        "audience": [audience.value for audience in article.audience],
        "audience_colleges": list(detail.colleges) if detail else [],
        "audience_majors": list(detail.majors) if detail else [],
        "audience_intake_years": list(detail.intake_years) if detail else [],
        "audience_intake_from": detail.intake_from if detail else None,
        "audience_graduating_years": list(detail.graduating_years) if detail else [],
        "audience_admission_year": detail.admission_year if detail else None,
        "audience_stages": [stage.value for stage in detail.stages] if detail else [],
        "audience_program_types": (
            [program_type.value for program_type in detail.program_types] if detail else []
        ),
        "audience_learning_modes": (
            [mode.value for mode in detail.learning_modes] if detail else []
        ),
        "audience_degree_types": (
            [degree.value for degree in detail.degree_types] if detail else []
        ),
        "audience_groups": [group.value for group in detail.groups] if detail else [],
        "audience_status": [status.value for status in detail.status] if detail else [],
        "audience_class_types": list(detail.class_types) if detail else [],
        "audience_scope_status": detail.scope_status.value if detail else "unknown",
        "audience_scope_evidence": detail.scope_evidence if detail else None,
        "audience_cycle": detail.cycle if detail else None,
        "status": article.status.value,
        "effective_from": _rfc3339(article.effective_from),
        "effective_to": _rfc3339(article.effective_to),
        "date_confidence": article.date_confidence,
        "date_source": article.date_source,
        "source_url": str(article.source_url),
        "published_at": _rfc3339(article.published_at),
        "issuing_department": article.issuing_department,
        "topic_tags": list(article.topic_tags),
        "flow_ids": list(article.flow_ids),
        "source_sha256": article.source_sha256,
        "metadata_source_status": article.metadata_source_status,
        "metadata_source_audit_sha256": article.metadata_source_audit_sha256,
    }


def make_index_version(
    *,
    collection: str,
    embedding_mode: str,
    source_content_hash: str | None,
    sparse_version: str | None,
) -> str:
    """稳定、可比较的索引版本号；应用启动时用于校验 server 与本地一致。"""
    digest = hashlib.sha256()
    digest.update(collection.encode("utf-8"))
    digest.update(embedding_mode.encode("utf-8"))
    digest.update((source_content_hash or "").encode("utf-8"))
    digest.update((sparse_version or "").encode("utf-8"))
    return f"{collection}@{embedding_mode}-{digest.hexdigest()[:12]}"


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


def _validate_parent_links(
    indexable: Sequence[Article],
    parents: Sequence[Article],
) -> None:
    """在写入前拒绝重复点键和悬空/错绑的表格父子关系。"""
    for parent in parents:
        if parent.is_indexable:
            raise VectorIndexError(
                f"父表 {parent.article_id} 必须不可索引（is_indexable=False）"
            )
        if not parent.table_id:
            raise VectorIndexError(f"父表 {parent.article_id} 的 table_id 为空")
        if parent.parent_table_id != parent.table_id:
            raise VectorIndexError(
                f"父表 {parent.article_id} 的 parent_table_id={parent.parent_table_id!r} "
                f"与 table_id={parent.table_id!r} 不一致"
            )

    parent_ids = Counter(parent.article_id for parent in parents)
    duplicate_parent_ids = sorted(
        article_id for article_id, count in parent_ids.items() if count > 1
    )
    if duplicate_parent_ids:
        preview = ", ".join(duplicate_parent_ids[:5])
        raise VectorIndexError(
            f"同一 article_id 存在多个父表记录（行块仅按 article_id 引用）：{preview}"
        )

    keys = Counter(
        (article.article_id, article.part_no) for article in (*indexable, *parents)
    )
    duplicates = sorted(key for key, count in keys.items() if count > 1)
    if duplicates:
        preview = ", ".join(
            f"{article_id}#part{part_no}" for article_id, part_no in duplicates[:5]
        )
        raise VectorIndexError(f"存在重复点键（article_id + part_no）：{preview}")

    parent_by_id = {parent.article_id: parent for parent in parents}
    for row in indexable:
        if not row.parent_article_id or row.block_kind not in TABLE_ROW_KINDS:
            continue
        parent = parent_by_id.get(row.parent_article_id)
        if parent is None:
            raise VectorIndexError(
                f"行块 {row.article_id} 引用的父表 {row.parent_article_id} 未存储"
            )
        if parent.block_kind != TABLE_PARENT_KIND or parent.is_indexable:
            raise VectorIndexError(
                f"行块 {row.article_id} 的父块 {parent.article_id} 不是无向量的 "
                f"table_parent（block_kind={parent.block_kind!r}, "
                f"is_indexable={parent.is_indexable}）"
            )
        if (row.doc_id, row.version_id) != (parent.doc_id, parent.version_id):
            raise VectorIndexError(
                f"行块 {row.article_id} 与父表 {parent.article_id} 的 doc/version 不一致"
            )
        if not row.table_id or parent.table_id != row.table_id:
            raise VectorIndexError(
                f"行块 {row.article_id} 的 table_id={row.table_id!r} 与父表 "
                f"{parent.article_id} 的 {parent.table_id!r} 不一致"
            )
        if not parent.parent_table_id or parent.parent_table_id != parent.table_id:
            raise VectorIndexError(
                f"父表 {parent.article_id} 的 parent_table_id={parent.parent_table_id!r} "
                f"与 table_id={parent.table_id!r} 不一致"
            )
        if row.parent_table_id is not None and row.parent_table_id != row.table_id:
            raise VectorIndexError(
                f"行块 {row.article_id} 的 parent_table_id={row.parent_table_id!r} "
                f"与 table_id={row.table_id!r} 不一致"
            )
        for field_name in (*PARENT_PROVENANCE_FIELDS, *PARENT_SOURCE_FIELDS):
            if getattr(row, field_name) != getattr(parent, field_name):
                raise VectorIndexError(
                    f"行块 {row.article_id} 与父表 {parent.article_id} 的 "
                    f"{field_name} 不一致"
                )


def build_vector_index(
    *,
    client: QdrantClient,
    articles: Sequence[Article],
    embedder: EmbeddingProvider,
    retrieval: RetrievalConfig,
    sparse_encoder: SparseEncoder | None = None,
    source_content_hash: str | None = None,
    batch_size: int = 16,
    recreate: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """写入 dense（+sparse）向量并返回 vector manifest 内容（不落盘）。"""
    parents = [article for article in articles if article.block_kind == TABLE_PARENT_KIND]
    indexable = [
        article
        for article in articles
        if article.is_indexable and article.block_kind != TABLE_PARENT_KIND
    ]
    if not indexable:
        raise VectorIndexError("没有可索引块，拒绝构建空向量集合")

    if embedder.dimension != retrieval.dense_vector_size:
        raise VectorIndexError(
            f"embedding 维度 {embedder.dimension} 与 collection 配置 "
            f"{retrieval.dense_vector_size} 不一致"
        )

    _validate_parent_links(indexable, parents)

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

    if sparse_encoder is not None:
        info = client.get_collection(retrieval.collection)
        sparse_config = getattr(info.config.params, "sparse_vectors", None) or {}
        if retrieval.sparse_vector_name not in sparse_config:
            raise VectorIndexError(
                f"collection 缺少 sparse 向量 {retrieval.sparse_vector_name!r}；"
                "W4 重建必须使用 --recreate"
            )

    total = len(indexable)
    written = 0
    for start in range(0, total, batch_size):
        batch = indexable[start : start + batch_size]
        texts = [embedding_text(article) for article in batch]
        vectors = embedder.embed_documents(texts)
        if len(vectors) != len(batch):
            raise VectorIndexError("embedding 返回数量与输入块数不一致")
        sparse_vectors = (
            sparse_encoder.encode_documents(texts) if sparse_encoder is not None else None
        )
        if sparse_vectors is not None and len(sparse_vectors) != len(batch):
            raise VectorIndexError("sparse encoder 返回数量与输入块数不一致")
        points = []
        for offset, (article, vector) in enumerate(zip(batch, vectors, strict=True)):
            point_vector: dict[str, Any] = {retrieval.dense_vector_name: vector}
            if sparse_vectors is not None:
                point_vector[retrieval.sparse_vector_name] = sparse_vectors[offset]
            points.append(
                PointStruct(
                    id=point_id(article.article_id, article.part_no),
                    vector=point_vector,
                    payload=article_payload(article),
                )
            )
        client.upsert(
            collection_name=retrieval.collection,
            points=points,
            wait=True,
        )
        written += len(points)
        if progress is not None:
            progress(written, total)

    # 父表块只存 payload：空 vector 不进入 dense/sparse 检索，但 scroll 可水合。
    parent_points = [
        PointStruct(
            id=point_id(parent.article_id, parent.part_no),
            vector={},
            payload=article_payload(parent),
        )
        for parent in parents
    ]
    stored_parents = 0
    for start in range(0, len(parent_points), batch_size):
        batch = parent_points[start : start + batch_size]
        client.upsert(
            collection_name=retrieval.collection,
            points=batch,
            wait=True,
        )
        stored_parents += len(batch)

    counts = Counter(article.block_type.value for article in (*indexable, *parents))
    embedding_mode = "hybrid" if sparse_encoder is not None else "dense-only"
    sparse_info: dict[str, Any] = {"enabled": sparse_encoder is not None}
    if sparse_encoder is not None:
        sparse_index = getattr(sparse_encoder, "index", None)
        sparse_info.update(
            {
                "vector_name": retrieval.sparse_vector_name,
                "mode": "bm25",
                "version": sparse_encoder.version,
                "k1": getattr(sparse_index, "k1", None),
                "b": getattr(sparse_index, "b", None),
                "doc_count": getattr(sparse_index, "doc_count", None),
                "avg_doc_len": getattr(sparse_index, "avg_doc_len", None),
                "idf_hash": getattr(sparse_index, "idf_hash", None),
                "tokenizer_version": getattr(sparse_index, "tokenizer_version", None),
            }
        )
    manifest = {
        "schema_version": VECTOR_MANIFEST_SCHEMA_VERSION,
        "collection": retrieval.collection,
        "alias": retrieval.alias,
        "embedding_mode": embedding_mode,
        "embedding_model": embedder.model_name,
        "dimension": embedder.dimension,
        "dense_vector_name": retrieval.dense_vector_name,
        "sparse": sparse_info,
        "points": written + stored_parents,
        "indexable_points": written,
        "stored_parent_points": stored_parents,
        "block_types": dict(sorted(counts.items())),
        "device": embedder.device,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "embedding_text": "标题/章节/位置/正文",
        "warnings": warnings,
    }
    manifest["index_version"] = make_index_version(
        collection=retrieval.collection,
        embedding_mode=embedding_mode,
        source_content_hash=source_content_hash,
        sparse_version=sparse_encoder.version if sparse_encoder is not None else None,
    )
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

    sparse_info = manifest.get("sparse") or {}
    if sparse_info.get("enabled"):
        sparse_vectors = getattr(info.config.params, "sparse_vectors", None) or {}
        sparse_name = sparse_info.get("vector_name", "sparse")
        if sparse_name not in sparse_vectors:
            problems.append(f"collection 缺少 sparse 向量：{sparse_name}")

    return problems
