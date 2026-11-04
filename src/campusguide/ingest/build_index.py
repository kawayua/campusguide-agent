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
import re
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
    OrganizationConfig,
    RetrievalConfig,
    SourceDocument,
    SourcesConfig,
    SourceVersion,
    load_organization,
    load_sources,
)
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import ScopeStatus
from campusguide.domain.models import (
    Article,
    DocumentVersion,
    MetadataSourceStatus,
)
from campusguide.ingest.parse import ParsedDocument, load_parsed_document
from campusguide.ingest.segment import SegmentResult, segment_document
from campusguide.ingest.structure import STRUCTURE_SCHEMA_VERSION, TableStructure
from campusguide.retrieval.connections import qdrant_trust_env

INDEX_SCHEMA_VERSION = "1.3"
INDEX_VERSION = "w2-articles-v1"
# 2026-10 审计：原 17 份 + audience_detail.note 明确标注 low 的 25 份静态页 = 42。
# 2026-11-01 复核：前次 42 份 + national-aid-2026-info-merit@v2026 按网页发布时间/正文落款分列的 1 份 = 43。
# 除数量外还要求每份低置信版本带 date_source，避免用配额掩盖字段错误。
EXPECTED_LOW_CONFIDENCE_VERSIONS = 43
ARTICLES_FILENAME = "articles.jsonl"
DOCUMENTS_FILENAME = "documents.jsonl"
MANIFEST_FILENAME = "manifest.json"

PAYLOAD_KEYWORD_FIELDS = (
    "article_id",
    "doc_id",
    "version_id",
    "block_type",
    "doc_type",
    "authority_level",
    "issuer_level",
    "audience",
    "audience_colleges",
    "audience_majors",
    "audience_stages",
    "audience_program_types",
    "audience_groups",
    "audience_status",
    "audience_class_types",
    "audience_cycle",
    "status",
    "date_confidence",
    "flow_ids",
    "block_kind",
    "parent_article_id",
    "parent_table_id",
    "table_id",
    "row_id",
)
PAYLOAD_DATETIME_FIELDS = ("effective_from", "effective_to")
PAYLOAD_INTEGER_FIELDS = (
    "article_index",
    "part_no",
    "row_index",
    "table_header_source_page",
    "audience_intake_years",
    "audience_intake_from",
    "audience_graduating_years",
)


class BuildError(RuntimeError):
    """索引构建失败，需要人工修复输入。"""


@dataclass
class BuildResult:
    articles_written: int
    stored_articles_written: int
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


def _metadata_source_audit_sha256(audit: dict[str, Any]) -> str:
    canonical = json.dumps(
        audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


RAW_SNAPSHOT_META_FILENAME = "meta.json"


def _metadata_audit_source_path(
    doc: SourceDocument,
    version: SourceVersion,
    parsed: ParsedDocument,
    *,
    metadata_raw_dir: Path | None,
    parsed_dir: Path,
) -> Path:
    """定位审计要读取的实际源文件：原始快照优先，旧版本才回退 local_path。"""

    # parsed.local_path 只记录抓取前的本地输入；远程 HTML 与 PDF 来源通常为
    # None，审计必须读取 raw/<doc>/<version>/ 下的真实快照字节。
    label = f"{doc.doc_id}@{version.version_id}"
    raw_root = (
        metadata_raw_dir
        if metadata_raw_dir is not None
        else parsed_dir.parent.parent / "raw"
    )
    version_dir = raw_root / doc.doc_id / version.version_id
    if version_dir.is_dir():
        meta_path = version_dir / RAW_SNAPSHOT_META_FILENAME
        if not meta_path.is_file():
            raise BuildError(
                f"{label} 原始快照目录存在但缺少 {RAW_SNAPSHOT_META_FILENAME}："
                f"{meta_path}；拒绝回退 parsed.local_path"
            )
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildError(
                f"{label} 原始快照 meta.json 无法读取：{meta_path}：{exc}"
            ) from exc
        if not isinstance(meta, dict):
            raise BuildError(f"{label} 原始快照 meta.json 顶层必须是对象：{meta_path}")

        expected_fields = {
            "doc_id": doc.doc_id,
            "version_id": version.version_id,
        }
        if version.url is not None:
            expected_fields["source_url"] = str(version.url)
        for field, expected in expected_fields.items():
            actual = meta.get(field)
            if not isinstance(actual, str) or actual.strip() != expected:
                raise BuildError(
                    f"{label} 原始快照 meta.json 的 {field}={actual!r} "
                    f"与来源清单 {expected!r} 不一致"
                )

        parsed_sha = (parsed.source_sha256 or "").strip().lower()
        declared_sha = str(meta.get("sha256") or "").strip().lower()
        if not parsed_sha or declared_sha != parsed_sha:
            raise BuildError(
                f"{label} 原始快照 meta.json 的 sha256={meta.get('sha256')!r} "
                f"与解析结果 {parsed.source_sha256!r} 不一致"
            )

        raw_name = meta.get("source_file")
        if raw_name is None:
            raw_name = f"source.{parsed.format}"
        elif not isinstance(raw_name, str) or not raw_name.strip():
            raise BuildError(
                f"{label} 原始快照 meta.json 的 source_file={raw_name!r} 非法"
            )
        candidate = Path(raw_name)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise BuildError(
                f"{label} 原始快照 meta.json 的 source_file={raw_name!r} "
                "不是版本目录内的相对路径"
            )
        version_root = version_dir.resolve()
        source_path = (version_root / candidate).resolve()
        if not source_path.is_relative_to(version_root):
            raise BuildError(
                f"{label} 原始快照 meta.json 的 source_file={raw_name!r} 越出版本目录"
            )
        if not source_path.is_file():
            raise BuildError(f"{label} 原始快照文件不存在：{source_path}")
        actual_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_sha != parsed_sha:
            raise BuildError(
                f"{label} 原始快照文件 SHA-256={actual_sha} 与解析结果 "
                f"{parsed.source_sha256} 不一致"
            )
        return source_path

    if parsed.local_path:
        return Path(parsed.local_path)
    raise BuildError(
        f"{label} 缺少原始快照与 parsed.local_path，无法按实际源文件执行来源元数据审计"
    )


def _make_document_version(
    doc: SourceDocument,
    version: SourceVersion,
    parsed: ParsedDocument,
    *,
    metadata_source_status: MetadataSourceStatus = "not_checked",
    metadata_source_audit_sha256: str | None = None,
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
    if effective_detail is None:
        effective_detail = AudienceDetail(
            scope_status=ScopeStatus.UNKNOWN,
            note="来源清单未提供受众细目，适用范围未识别",
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
        parent_version_id=doc.parent_version_id,
        relation=doc.relation,
        issuing_department=doc.issuing_department,
        source_url=version.url,
        published_at=version.published_at,
        effective_from=version.effective_from or version.published_at,
        effective_to=version.effective_to,
        status=version.status,
        source_sha256=parsed.source_sha256,
        metadata_source_status=metadata_source_status,
        metadata_source_audit_sha256=metadata_source_audit_sha256,
        fetched_at=_parse_fetched_at(parsed.fetched_at, context=context),
        parser_version=parsed.parser_version,
        audience=effective_audience,
        audience_detail=effective_detail,
        audience_scopes=doc.audience_scopes,
        date_confidence=version.date_confidence,
        date_source=version.date_source,
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


FATAL_WARNING_MARKERS = (
    "解析结果为空",
    "PDF 未提取到文本",
    "结构层有表格但未生成",
    "SHA-256 不一致",
    "schema 版本",
    "父块",
    "无法水合",
)
REVIEW_WARNING_MARKERS = (
    "请人工",
    "待复核",
    "需复核",
    "需视觉",
    "未识别",
    "未能映射",
    "少于 200",
    "OCR",
    "扫描",
    "冲突",
    "重复",
    "超过",
    "空单元格",
    "跨行",
    "合并",
    "无表头",
    "矩阵",
    "行列",
)


def _classify_warning(message: str) -> str:
    if any(marker in message for marker in FATAL_WARNING_MARKERS):
        return "fatal"
    if any(marker in message for marker in REVIEW_WARNING_MARKERS):
        return "review"
    return "info"


def _warning_buckets() -> dict[str, list[str]]:
    return {"fatal": [], "review": [], "info": []}


def _add_warning(buckets: dict[str, list[str]], message: str, severity: str | None = None) -> None:
    level = severity or _classify_warning(message)
    if message not in buckets[level]:
        buckets[level].append(message)


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _int_or_none(value: Any) -> int | None:
    # 计数和页号不接受 bool/float：int(2.9) 静默截断会伪造覆盖证据。
    if isinstance(value, (bool, float)):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _page_map_problems(
    mapping: Any,
    field: str,
    expected_pages: set[int],
) -> tuple[list[str], set[int]]:
    problems: list[str] = []
    if mapping is None:
        return problems, set()
    if not isinstance(mapping, dict):
        return [f"{field} 不是逐页对象"], set()
    numbers: set[int] = set()
    upper = max(expected_pages) if expected_pages else 0
    for raw_page, evidence in mapping.items():
        page = _int_or_none(raw_page)
        if page is None or page <= 0:
            problems.append(f"{field} 含非法页号 {raw_page!r}")
            continue
        if page in numbers:
            problems.append(f"{field} 页号重复：{page}")
            continue
        numbers.add(page)
        if page not in expected_pages:
            problems.append(f"{field} 页号 {page} 超出 1..{upper}")
        image_sha = evidence.get("image_sha256") if isinstance(evidence, dict) else None
        if not _is_sha256(image_sha):
            problems.append(
                f"{field} 第 {page} 页缺少或非法 image_sha256（需 64 位十六进制）"
            )
    return problems, numbers


def _vision_proof_problems(
    vision: dict[str, Any],
    *,
    parsed_source_sha256: str | None = None,
) -> list[str]:
    """校验 vision_first 结果是否真的逐页完成，而不是只有一个成功标记。"""

    problems: list[str] = []
    if vision.get("publishable") is not True:
        problems.append("视觉结果未明确 publishable=True")
    if vision.get("response_validated") is not True:
        problems.append("视觉结果未通过 response_validated")
    if vision.get("ok") is not True or vision.get("status") != "success":
        problems.append(
            f"视觉状态不是 success（status={vision.get('status')!r}, "
            f"ok={vision.get('ok')!r}）"
        )

    expected = _int_or_none(vision.get("pages_expected"))
    processed = _int_or_none(vision.get("pages_processed"))
    if expected is None or expected <= 0:
        problems.append("pages_expected 必须大于 0，0 页不能作为视觉证明")
        expected = 0
    if processed is None or processed != expected:
        problems.append(f"视觉页覆盖不足或页数不一致：{processed}/{expected}")
    expected_pages = set(range(1, expected + 1))

    proofs = vision.get("page_proofs")
    proof_cache_hits = 0
    proof_requests = 0
    if isinstance(proofs, list) and proofs:
        seen: set[int] = set()
        for index, proof in enumerate(proofs, start=1):
            if not isinstance(proof, dict):
                problems.append(f"page_proofs 第 {index} 项不是对象")
                continue
            page = _int_or_none(proof.get("page"))
            if page is None or page <= 0:
                problems.append(f"page_proofs 第 {index} 项缺少合法页号")
                continue
            if page in seen:
                problems.append(f"page_proofs 页号重复：{page}")
                continue
            seen.add(page)
            if page not in expected_pages:
                problems.append(f"page_proofs 页号 {page} 超出 1..{expected}")
            if not _is_sha256(proof.get("image_sha256")):
                problems.append(
                    f"page_proofs 第 {page} 页缺少或非法 image_sha256（需 64 位十六进制）"
                )
            if proof.get("validated") is not True:
                problems.append(f"page_proofs 第 {page} 页缺少 validated=True 响应证据")
            cache_hit = proof.get("cache_hit")
            if not isinstance(cache_hit, bool):
                problems.append(f"page_proofs 第 {page} 页 cache_hit 必须是布尔值")
            elif cache_hit:
                proof_cache_hits += 1
            else:
                proof_requests += 1
        missing = expected_pages - seen
        if missing:
            problems.append(f"page_proofs 缺少页号：{sorted(missing)[:5]}")
    elif "page_proofs" in vision:
        problems.append("page_proofs 必须是非空列表")
    else:
        evidence = vision.get("page_evidence")
        status_map = vision.get("page_status")
        evidence_problems, evidence_pages = _page_map_problems(
            evidence, "page_evidence", expected_pages
        )
        status_problems, status_pages = _page_map_problems(
            status_map, "page_status", expected_pages
        )
        problems.extend(evidence_problems)
        problems.extend(status_problems)
        for field, pages in (("page_evidence", evidence_pages), ("page_status", status_pages)):
            missing = expected_pages - pages
            if pages and missing:
                problems.append(f"{field} 缺少页号：{sorted(missing)[:5]}")
        if not evidence_pages:
            problems.append("缺少逐页视觉证据（page_proofs/page_evidence）")

    images_sent = _int_or_none(vision.get("images_sent"))
    cache_hits = _int_or_none(vision.get("cache_hits"))
    if images_sent is None or images_sent < 0:
        problems.append("images_sent 缺失或非法")
        images_sent = 0
    if cache_hits is None or cache_hits < 0:
        problems.append("cache_hits 缺失或非法")
        cache_hits = 0
    if expected > 0 and images_sent + cache_hits < expected:
        problems.append(
            f"images_sent={images_sent} 且 cache_hits={cache_hits} 合计未覆盖 {expected} 页，"
            "没有足够的实际请求或已验证缓存"
        )
    if proof_cache_hits > cache_hits:
        problems.append(
            f"page_proofs 有 {proof_cache_hits} 页声明 cache_hit，"
            f"但 cache_hits={cache_hits}"
        )
    if proof_requests > images_sent:
        problems.append(
            f"page_proofs 有 {proof_requests} 页声明非缓存，"
            f"但 images_sent={images_sent}"
        )

    declared_source = vision.get("source_sha256")
    if declared_source is not None:
        if not _is_sha256(declared_source):
            problems.append("视觉 source_sha256 不是 64 位十六进制")
        elif parsed_source_sha256 and declared_source.lower() != parsed_source_sha256.lower():
            problems.append("视觉 source_sha256 与本次解析源不一致，拒绝复用其他来源的视觉证明")

    conflicts = vision.get("unresolved_conflicts")
    if conflicts:
        problems.append(f"仍有未解决视觉冲突：{conflicts!r}")
    return problems


def _relation_scope_problem(identifier: str, parsed: ParsedDocument) -> str | None:
    prefix, separator, _ = identifier.partition("#")
    if not separator or "@" not in prefix:
        return None
    if prefix != f"{parsed.doc_id}@{parsed.version_id}":
        return f"跨 doc/version 引用 {prefix!r}"
    return None


def _header_relation_problems(
    *,
    continuation_of: str | None,
    source_id: str | None,
    source_page: int | None,
    current_table_id: str | None,
    table_by_id: dict[str, TableStructure],
    parsed: ParsedDocument,
) -> list[str]:
    if continuation_of is None and source_id is None and source_page is None:
        return []
    problems: list[str] = []
    if not continuation_of or continuation_of != continuation_of.strip():
        problems.append("continuation_of 为空或含首尾空白")
    if not source_id or source_id != source_id.strip():
        problems.append("header_source_table_id 为空或含首尾空白")
    if problems:
        return problems
    if source_page is not None and (
        isinstance(source_page, bool)
        or not isinstance(source_page, int)
        or source_page <= 0
    ):
        problems.append("header_source_page 不是正整数")
    if current_table_id and (
        continuation_of == current_table_id or source_id == current_table_id
    ):
        problems.append("续表来源指向当前表自身")
    for identifier in (continuation_of, source_id):
        scope_problem = _relation_scope_problem(identifier, parsed)
        if scope_problem:
            problems.append(scope_problem)
    precursor = table_by_id.get(continuation_of)
    source = table_by_id.get(source_id)
    if precursor is None:
        problems.append(f"前表 {continuation_of!r} 不在当前解析结构中")
    if source is None:
        problems.append(f"表头来源 {source_id!r} 不在当前解析结构中")
    if precursor is not None and precursor.kind == "layout":
        problems.append(f"前表 {continuation_of!r} 是 layout 表")
    if source is not None and source.kind == "layout":
        problems.append(f"表头来源 {source_id!r} 是 layout 表")
    if precursor is not None and source is not None:
        expected_source = precursor.header_source_table_id or precursor.table_id
        if expected_source != source_id:
            problems.append(
                f"表头来源 {source_id!r} 与前表链 {expected_source!r} 不一致"
            )
        actual_page = (
            source.header_source_page
            if source.header_source_page is not None
            else source.page
        )
        if (
            source_page is not None
            and actual_page is not None
            and source_page != actual_page
        ):
            problems.append(
                f"header_source_page={source_page} 与来源表实际页 {actual_page} 不一致"
            )
    return problems


def _collect_quality_findings(
    parsed: ParsedDocument,
    segment: SegmentResult,
    *,
    label: str,
    require_vision: bool,
) -> dict[str, list[str]]:
    """汇总解析/切块/结构告警，并做可审计的父子与表格完整性校验。"""

    buckets = _warning_buckets()
    messages = [
        *parsed.warnings,
        *segment.warnings,
    ]
    if parsed.structure is not None:
        messages.extend(parsed.structure.warnings)
        for page in parsed.structure.pages:
            messages.extend(page.warnings)
        for table in parsed.structure.tables:
            messages.extend(table.warnings)
        for asset in parsed.structure.assets:
            if asset.note and not asset.recognized:
                messages.append(asset.note)
    for message in messages:
        _add_warning(buckets, f"{label}: {message}")

    if require_vision and parsed.ingestion_mode != "vision_first":
        _add_warning(
            buckets,
            f"{label}: 正式门禁要求 vision_first，实际为 {parsed.ingestion_mode}",
            "fatal",
        )
    if parsed.ingestion_mode == "vision_first":
        vision = parsed.vision if isinstance(parsed.vision, dict) else {}
        for problem in _vision_proof_problems(
            vision, parsed_source_sha256=parsed.source_sha256
        ):
            _add_warning(buckets, f"{label}: {problem}", "fatal")

    structure = parsed.structure
    if structure is None:
        return buckets
    if structure.schema_version != STRUCTURE_SCHEMA_VERSION:
        _add_warning(
            buckets,
            f"{label}: 结构 schema 版本 {structure.schema_version} 与当前 "
            f"{STRUCTURE_SCHEMA_VERSION} 不一致，请重新解析后再正式构建",
            "fatal",
        )

    table_ids = [table.table_id for table in structure.tables]
    duplicates = sorted({table_id for table_id in table_ids if table_ids.count(table_id) > 1})
    if duplicates:
        _add_warning(buckets, f"{label}: table_id 重复 {duplicates[:5]}", "fatal")
    table_by_id = {table.table_id: table for table in structure.tables}

    parent_blocks = {
        article.article_id: article
        for article in segment.articles
        if article.block_kind == "table_parent"
    }
    for article in segment.articles:
        parent_id = article.parent_article_id
        if parent_id:
            parent = parent_blocks.get(parent_id)
            if parent is None:
                _add_warning(
                    buckets,
                    f"{label}: 子块 {article.article_id} 的父表 {parent_id} 未存储",
                    "fatal",
                )
            elif article.parent_table_id and parent.table_id != article.parent_table_id:
                _add_warning(
                    buckets,
                    f"{label}: 子块 {article.article_id} 与父表 table_id 不一致",
                    "fatal",
                )
        if article.source_cell_ids:
            table = table_by_id.get(article.table_id or "")
            if table is None:
                _add_warning(
                    buckets,
                    f"{label}: 行块 {article.article_id} 缺少可回查的表格结构",
                    "fatal",
                )
            else:
                cell_ids = {
                    cell.cell_id for row in table.rows for cell in row.cells
                }
                missing = sorted(set(article.source_cell_ids) - cell_ids)
                if missing:
                    _add_warning(
                        buckets,
                        f"{label}: 行块 {article.article_id} 引用了不存在的单元格 "
                        f"{missing[:5]}",
                        "fatal",
                    )
        if article.block_kind in {"table_row", "matrix_row"}:
            table = table_by_id.get(article.table_id or "")
            if table is None:
                _add_warning(
                    buckets,
                    f"{label}: 行块 {article.article_id} 找不到原表格结构，无法回溯单元格",
                    "fatal",
                )
            else:
                if not article.cell_provenance:
                    _add_warning(
                        buckets,
                        f"{label}: 行块 {article.article_id} 缺少单元格列坐标/跨度",
                        "review",
                    )
                else:
                    columns = [cell.col for cell in article.cell_provenance]
                    if columns != list(range(table.column_count)):
                        _add_warning(
                            buckets,
                            f"{label}: 行块 {article.article_id} 列坐标不完整或乱序，"
                            f"实际 {columns[:8]}，预期 0..{table.column_count - 1}",
                            "fatal",
                        )
                if article.header_paths and len(article.header_paths) != table.column_count:
                    _add_warning(
                        buckets,
                        f"{label}: 行块 {article.article_id} 表头路径数 "
                        f"{len(article.header_paths)} 与列数 {table.column_count} 不一致",
                        "review",
                    )

        if any(
            value is not None
            for value in (
                article.table_continuation_of,
                article.table_header_source_id,
                article.table_header_source_page,
            )
        ):
            if article.block_kind not in {"table_parent", "table_row", "matrix_row"}:
                _add_warning(
                    buckets,
                    f"{label}: 非表格块 {article.article_id} 携带续表头来源",
                    "fatal",
                )
            else:
                for problem in _header_relation_problems(
                    continuation_of=article.table_continuation_of,
                    source_id=article.table_header_source_id,
                    source_page=article.table_header_source_page,
                    current_table_id=article.table_id,
                    table_by_id=table_by_id,
                    parsed=parsed,
                ):
                    _add_warning(
                        buckets,
                        f"{label}: 块 {article.article_id} 续表头来源不可追溯：{problem}",
                        "fatal",
                    )

    for table in structure.tables:
        for problem in _header_relation_problems(
            continuation_of=table.continuation_of,
            source_id=table.header_source_table_id,
            source_page=table.header_source_page,
            current_table_id=table.table_id,
            table_by_id=table_by_id,
            parsed=parsed,
        ):
            _add_warning(
                buckets,
                f"{label}: 表格 {table.table_id} 续表头来源不可追溯：{problem}",
                "review",
            )
        if table.rows and table.column_count <= 0:
            _add_warning(buckets, f"{label}: 表格 {table.table_id} 列数为 0", "fatal")
        if table.row_count and table.row_count != len(table.rows):
            _add_warning(
                buckets,
                f"{label}: 表格 {table.table_id} row_count={table.row_count} 与实际 "
                f"{len(table.rows)} 行不一致",
                "review",
            )
        bad_headers = [
            index
            for index in table.header_row_indices
            if index < 0 or index >= len(table.rows)
        ]
        if bad_headers:
            _add_warning(
                buckets,
                f"{label}: 表格 {table.table_id} 表头行索引越界 {bad_headers}",
                "fatal",
            )
        if table.header_paths and len(table.header_paths) != table.column_count:
            _add_warning(
                buckets,
                f"{label}: 表格 {table.table_id} 表头路径数 {len(table.header_paths)} "
                f"与列数 {table.column_count} 不一致",
                "review",
            )
        for row in table.rows:
            if row.cells and len(row.cells) != table.column_count:
                _add_warning(
                    buckets,
                    f"{label}: 表格 {table.table_id} 第 {row.row_index + 1} 行有 "
                    f"{len(row.cells)} 格，列数 {table.column_count}",
                    "review",
                )
            for cell in row.cells:
                if (
                    cell.row < 0
                    or cell.row >= max(table.row_count, len(table.rows))
                    or cell.col < 0
                    or cell.col >= max(table.column_count, 1)
                ):
                    _add_warning(
                        buckets,
                        f"{label}: 表格 {table.table_id} 单元格 {cell.cell_id} 坐标越界 "
                        f"({cell.row},{cell.col})",
                        "fatal",
                    )
                if cell.needs_review:
                    _add_warning(
                        buckets,
                        f"{label}: 表格 {table.table_id} 单元格 {cell.cell_id} 需复核",
                        "review",
                    )
        if table.kind == "data" and table.rows and not table.header_rows:
            _add_warning(
                buckets,
                f"{label}: 表格 {table.table_id} 未识别到表头行",
                "review",
            )

    if parsed.format == "pdf":
        missing_text_pages = [
            page.page for page in structure.pages if page.needs_ocr
        ]
        if missing_text_pages and parsed.ingestion_mode != "vision_first":
            _add_warning(
                buckets,
                f"{label}: 第 {missing_text_pages[:5]} 页几乎没有文本层且未走 vision_first",
                "review",
            )
    return buckets


def _load_parse_report(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise BuildError(f"缺少本次解析报告：{path}；请先用 parse CLI 生成 parse_report.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"解析报告无法读取：{path}：{exc}") from exc
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise BuildError(f"解析报告缺少 results 列表：{path}")
    entries: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        doc_id = str(row.get("doc_id") or "")
        version_id = str(row.get("version_id") or "")
        if doc_id and version_id:
            entries[f"{doc_id}@{version_id}"] = row
    return entries


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

    for field_name in PAYLOAD_INTEGER_FIELDS:
        try:
            client.create_payload_index(
                collection_name=retrieval.collection,
                field_name=field_name,
                field_schema=PayloadSchemaType.INTEGER,
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
    metadata_organization_path: Path | None = None,
    metadata_raw_dir: Path | None = None,
    parsed_dir: Path,
    output_dir: Path,
    retrieval: RetrievalConfig | None = None,
    init_qdrant: bool = False,
    qdrant_client: QdrantClient | None = None,
    qdrant_url: str | None = None,
    manifest_path: Path | None = None,
    recreate_collection: bool = False,
    generated_at: datetime | None = None,
    expected_low_confidence_versions: int | None = None,
    require_vision: bool = False,
    parse_report_path: Path | None = None,
    strict: bool = False,
) -> BuildResult:
    if require_vision:
        strict = True
    if require_vision and parse_report_path is None:
        raise BuildError(
            "require_vision 正式构建必须提供本次 parse_report.json，"
            "否则无法排除旧 parsed 文件混入"
        )
    config: SourcesConfig = load_sources(sources_path)
    organization: OrganizationConfig | None = None
    if metadata_organization_path is not None:
        try:
            organization = load_organization(metadata_organization_path)
        except (OSError, ValueError) as exc:
            raise BuildError(
                f"组织注册表无法加载：{metadata_organization_path}：{exc}"
            ) from exc
    source_documents = {document.doc_id: document for document in config.documents}
    if organization is not None:
        # 延迟导入：未启用审计的调用方不依赖审计模块。
        from campusguide.ingest.metadata_source import audit_metadata_source
    else:
        audit_metadata_source = None

    articles: list[Article] = []
    documents: list[DocumentVersion] = []
    sources_manifest: list[dict[str, Any]] = []
    metadata_audits: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_versions: list[str] = []
    blocks_skipped = 0
    indexable_total = 0
    quality: dict[str, list[str]] = _warning_buckets()
    ingestion_modes: Counter[str] = Counter()
    report_entries = _load_parse_report(parse_report_path) if parse_report_path else None

    for doc in sorted(config.documents, key=lambda item: item.doc_id):
        for version in sorted(doc.versions, key=lambda item: item.version_id):
            label = f"{doc.doc_id}@{version.version_id}"
            parsed_path = parsed_dir / doc.doc_id / f"{version.version_id}.json"
            if not parsed_path.exists():
                missing_versions.append(label)
                message = f"缺少解析结果，跳过 {label}（期望 {parsed_path}）"
                warnings.append(message)
                _add_warning(quality, message, "fatal" if strict else "review")
                continue

            parsed = load_parsed_document(parsed_path)
            if parsed.doc_id != doc.doc_id or parsed.version_id != version.version_id:
                raise BuildError(
                    f"解析结果标识不匹配：{parsed_path} 为 "
                    f"{parsed.doc_id}@{parsed.version_id}，期望 {label}"
                )
            if report_entries is not None:
                entry = report_entries.get(label)
                if entry is None:
                    raise BuildError(
                        f"本次解析报告缺少 {label}，拒绝混用上次遗留的 parsed 文件"
                    )
                if entry.get("status") != "parsed":
                    raise BuildError(
                        f"{label} 在本次解析中状态为 {entry.get('status')}，"
                        f"不能进入构建：{entry.get('error') or '未提供错误详情'}"
                    )
                report_sha = str(entry.get("source_sha256") or "")
                if not report_sha or report_sha != str(parsed.source_sha256):
                    raise BuildError(
                        f"{label} 的 parsed.source_sha256 与本次解析报告不一致，"
                        "疑似旧文件混入，请重新运行 parse CLI"
                    )
                if require_vision and entry.get("ingestion_mode") != "vision_first":
                    raise BuildError(
                        f"{label} 未通过 vision_first，正式构建要求全部来源完成视觉流程"
                    )

            metadata_audit: dict[str, Any] | None = None
            metadata_status: MetadataSourceStatus = "not_checked"
            metadata_sha: str | None = None
            if audit_metadata_source is not None:
                metadata_audit = audit_metadata_source(
                    doc,
                    version,
                    parsed,
                    source_path=_metadata_audit_source_path(
                        doc,
                        version,
                        parsed,
                        metadata_raw_dir=metadata_raw_dir,
                        parsed_dir=parsed_dir,
                    ),
                    organization=organization,
                    source_documents=source_documents,
                )
                audit_status = metadata_audit.get("status")
                if audit_status == "failed":
                    codes = sorted(
                        {
                            str(item.get("code"))
                            for item in metadata_audit.get("fatal", [])
                        }
                    )
                    raise BuildError(
                        f"来源元数据审计失败，拒绝替换索引输出 {label}："
                        + ("、".join(codes) if codes else "未提供错误码")
                    )
                if audit_status not in {"verified", "review"}:
                    raise BuildError(
                        f"{label} 来源元数据审计返回未知状态 {audit_status!r}，拒绝构建"
                    )
                metadata_status = "verified" if audit_status == "verified" else "review"
                metadata_sha = _metadata_source_audit_sha256(metadata_audit)
                metadata_audits.append(metadata_audit)

            document_version = _make_document_version(
                doc,
                version,
                parsed,
                metadata_source_status=metadata_status,
                metadata_source_audit_sha256=metadata_sha,
            )
            segment = segment_document(
                parsed,
                document_version,
                topic_tags=doc.topic_tags,
                flow_ids=doc.flow_ids,
                redact_contacts=doc.redact_contacts,
            )
            indexable_articles = segment.indexable_articles
            # 父表块只作为 payload-only 点保存，不参与向量检索；这里仍须落盘
            # 供子行 hydration，只写 indexable_articles 会让 parent_article_id
            # 悬空。前言等既不可检索也没有子块的块仍然跳过，保持原语义。
            stored_articles = [
                article
                for article in segment.articles
                if article.is_indexable or article.block_kind == "table_parent"
            ]
            blocks_skipped += len(segment.articles) - len(stored_articles)
            indexable_total += len(indexable_articles)
            ingestion_modes[parsed.ingestion_mode] += 1
            findings = _collect_quality_findings(
                parsed,
                segment,
                label=label,
                require_vision=require_vision,
            )
            for level, items in findings.items():
                for item in items:
                    _add_warning(quality, item, level)
                    if item not in warnings:
                        warnings.append(item)
            structure = parsed.structure
            needs_review_cells = (
                sum(
                    1
                    for table in structure.tables
                    for row in table.rows
                    for cell in row.cells
                    if cell.needs_review
                )
                if structure is not None
                else 0
            )
            articles.extend(stored_articles)
            documents.append(document_version)
            sources_manifest.append(
                {
                    "doc_id": doc.doc_id,
                    "version_id": version.version_id,
                    "source_sha256": parsed.source_sha256,
                    "parser_version": parsed.parser_version,
                    "articles": len(indexable_articles),
                    "stored_articles": len(stored_articles),
                    "indexable_articles": len(indexable_articles),
                    "blocks": len(segment.articles) - len(indexable_articles),
                    "warnings": segment.warnings,
                    "warning_levels": findings,
                    "ingestion_mode": parsed.ingestion_mode,
                    "structure": {
                        "schema_version": structure.schema_version if structure else None,
                        "extractor_version": structure.extractor_version if structure else None,
                        "tables": len(structure.tables) if structure else 0,
                        "table_rows": (
                            sum(table.row_count for table in structure.tables)
                            if structure
                            else 0
                        ),
                        "needs_review_cells": needs_review_cells,
                        "vision": structure.vision if structure else None,
                    },
                    "metadata_source_audit": metadata_audit
                    or {"status": "not_checked"},
                    "vision": parsed.vision,
                }
            )

    articles.sort(key=_article_sort_key)
    documents.sort(key=lambda item: (item.doc_id, item.version_id))

    low_confidence_versions = [
        f"{document.doc_id}@{document.version_id}"
        for document in documents
        if document.date_confidence == "low"
    ]
    low_confidence_without_source = [
        f"{document.doc_id}@{document.version_id}"
        for document in documents
        if document.date_confidence == "low" and not document.date_source
    ]
    if low_confidence_without_source:
        raise BuildError(
            "以下低置信日期版本缺少 date_source，必须写明日期依据："
            + ", ".join(low_confidence_without_source[:10])
        )
    if (
        expected_low_confidence_versions is not None
        and len(low_confidence_versions) != expected_low_confidence_versions
    ):
        raise BuildError(
            "date_confidence=low 的版本数不符合审计基线："
            f"期望 {expected_low_confidence_versions}，实际 {len(low_confidence_versions)}；"
            "请核对 sources.yaml 的日期依据或更新审计清单"
        )

    keys = Counter((article.article_id, article.part_no) for article in articles)
    duplicates = sorted(key for key, count in keys.items() if count > 1)
    if duplicates:
        preview = ", ".join(f"{article_id}#part{part_no}" for article_id, part_no in duplicates[:5])
        raise BuildError(f"存在重复条款键（article_id + part_no）：{preview}")

    multi_part = sum(
        1 for count in Counter(article.article_id for article in articles).values() if count > 1
    )
    empty_text = sum(1 for article in articles if not article.text.strip())
    if strict and quality["fatal"]:
        preview = "；".join(quality["fatal"][:8])
        raise BuildError(
            f"严格质量门禁未通过（{len(quality['fatal'])} 项致命问题）：{preview}"
        )
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
            "articles": indexable_total,
            "stored_articles": len(articles),
            "indexable_articles": indexable_total,
            "articles_with_multiple_parts": multi_part,
            "blocks_excluded": blocks_skipped,
            "empty_text": empty_text,
            "missing_versions": len(missing_versions),
            "date_confidence_low": len(low_confidence_versions),
            "vision_first_sources": ingestion_modes.get("vision_first", 0),
            "native_only_sources": ingestion_modes.get("native_only", 0),
            "legacy_sources": ingestion_modes.get("legacy", 0),
            "fatal_warnings": len(quality["fatal"]),
            "review_warnings": len(quality["review"]),
        },
        "ingestion_modes": dict(sorted(ingestion_modes.items())),
        "quality_gates": {
            "strict": strict,
            "require_vision": require_vision,
            "parse_report_verified": report_entries is not None,
            "parse_report": str(parse_report_path) if parse_report_path else None,
            "fatal": quality["fatal"],
            "review": quality["review"],
            "info": quality["info"],
        },
        "metadata_source_audit": {
            "enabled": organization is not None,
            "organization_path": (
                str(metadata_organization_path)
                if metadata_organization_path is not None
                else None
            ),
            "versions": len(documents),
            "audited_versions": len(metadata_audits),
            "status_counts": {
                status: sum(
                    1
                    for document in documents
                    if document.metadata_source_status == status
                )
                for status in ("not_checked", "verified", "review", "failed")
            },
            "review_versions": sorted(
                f"{audit['doc_id']}@{audit['version_id']}"
                for audit in metadata_audits
                if audit.get("status") == "review"
            ),
            "schema_versions": sorted(
                {str(audit.get("schema_version")) for audit in metadata_audits}
            ),
        },
        "low_confidence_versions": low_confidence_versions,
        "sources": sources_manifest,
        "missing_versions": missing_versions,
        "warnings": warnings,
    }

    qdrant_warnings: list[str] = []
    if init_qdrant:
        if retrieval is None:
            raise BuildError("init_qdrant=True 时必须提供 retrieval 配置")
        client = qdrant_client or QdrantClient(
            url=qdrant_url or os.getenv("QDRANT_URL", "http://localhost:6333"),
            trust_env=qdrant_trust_env(),
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
        articles_written=indexable_total,
        stored_articles_written=len(articles),
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
