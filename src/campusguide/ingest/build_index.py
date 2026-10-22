"""索引构建：解析结果 + 来源清单 -> articles.jsonl / documents.jsonl / manifest.json。

W1 只做确定性的文本索引与 Qdrant 集合骨架，不生成 embedding：
- 条款按 (doc_id, version_id, article_index, part_no) 排序，保证重建一致；
- manifest 的 content_hash 不包含 generated_at，可用于验证幂等；
- 缺少解析结果的版本会记录在 missing_versions，不静默跳过。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    Distance,
    PayloadSchemaType,
    SparseIndexParams,
    SparseVectorParams,
    VectorParams,
)

from campusguide.config import (
    RetrievalConfig,
    SourceDocument,
    SourcesConfig,
    SourceVersion,
    load_sources,
)
from campusguide.domain.models import Article, DocumentVersion
from campusguide.ingest.parse import ParsedDocument, load_parsed_document
from campusguide.ingest.segment import segment_document

INDEX_SCHEMA_VERSION = "1.1"
INDEX_VERSION = "w1-articles-v2"
ARTICLES_FILENAME = "articles.jsonl"
DOCUMENTS_FILENAME = "documents.jsonl"
MANIFEST_FILENAME = "manifest.json"

PAYLOAD_KEYWORD_FIELDS = (
    "doc_id",
    "version_id",
    "doc_type",
    "authority_level",
    "issuer_level",
    "audience",
    "status",
    "flow_ids",
)
PAYLOAD_DATETIME_FIELDS = ("effective_from", "effective_to")


class BuildError(RuntimeError):
    """索引构建失败，需要人工修复输入。"""


@dataclass
class BuildResult:
    articles_written: int
    documents_written: int
    versions_written: int
    blocks_skipped: int
    missing_versions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    articles_path: Path | None = None
    documents_path: Path | None = None
    manifest_path: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    _atomic_write_text(path, text)


def _parse_fetched_at(value: str | None, *, context: str) -> datetime:
    if not value:
        raise BuildError(f"{context} 缺少 fetched_at，请重新执行 fetch/parse")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BuildError(f"{context} 的 fetched_at 不是 ISO 时间：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _make_document_version(
    doc: SourceDocument,
    version: SourceVersion,
    parsed: ParsedDocument,
) -> DocumentVersion:
    context = f"{doc.doc_id}@{version.version_id}"
    if version.published_at is None:
        raise BuildError(
            f"{context} 缺少 published_at，无法生成版本元数据；请在 sources.yaml 补齐日期"
        )
    if version.url is None:
        raise BuildError(f"{context} 缺少 source_url，无法生成正式引用")
    if not parsed.source_sha256:
        raise BuildError(f"{context} 的解析结果缺少 source_sha256，请重新执行 fetch/parse")

    effective_audience = version.audience or doc.audience
    effective_detail = (
        version.audience_detail if version.audience_detail is not None else doc.audience_detail
    )

    return DocumentVersion(
        doc_id=doc.doc_id,
        version_id=version.version_id,
        title=doc.title,
        doc_type=doc.doc_type,
        authority_level=doc.effective_authority_level,
        issuer_level=doc.issuer_level,
        issuer_college_id=doc.issuer_college_id,
        parent_doc_id=doc.parent_doc_id,
        relation=doc.relation,
        issuing_department=doc.issuing_department,
        source_url=version.url,
        published_at=version.published_at,
        effective_from=version.effective_from or version.published_at,
        effective_to=version.effective_to,
        status=version.status,
        source_sha256=parsed.source_sha256,
        fetched_at=_parse_fetched_at(parsed.fetched_at, context=context),
        parser_version=parsed.parser_version,
        audience=effective_audience,
        audience_detail=effective_detail,
        notes=version.notes,
    )


def _content_hash(
    articles: list[Article],
    documents: list[DocumentVersion],
) -> str:
    payload = {
        "index_version": INDEX_VERSION,
        "articles": [article.model_dump(mode="json") for article in articles],
        "documents": [document.model_dump(mode="json") for document in documents],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _article_sort_key(article: Article) -> tuple[str, str, int, int, str]:
    return (
        article.doc_id,
        article.version_id,
        article.article_index,
        article.part_no,
        article.article_id,
    )


def init_qdrant_collection(
    client: QdrantClient,
    retrieval: RetrievalConfig,
    *,
    recreate: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """创建集合骨架与 payload 索引；W1 不上传向量点。"""
    warnings: list[str] = []
    existed = client.collection_exists(retrieval.collection)
    recreated = False
    if existed and recreate:
        client.delete_collection(retrieval.collection)
        existed = False
        recreated = True

    created = False
    if not existed:
        client.create_collection(
            collection_name=retrieval.collection,
            vectors_config={
                retrieval.dense_vector_name: VectorParams(
                    size=retrieval.dense_vector_size,
                    distance=Distance.COSINE,
                )
            },
            sparse_vectors_config={
                retrieval.sparse_vector_name: SparseVectorParams(
                    index=SparseIndexParams(on_disk=False)
                )
            },
        )
        created = True

    for field_name in PAYLOAD_KEYWORD_FIELDS:
        try:
            client.create_payload_index(
                collection_name=retrieval.collection,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:  # 本地模式能力不同，不应阻断构建
            warnings.append(f"payload 索引 {field_name} 创建失败：{exc}")

    for field_name in PAYLOAD_DATETIME_FIELDS:
        try:
            client.create_payload_index(
                collection_name=retrieval.collection,
                field_name=field_name,
                field_schema=PayloadSchemaType.DATETIME,
            )
        except Exception as exc:  # 本地模式能力不同，不应阻断构建
            warnings.append(f"payload 索引 {field_name} 创建失败：{exc}")

    try:
        client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=retrieval.collection,
                        alias_name=retrieval.alias,
                    )
                )
            ]
        )
    except Exception as exc:  # 本地模式可能不支持 alias
        warnings.append(f"alias {retrieval.alias} 创建失败：{exc}")

    info = client.get_collection(retrieval.collection)
    vectors = info.config.params.vectors
    dense_size = retrieval.dense_vector_size
    if isinstance(vectors, dict):
        configured = vectors.get(retrieval.dense_vector_name)
        if configured is not None:
            dense_size = configured.size

    details = {
        "collection": retrieval.collection,
        "alias": retrieval.alias,
        "created": created,
        "recreated": recreated,
        "dense_vector_name": retrieval.dense_vector_name,
        "dense_vector_size": dense_size,
        "sparse_vector_name": retrieval.sparse_vector_name,
        "points_uploaded": 0,
        "warnings": warnings,
    }
    return details, warnings


def build_index(
    *,
    sources_path: Path,
    parsed_dir: Path,
    output_dir: Path,
    retrieval: RetrievalConfig | None = None,
    init_qdrant: bool = False,
    qdrant_client: QdrantClient | None = None,
    qdrant_url: str | None = None,
    manifest_path: Path | None = None,
    recreate_collection: bool = False,
    generated_at: datetime | None = None,
) -> BuildResult:
    config: SourcesConfig = load_sources(sources_path)
    articles: list[Article] = []
    documents: list[DocumentVersion] = []
    sources_manifest: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_versions: list[str] = []
    blocks_skipped = 0

    for doc in sorted(config.documents, key=lambda item: item.doc_id):
        for version in sorted(doc.versions, key=lambda item: item.version_id):
            label = f"{doc.doc_id}@{version.version_id}"
            parsed_path = parsed_dir / doc.doc_id / f"{version.version_id}.json"
            if not parsed_path.exists():
                missing_versions.append(label)
                warnings.append(f"缺少解析结果，跳过 {label}（期望 {parsed_path}）")
                continue

            parsed = load_parsed_document(parsed_path)
            if parsed.doc_id != doc.doc_id or parsed.version_id != version.version_id:
                raise BuildError(
                    f"解析结果标识不匹配：{parsed_path} 为 "
                    f"{parsed.doc_id}@{parsed.version_id}，期望 {label}"
                )

            document_version = _make_document_version(doc, version, parsed)
            segment = segment_document(
                parsed,
                document_version,
                topic_tags=doc.topic_tags,
                flow_ids=doc.flow_ids,
                redact_contacts=doc.redact_contacts,
            )
            blocks_skipped += len(segment.blocks)
            warnings.extend(f"{label}: {warning}" for warning in segment.warnings)
            articles.extend(segment.indexable_articles)
            documents.append(document_version)
            sources_manifest.append(
                {
                    "doc_id": doc.doc_id,
                    "version_id": version.version_id,
                    "source_sha256": parsed.source_sha256,
                    "parser_version": parsed.parser_version,
                    "articles": len(segment.indexable_articles),
                    "blocks": len(segment.blocks),
                    "warnings": segment.warnings,
                }
            )

    articles.sort(key=_article_sort_key)
    documents.sort(key=lambda item: (item.doc_id, item.version_id))

    keys = Counter((article.article_id, article.part_no) for article in articles)
    duplicates = sorted(key for key, count in keys.items() if count > 1)
    if duplicates:
        preview = ", ".join(f"{article_id}#part{part_no}" for article_id, part_no in duplicates[:5])
        raise BuildError(f"存在重复条款键（article_id + part_no）：{preview}")

    multi_part = sum(
        1 for count in Counter(article.article_id for article in articles).values() if count > 1
    )
    empty_text = sum(1 for article in articles if not article.text.strip())
    manifest: dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "index_version": INDEX_VERSION,
        "corpus_id": config.corpus_id,
        "school": config.school,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "content_hash": _content_hash(articles, documents),
        "embedding": {
            "model": retrieval.embedding_model if retrieval else None,
            "dense_vector_name": retrieval.dense_vector_name if retrieval else None,
            "dense_vector_size": retrieval.dense_vector_size if retrieval else None,
            "sparse_vector_name": retrieval.sparse_vector_name if retrieval else None,
        },
        "counts": {
            "documents": len({document.doc_id for document in documents}),
            "versions": len(documents),
            "articles": len(articles),
            "articles_with_multiple_parts": multi_part,
            "blocks_excluded": blocks_skipped,
            "empty_text": empty_text,
            "missing_versions": len(missing_versions),
        },
        "sources": sources_manifest,
        "missing_versions": missing_versions,
        "warnings": warnings,
    }

    qdrant_warnings: list[str] = []
    if init_qdrant:
        if retrieval is None:
            raise BuildError("init_qdrant=True 时必须提供 retrieval 配置")
        client = qdrant_client or QdrantClient(
            url=qdrant_url or os.getenv("QDRANT_URL", "http://localhost:6333")
        )
        details, qdrant_warnings = init_qdrant_collection(
            client,
            retrieval,
            recreate=recreate_collection,
        )
        manifest["qdrant"] = details

    output_dir.mkdir(parents=True, exist_ok=True)
    articles_path = output_dir / ARTICLES_FILENAME
    documents_path = output_dir / DOCUMENTS_FILENAME
    manifest_path = manifest_path or (output_dir / MANIFEST_FILENAME)
    _write_jsonl(articles_path, [article.model_dump(mode="json") for article in articles])
    _write_jsonl(
        documents_path,
        [document.model_dump(mode="json") for document in documents],
    )
    _atomic_write_json(manifest_path, manifest)

    return BuildResult(
        articles_written=len(articles),
        documents_written=len({document.doc_id for document in documents}),
        versions_written=len(documents),
        blocks_skipped=blocks_skipped,
        missing_versions=missing_versions,
        warnings=warnings + qdrant_warnings,
        output_dir=output_dir,
        articles_path=articles_path,
        documents_path=documents_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )
