"""确定性来源元数据审计。

把 sources.yaml 的标题、班型、关系、日期和章节适用范围声明绑定到本次实际
源文件与解析结果。审计只做哈希、路径和字符串/结构比对，不调用模型，也不
修改索引；失败以结构化 fatal/review 记录返回，供后续构建批次接线。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from campusguide.config import ClassType, OrganizationConfig, SourceDocument, SourceVersion
from campusguide.domain.audience import AudienceDetail, AudienceScope
from campusguide.domain.enums import PolicyRelation
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.segment import MAJOR_HEADING_RE
from campusguide.ingest.structure import StructureBlock

AUDIT_SCHEMA_VERSION = "1.0"
ACQUISITION_FILE = "acquisition.json"
RAW_META_FILE = "meta.json"
REQUIRED_ACQUISITION_FIELDS = (
    "doc_id",
    "version_id",
    "source_url",
    "source_sha256",
    "fetched_at",
)
REQUIRED_RAW_META_FIELDS = (
    "doc_id",
    "version_id",
    "source_url",
    "source_sha256",
    "fetched_at",
)
RAW_META_VALUE_FIELDS = (
    "doc_id",
    "version_id",
    "source_url",
    "source_sha256",
    "fetched_at",
    "http_status",
    "byte_size",
    "local_path",
    "final_url",
    "source_file",
)
FORMAT_SUFFIXES = {
    "html": frozenset({".html", ".htm", ".shtml", ".psp"}),
    "pdf": frozenset({".pdf"}),
}
GENERAL_CLASS_SUFFIX = "-general"
GENERAL_CLASS_MARKER = "普通"
GENERAL_CLASS_PROGRAM_SUFFIXES = ("专业人才培养方案", "专业培养方案")
VERSION_SUFFIX_RE = re.compile(
    r"\s*(?:[（(]\s*(?:19|20)\d{2}\s*年?[^（）()]{0,16}[）)]"
    r"|(?:19|20)\d{2}\s*年?\s*(?:修订版|修订|版))\s*$"
)
# 来源说明常用“文件标题“…””之类的引述，引号内才是候选原文。
QUOTED_PHRASE_RES = (
    re.compile(r"“([^”\n]{1,300})”"),
    re.compile(r"\"([^\"\n]{1,300})\""),
    re.compile(r"「([^」\n]{1,300})」"),
    re.compile(r"『([^』\n]{1,300})』"),
    re.compile(r"‘([^’\n]{1,300})’"),
)
STATUS_VERIFIED = "verified"
STATUS_DECLARED = "declared_unverified"
STATUS_FATAL = "fatal"
STATUS_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class _SpanMatch:
    quote: str
    start: int
    end: int


@dataclass(frozen=True)
class _SourceMatch:
    quote: str
    location: str
    method: str
    page: int | None = None
    bbox: list[float] | None = None


@dataclass(frozen=True)
class _TitleCandidate:
    index: int
    block: StructureBlock
    text: str
    native_heading: bool

    @property
    def location(self) -> str:
        prefix = "heading" if self.native_heading else "paragraph_title"
        return f"{prefix}:{self.block.block_id}"


class _SourceText:
    """NFKC + 去空白后的文本，保留归一化位置到原文的映射以取原文引句。"""

    __slots__ = ("normalized", "offsets", "original")

    def __init__(self, original: str) -> None:
        self.original = original
        self.normalized, self.offsets = _normalize_with_offsets(original)

    def find(self, needle: str) -> _SpanMatch | None:
        normalized_needle = _normalize_text(needle)
        if not normalized_needle:
            return None
        position = self.normalized.find(normalized_needle)
        if position < 0:
            return None
        start = self.offsets[position]
        end = self.offsets[position + len(normalized_needle) - 1] + 1
        return _SpanMatch(quote=self.original[start:end], start=start, end=end)


def _normalize_with_offsets(value: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value):
        for normalized in unicodedata.normalize("NFKC", char):
            if normalized.isspace():
                continue
            chars.append(normalized)
            offsets.append(index)
    return "".join(chars), offsets


def _normalize_text(value: str) -> str:
    return _normalize_with_offsets(value)[0]


def _path_text(value: str | Path) -> str:
    return str(value).replace("\\", "/").rstrip("/")


def _paths_equivalent(first: str | Path, second: str | Path) -> bool:
    if _path_text(first) == _path_text(second):
        return True
    first_path = Path(first)
    second_path = Path(second)
    if not first_path.is_absolute():
        first_path = Path.cwd() / first_path
    if not second_path.is_absolute():
        second_path = Path.cwd() / second_path
    try:
        return first_path.resolve() == second_path.resolve()
    except OSError:
        return False


def _strip_version_suffix(title: str) -> str:
    stripped = title.strip()
    while stripped:
        candidate = VERSION_SUFFIX_RE.sub("", stripped).strip()
        if not candidate or candidate == stripped:
            return stripped
        stripped = candidate
    return stripped


def _parent_title_variants(title: str) -> list[str]:
    variants: list[str] = []
    for candidate in (_strip_version_suffix(title), title.strip()):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _class_tokens(class_type: ClassType) -> list[str]:
    tokens = [class_type.name, *class_type.aliases]
    return [token.strip() for token in dict.fromkeys(tokens) if token and token.strip()]


def _is_general_class(class_type: ClassType) -> bool:
    # 普通班只认班型自身名称中的“普通”标识，不从缺少创新班字样反推。
    return (
        class_type.class_id.endswith(GENERAL_CLASS_SUFFIX)
        or GENERAL_CLASS_MARKER in class_type.name
    )


def _general_class_phrases(
    class_type: ClassType,
    organization: OrganizationConfig,
) -> list[str]:
    if not _is_general_class(class_type):
        return []
    major_names = {
        major.major_id: major.name.strip()
        for major in organization.majors
        if major.name.strip()
    }
    phrases: list[str] = []
    for major_id in class_type.majors:
        major_name = major_names.get(major_id)
        if major_name is None:
            continue
        for suffix in GENERAL_CLASS_PROGRAM_SUFFIXES:
            phrase = f"{major_name}{suffix}"
            if phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _structure_blocks(parsed: ParsedDocument) -> list[StructureBlock]:
    if parsed.structure is None:
        return []
    return list(parsed.structure.blocks)


def _standalone_major_title(block: StructureBlock) -> str | None:
    """原生 PDF 可能把专业标题落成独立 paragraph，行级锚定后才可当标题。"""
    if block.kind != "paragraph":
        return None
    text = block.text.strip()
    if not text or "\n" in text:
        return None
    if MAJOR_HEADING_RE.fullmatch(text) is None:
        return None
    return text


def _title_candidates(parsed: ParsedDocument) -> list[_TitleCandidate]:
    candidates: list[_TitleCandidate] = []
    for index, block in enumerate(_structure_blocks(parsed)):
        text = block.text.strip()
        if not text:
            continue
        if block.kind == "heading":
            candidates.append(_TitleCandidate(index, block, text, True))
            continue
        standalone = _standalone_major_title(block)
        if standalone is not None:
            candidates.append(_TitleCandidate(index, block, standalone, False))
    return candidates


def _locator_extra(
    page: int | None,
    bbox: list[float] | None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if page is not None:
        extra["page"] = page
    if bbox is not None:
        extra["bbox"] = bbox
    return extra


def _quoted_phrases(value: str) -> list[str]:
    phrases: list[str] = []
    for pattern in QUOTED_PHRASE_RES:
        for match in pattern.finditer(value):
            phrase = match.group(1).strip()
            if phrase and phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _normalize_raw_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """把 ingest.fetch 的 meta.json 归一为 acquisition 字段口径。"""
    normalized = {
        field: meta.get(field)
        for field in RAW_META_VALUE_FIELDS
        if field != "source_sha256"
    }
    normalized["source_sha256"] = meta.get("sha256")
    return normalized


HTML_TITLE_SELECTORS = ("h1", ".article-title", ".article_title", ".title", "title")


def _html_title_texts(content: bytes) -> list[tuple[str, str]]:
    soup = BeautifulSoup(content, "lxml")
    found: list[tuple[str, str]] = []
    for selector in HTML_TITLE_SELECTORS:
        for element in soup.select(selector):
            text = element.get_text(" ", strip=True)
            if text:
                found.append((text, f"html.{selector.lstrip('.')}"))
    return found


def _find_actual_source_title(
    parsed: ParsedDocument,
    content: bytes | None,
    source_text: _SourceText,
) -> _SourceMatch | None:
    title = parsed.title.strip()
    if not title:
        return None
    if parsed.format == "html" and content is not None:
        for text, location in _html_title_texts(content):
            match = _find_in_text(
                text,
                title,
                location=location,
                method="html_title_element_match",
            )
            if match is not None:
                return match
    for candidate in _title_candidates(parsed):
        match = _find_in_text(
            candidate.text,
            title,
            location=candidate.location,
            method="structural_title_match",
            page=candidate.block.locator.page,
            bbox=candidate.block.locator.bbox,
        )
        if match is not None:
            return match
    match = source_text.find(title)
    if match is not None:
        return _SourceMatch(
            quote=match.quote,
            location="parsed.text",
            method="parsed_text_title_match",
        )
    return None


def _find_in_text(
    text: str,
    needle: str,
    *,
    location: str,
    method: str,
    page: int | None = None,
    bbox: list[float] | None = None,
) -> _SourceMatch | None:
    match = _SourceText(text).find(needle)
    if match is None:
        return None
    return _SourceMatch(
        quote=match.quote,
        location=location,
        method=method,
        page=page,
        bbox=bbox,
    )


def _find_in_headings(
    parsed: ParsedDocument,
    needle: str,
    *,
    method: str,
) -> _SourceMatch | None:
    for candidate in _title_candidates(parsed):
        match = _find_in_text(
            candidate.text,
            needle,
            location=candidate.location,
            method=method,
            page=candidate.block.locator.page,
            bbox=candidate.block.locator.bbox,
        )
        if match is not None:
            return match
    return None


def _find_in_blocks(
    blocks: list[StructureBlock],
    needle: str,
    *,
    method: str,
    section_id: str,
) -> _SourceMatch | None:
    for block in blocks:
        match = _find_in_text(
            block.text,
            needle,
            location=f"section:{section_id}:{block.block_id}",
            method=method,
            page=block.locator.page,
            bbox=block.locator.bbox,
        )
        if match is not None:
            return match
    return None


def _find_in_section_titles(
    section: list[StructureBlock],
    needle: str,
    *,
    method: str,
    section_id: str,
) -> _SourceMatch | None:
    for block in section:
        if block.kind != "heading" and _standalone_major_title(block) is None:
            continue
        match = _find_in_text(
            block.text.strip(),
            needle,
            location=f"section:{section_id}:{block.block_id}",
            method=method,
            page=block.locator.page,
            bbox=block.locator.bbox,
        )
        if match is not None:
            return match
    return None


def _find_scope_heading(
    parsed: ParsedDocument,
    match_text: str,
) -> tuple[_TitleCandidate | None, list[_TitleCandidate], list[StructureBlock]]:
    blocks = _structure_blocks(parsed)
    candidates = _title_candidates(parsed)
    normalized_match = _normalize_text(match_text)
    if not normalized_match:
        return None, candidates, blocks
    for candidate in candidates:
        if normalized_match in _normalize_text(candidate.text):
            return candidate, candidates, blocks
    return None, candidates, blocks


def _title_rank(candidate: _TitleCandidate) -> int | None:
    if candidate.native_heading:
        return candidate.block.level
    # 独立专业标题是合订本的第一级边界。
    return 1


def _section_blocks(
    blocks: list[StructureBlock],
    candidates: list[_TitleCandidate],
    heading_index: int,
) -> list[StructureBlock]:
    start = next((item for item in candidates if item.index == heading_index), None)
    if start is None:
        return [blocks[heading_index]] if 0 <= heading_index < len(blocks) else []
    end = len(blocks)
    start_rank = _title_rank(start)
    for candidate in candidates:
        if candidate.index <= heading_index:
            continue
        candidate_rank = _title_rank(candidate)
        if (
            start_rank is None
            or candidate_rank is None
            or candidate_rank <= start_rank
        ):
            end = candidate.index
            break
    return blocks[heading_index:end]


def _find_document_class_support(
    parsed: ParsedDocument,
    title_support: _SourceMatch | None,
    class_type: ClassType,
    organization: OrganizationConfig,
) -> _SourceMatch | None:
    if _is_general_class(class_type):
        # 普通班必须由“专业名称+培养方案”源标题/结构标题短语证明，
        # 班型名称单独出现或缺少创新班字样都不足以判定。
        for phrase in _general_class_phrases(class_type, organization):
            if title_support is not None:
                match = _find_in_text(
                    title_support.quote,
                    phrase,
                    location=title_support.location,
                    method="general_class_title_phrase",
                    page=title_support.page,
                    bbox=title_support.bbox,
                )
                if match is not None:
                    return match
            match = _find_in_headings(parsed, phrase, method="general_class_heading_phrase")
            if match is not None:
                return match
        return None
    # 班型适用性只由实际源标题或识别出的源标题块证明；正文顺带一提不算。
    for token in _class_tokens(class_type):
        if title_support is not None:
            match = _find_in_text(
                title_support.quote,
                token,
                location=title_support.location,
                method="title_token_match",
                page=title_support.page,
                bbox=title_support.bbox,
            )
            if match is not None:
                return match
        match = _find_in_headings(parsed, token, method="heading_token_match")
        if match is not None:
            return match
    return None


def _find_scope_class_support(
    section: list[StructureBlock],
    class_type: ClassType,
    organization: OrganizationConfig,
) -> _SourceMatch | None:
    if not section:
        return None
    section_id = section[0].block_id
    if _is_general_class(class_type):
        for phrase in _general_class_phrases(class_type, organization):
            match = _find_in_section_titles(
                section,
                phrase,
                method="general_class_scope_heading_phrase",
                section_id=section_id,
            )
            if match is not None:
                return match
        return None
    for token in _class_tokens(class_type):
        match = _find_in_section_titles(
            section,
            token,
            method="scope_section_heading_token_match",
            section_id=section_id,
        )
        if match is not None:
            return match
    return None


def audit_metadata_source(
    doc: SourceDocument,
    version: SourceVersion,
    parsed: ParsedDocument,
    *,
    source_path: Path,
    organization: OrganizationConfig,
    source_documents: dict[str, SourceDocument],
) -> dict[str, Any]:
    """审计单个文档版本的来源元数据，返回 JSON 可序列化字典。"""
    fatal: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    source_url = str(version.url) if version.url is not None else None
    parsed_sha = (parsed.source_sha256 or "").lower()
    actual_sha: str | None = None
    content: bytes | None = None

    def add_issue(
        bucket: list[dict[str, Any]],
        severity: str,
        code: str,
        field: str,
        detail: str,
    ) -> None:
        bucket.append(
            {
                "code": code,
                "severity": severity,
                "field": field,
                "detail": detail,
                "doc_id": doc.doc_id,
                "version_id": version.version_id,
            }
        )

    def fatal_issue(code: str, field: str, detail: str) -> None:
        add_issue(fatal, "fatal", code, field, detail)

    def review_issue(code: str, field: str, detail: str) -> None:
        add_issue(review, "review", code, field, detail)

    def add_evidence(
        field: str,
        value: Any,
        status: str,
        method: str,
        *,
        quote: str | None = None,
        location: str | None = None,
        **extra: Any,
    ) -> None:
        record: dict[str, Any] = {
            "field": field,
            "value": value,
            "status": status,
            "method": method,
            "quote": quote,
            "location": location,
            "doc_id": doc.doc_id,
            "version_id": version.version_id,
            "source_sha256": actual_sha,
            "source_url": source_url,
        }
        record.update(extra)
        evidence.append(record)

    if source_url is None:
        fatal_issue("source_url_missing", "version.url", "版本缺少引用 URL，无法绑定来源")

    try:
        if not source_path.is_file():
            raise OSError("路径不是文件")
        content = source_path.read_bytes()
    except OSError as exc:
        fatal_issue("source_unreadable", "source_path", f"无法读取实际源文件 {source_path}: {exc}")
    if content is not None:
        actual_sha = hashlib.sha256(content).hexdigest()

    if parsed.doc_id != doc.doc_id:
        fatal_issue(
            "parsed_document_identity_mismatch",
            "parsed.doc_id",
            f"解析结果 doc_id={parsed.doc_id!r} 与配置 {doc.doc_id!r} 不一致",
        )
    if parsed.version_id != version.version_id:
        fatal_issue(
            "parsed_document_identity_mismatch",
            "parsed.version_id",
            f"解析结果 version_id={parsed.version_id!r} 与配置 {version.version_id!r} 不一致",
        )
    configured_version = next(
        (item for item in doc.versions if item.version_id == version.version_id),
        None,
    )
    if configured_version is None:
        fatal_issue(
            "version_not_in_document",
            "version.version_id",
            f"{version.version_id} 不在 {doc.doc_id} 的 versions 中",
        )
    elif configured_version != version:
        fatal_issue(
            "version_binding_mismatch",
            "version",
            "传入版本与文档 versions 中的同名版本字段不一致",
        )

    expected_suffixes = FORMAT_SUFFIXES.get(parsed.format, frozenset())
    suffix_ok = source_path.suffix.lower() in expected_suffixes
    configured_format_ok = version.format == "auto" or version.format == parsed.format
    if not suffix_ok:
        fatal_issue(
            "source_format_mismatch",
            "source_path",
            f"源文件扩展名 {source_path.suffix!r} 与解析格式 {parsed.format!r} 不一致",
        )
    if not configured_format_ok:
        fatal_issue(
            "version_format_mismatch",
            "version.format",
            f"配置格式 {version.format!r} 与解析格式 {parsed.format!r} 不一致",
        )
    add_evidence(
        "source.format",
        parsed.format,
        STATUS_VERIFIED if suffix_ok and configured_format_ok else STATUS_FATAL,
        "path_suffix+parsed_format",
        location=str(source_path),
        configured_format=version.format,
    )

    if content is not None:
        hash_ok = actual_sha == parsed_sha
        if not hash_ok:
            fatal_issue(
                "source_sha256_mismatch",
                "source.sha256",
                f"实际文件 SHA-256 {actual_sha} 与 parsed.source_sha256 {parsed.source_sha256} 不一致",
            )
        add_evidence(
            "source.sha256",
            actual_sha,
            STATUS_VERIFIED if hash_ok else STATUS_FATAL,
            "sha256_file_bytes",
            location=str(source_path),
            parsed_sha256=parsed.source_sha256,
            byte_size=len(content),
        )

    acquisition_status = "missing"
    acquisition: dict[str, Any] | None = None
    provenance_path: Path | None = None
    provenance_kind: str | None = None
    provenance_method: str | None = None
    sidecar_path = source_path.parent / ACQUISITION_FILE
    raw_meta_path = source_path.parent / RAW_META_FILE
    if sidecar_path.exists():
        # 显式 acquisition.json 优先；即使内容损坏也绝不回退到 meta.json。
        provenance_path = sidecar_path
        provenance_kind = "acquisition"
        provenance_method = "acquisition_sidecar"
    elif raw_meta_path.exists():
        provenance_path = raw_meta_path
        provenance_kind = "raw_meta"
        provenance_method = "raw_fetch_meta"
    else:
        review_issue(
            "acquisition_sidecar_missing",
            "source.acquisition",
            f"缺少 {ACQUISITION_FILE} 与 {RAW_META_FILE}，URL/SHA/抓取时间只有文件哈希可核对",
        )
        add_evidence(
            "source.provenance",
            None,
            STATUS_DECLARED,
            "acquisition_sidecar_missing",
            location=str(sidecar_path),
            parsed_sha256=parsed.source_sha256,
        )

    if provenance_path is not None:
        is_raw_meta = provenance_kind == "raw_meta"
        record_prefix = "raw_meta" if is_raw_meta else "acquisition"
        field_prefix = f"source.{record_prefix}"
        try:
            loaded = json.loads(provenance_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{provenance_path.name} 顶层必须是对象")
        except (OSError, ValueError) as exc:
            acquisition_status = "malformed"
            if is_raw_meta:
                provenance_method = "raw_meta_unreadable"
            fatal_issue(
                "raw_meta_unreadable" if is_raw_meta else "acquisition_sidecar_unreadable",
                field_prefix,
                f"无法读取来源旁车 {provenance_path}: {exc}",
            )
            add_evidence(
                "source.provenance",
                None,
                STATUS_FATAL,
                provenance_method or field_prefix,
                location=str(provenance_path),
            )
        else:
            acquisition = _normalize_raw_meta(loaded) if is_raw_meta else loaded
            required_fields = (
                REQUIRED_RAW_META_FIELDS if is_raw_meta else REQUIRED_ACQUISITION_FIELDS
            )
            acquisition_ok = True
            missing_fields = [
                field
                for field in required_fields
                if not str(acquisition.get(field) or "").strip()
            ]
            if missing_fields:
                acquisition_ok = False
                acquisition_status = "incomplete"
                fatal_issue(
                    "raw_meta_incomplete"
                    if is_raw_meta
                    else "acquisition_sidecar_incomplete",
                    field_prefix,
                    f"{provenance_path.name} 缺少字段：{','.join(missing_fields)}",
                )

            def provenance_failure(code: str, field: str, detail: str) -> None:
                nonlocal acquisition_ok, acquisition_status
                acquisition_ok = False
                if acquisition_status != "incomplete":
                    acquisition_status = "mismatch"
                fatal_issue(code, field, detail)

            if actual_sha is None:
                provenance_failure(
                    f"{record_prefix}_source_unreadable",
                    field_prefix,
                    "实际源文件不可读，无法核对来源记录的 SHA 与字节数绑定",
                )

            if acquisition.get("doc_id") != doc.doc_id:
                provenance_failure(
                    f"{record_prefix}_identity_mismatch",
                    f"{field_prefix}.doc_id",
                    f"来源记录 doc_id={acquisition.get('doc_id')!r} 与配置 {doc.doc_id!r} 不一致",
                )
            if acquisition.get("version_id") != version.version_id:
                provenance_failure(
                    f"{record_prefix}_identity_mismatch",
                    f"{field_prefix}.version_id",
                    f"来源记录 version_id={acquisition.get('version_id')!r} "
                    f"与配置 {version.version_id!r} 不一致",
                )
            if str(acquisition.get("source_url") or "") != (source_url or ""):
                provenance_failure(
                    f"{record_prefix}_url_mismatch",
                    f"{field_prefix}.source_url",
                    f"来源记录 source_url={acquisition.get('source_url')!r} "
                    f"与配置 {source_url!r} 不一致",
                )
            if actual_sha is not None:
                record_sha = str(acquisition.get("source_sha256") or "").lower()
                if record_sha != actual_sha:
                    provenance_failure(
                        f"{record_prefix}_sha256_mismatch",
                        f"{field_prefix}.source_sha256",
                        f"来源记录 source_sha256={record_sha} 与实际文件 {actual_sha} 不一致",
                    )
            fetched_at = str(acquisition.get("fetched_at") or "")
            if fetched_at:
                try:
                    datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                except ValueError:
                    provenance_failure(
                        f"{record_prefix}_fetched_at_invalid",
                        f"{field_prefix}.fetched_at",
                        f"来源记录 fetched_at 不是 ISO 时间：{fetched_at!r}",
                    )

            http_status = acquisition.get("http_status")
            local_copy = False
            unlabeled = False
            if is_raw_meta:
                if http_status == 200:
                    provenance_method = "raw_fetch_meta"
                elif http_status is None and str(
                    acquisition.get("local_path") or ""
                ).strip():
                    provenance_method = "raw_meta_local_copy"
                    local_copy = True
                elif http_status is None:
                    provenance_method = "raw_meta_method_unlabeled"
                    unlabeled = True
                else:
                    provenance_method = "raw_meta_http_status"
                    provenance_failure(
                        "raw_meta_http_status",
                        f"{field_prefix}.http_status",
                        f"raw meta http_status={http_status!r} 不是 200",
                    )
            elif http_status is not None and http_status != 200:
                provenance_failure(
                    "acquisition_http_status",
                    f"{field_prefix}.http_status",
                    f"acquisition http_status={http_status!r} 不是 200",
                )

            byte_size = acquisition.get("byte_size")
            if byte_size is not None and content is not None and byte_size != len(content):
                provenance_failure(
                    f"{record_prefix}_byte_size_mismatch",
                    f"{field_prefix}.byte_size",
                    f"来源记录 byte_size={byte_size!r} 与实际 {len(content)} 不一致",
                )
            record_local_path = acquisition.get("local_path")
            if (
                record_local_path
                and version.local_path
                and not _paths_equivalent(str(record_local_path), version.local_path)
            ):
                provenance_failure(
                    f"{record_prefix}_local_path_mismatch",
                    f"{field_prefix}.local_path",
                    f"来源记录 local_path={record_local_path!r} "
                    f"与配置 {version.local_path!r} 不一致",
                )

            if is_raw_meta:
                source_file = str(acquisition.get("source_file") or "").strip()
                if source_file:
                    source_file_path = Path(source_file)
                    if source_file_path.is_absolute() or ".." in source_file_path.parts:
                        provenance_failure(
                            "raw_meta_source_file_unsafe",
                            f"{field_prefix}.source_file",
                            f"raw meta source_file={source_file!r} 是绝对路径或包含上级目录",
                        )
                    elif not _paths_equivalent(
                        source_path.parent / source_file_path, source_path
                    ):
                        provenance_failure(
                            "raw_meta_source_file_mismatch",
                            f"{field_prefix}.source_file",
                            f"raw meta source_file={source_file!r} "
                            f"未绑定实际源文件 {str(source_path)!r}",
                        )

            if not acquisition_ok:
                provenance_status = STATUS_FATAL
            elif local_copy or unlabeled:
                provenance_status = STATUS_DECLARED
            else:
                provenance_status = STATUS_VERIFIED

            if acquisition_ok and local_copy:
                acquisition_status = "verified_local_copy"
                review_issue(
                    "raw_meta_local_copy",
                    "source.provenance",
                    "raw meta 记录为本地文件复制（http_status=None 且有 local_path），"
                    "只验证本地副本，官网原始抓取仍是声明值",
                )
            elif acquisition_ok and unlabeled:
                acquisition_status = "unlabeled"
                review_issue(
                    "raw_meta_acquisition_method_unlabeled",
                    "source.provenance",
                    "raw meta 缺少 http_status 与 local_path，无法确认抓取方式",
                )
            elif acquisition_ok:
                acquisition_status = "verified"

            provenance_fields = (
                RAW_META_VALUE_FIELDS if is_raw_meta else REQUIRED_ACQUISITION_FIELDS
            )
            provenance_value = {
                field: acquisition.get(field) for field in provenance_fields
            }
            add_evidence(
                "source.provenance",
                provenance_value,
                provenance_status,
                provenance_method or record_prefix,
                location=str(provenance_path),
                parsed_sha256=parsed.source_sha256,
            )

    if source_url is not None:
        if parsed.source_url is None:
            review_issue(
                "parsed_source_url_missing",
                "parsed.source_url",
                "解析结果缺少 source_url，无法与配置引用链接核对",
            )
            add_evidence(
                "source.url",
                None,
                STATUS_DECLARED,
                "parsed_source_url_missing",
                location="parsed.source_url",
            )
        elif parsed.source_url == source_url:
            add_evidence(
                "source.url",
                source_url,
                STATUS_VERIFIED,
                "parsed_source_url_exact",
                location="parsed.source_url",
            )
        else:
            review_issue(
                "parsed_source_url_differs",
                "parsed.source_url",
                f"解析 source_url={parsed.source_url!r} 与配置 {source_url!r} 不一致（可能为重定向）",
            )
            add_evidence(
                "source.url",
                parsed.source_url,
                STATUS_DECLARED,
                "parsed_source_url_differs",
                location="parsed.source_url",
                configured_url=source_url,
            )

    if version.local_path is not None:
        add_evidence(
            "version.local_path",
            version.local_path,
            STATUS_DECLARED,
            "config_declaration",
            location="sources.yaml",
        )
    if parsed.local_path is None:
        # 抓取前的本地输入是可选项；远端来源没有 local_path 属于正常情况。
        # 实际来源绑定由 source_path 字节 SHA 与构建接线的 raw meta 校验证明。
        add_evidence(
            "parsed.local_path",
            None,
            STATUS_NOT_APPLICABLE,
            "local_input_absent",
            location="parsed.local_path",
            actual_source_path=str(source_path),
            note="抓取前本地输入可选；不把 parsed.local_path 当作实际来源绑定",
        )
    else:
        path_matches_config = version.local_path is not None and _paths_equivalent(
            parsed.local_path, version.local_path
        )
        if version.local_path is not None and not path_matches_config:
            review_issue(
                "parsed_local_path_differs",
                "parsed.local_path",
                f"解析 local_path={parsed.local_path!r} 与配置 {version.local_path!r} 不一致",
            )
        add_evidence(
            "parsed.local_path",
            parsed.local_path,
            STATUS_DECLARED,
            "parsed_local_path_declaration"
            if path_matches_config
            else "declared_local_input",
            location="parsed.local_path",
            actual_source_path=str(source_path),
            configured_local_path=version.local_path,
            note="本地输入声明，不是实际来源绑定",
        )

    source_text = _SourceText(parsed.text)
    add_evidence(
        "title",
        doc.title,
        STATUS_DECLARED,
        "config_declaration",
        location="sources.yaml",
    )
    source_title = parsed.title.strip()
    title_support = (
        _find_actual_source_title(parsed, content, source_text) if source_title else None
    )
    if not source_title:
        review_issue("source_title_missing", "parsed.title", "解析结果缺少源标题")
        add_evidence(
            "title.source",
            None,
            STATUS_DECLARED,
            "parsed_title_missing",
            location="parsed.title",
        )
    elif title_support is None:
        review_issue(
            "parsed_title_not_in_source",
            "parsed.title",
            f"解析标题 {source_title!r} 未出现在实际源文件标题/结构/正文中，"
            "不能作为来源或班型依据",
        )
        if _normalize_text(doc.title) != _normalize_text(source_title):
            review_issue(
                "title_mismatch",
                "title",
                f"配置标题 {doc.title!r} 与解析源标题 {source_title!r} 不一致",
            )
        add_evidence(
            "title.source",
            source_title,
            STATUS_DECLARED,
            "parsed_title_not_in_source",
            location="parsed.title",
        )
    elif _normalize_text(doc.title) == _normalize_text(source_title):
        add_evidence(
            "title.source",
            source_title,
            STATUS_VERIFIED,
            title_support.method,
            quote=title_support.quote,
            location=title_support.location,
            **_locator_extra(title_support.page, title_support.bbox),
        )
    else:
        review_issue(
            "title_mismatch",
            "title",
            f"配置标题 {doc.title!r} 与解析源标题 {source_title!r} 不一致",
        )
        add_evidence(
            "title.source",
            source_title,
            STATUS_DECLARED,
            "parsed_title_differs",
            quote=title_support.quote,
            location=title_support.location,
            **_locator_extra(title_support.page, title_support.bbox),
        )

    effective_detail = (
        version.audience_detail
        if version.audience_detail is not None
        else doc.audience_detail
    )
    add_evidence(
        "version.date_source",
        version.date_source,
        STATUS_DECLARED,
        "config_declaration",
        location="sources.yaml",
        confidence=version.date_confidence,
    )
    add_evidence(
        "version.date_confidence",
        version.date_confidence,
        STATUS_DECLARED,
        "config_declaration",
        location="sources.yaml",
    )
    if effective_detail is None:
        add_evidence(
            "audience.scope_status",
            None,
            STATUS_NOT_APPLICABLE,
            "config_declaration",
            location="sources.yaml",
        )
    else:
        scope_extra: dict[str, Any] = {
            "has_scope_constraints": effective_detail.has_scope_constraints,
        }
        if (
            effective_detail.scope_status.value == "unknown"
            and effective_detail.has_scope_constraints
        ):
            scope_extra["note"] = "配置字段存在不自动升级为 exact"
        add_evidence(
            "audience.scope_status",
            effective_detail.scope_status.value,
            STATUS_DECLARED,
            "config_declaration",
            location="sources.yaml",
            **scope_extra,
        )
        if effective_detail.scope_evidence:
            scope_evidence = effective_detail.scope_evidence
            add_evidence(
                "audience.scope_evidence",
                scope_evidence,
                STATUS_DECLARED,
                "config_declaration",
                location="sources.yaml",
            )
            literal_match = source_text.find(scope_evidence)
            if literal_match is not None:
                add_evidence(
                    "audience.scope_evidence.source",
                    scope_evidence,
                    STATUS_VERIFIED,
                    "parsed_text_substring",
                    quote=literal_match.quote,
                    location="parsed.text",
                )
            else:
                phrases = _quoted_phrases(scope_evidence)
                matches: list[_SourceMatch] = []
                unmatched: list[str] = []
                for phrase in phrases:
                    found = source_text.find(phrase)
                    if found is None:
                        unmatched.append(phrase)
                    else:
                        matches.append(found)
                if matches:
                    add_evidence(
                        "audience.scope_evidence.source",
                        scope_evidence,
                        STATUS_VERIFIED,
                        "scope_evidence_quote_substring",
                        quote=matches[0].quote,
                        location="parsed.text",
                        matched_quotes=[item.quote for item in matches],
                        unmatched_quotes=unmatched,
                    )
                elif phrases:
                    review_issue(
                        "scope_evidence_description_unverified",
                        "audience.scope_evidence",
                        "audience_detail.scope_evidence 为说明性文字，"
                        "引号内短语未在解析正文找到",
                    )
                    add_evidence(
                        "audience.scope_evidence.source",
                        scope_evidence,
                        STATUS_DECLARED,
                        "scope_evidence_description_unverified",
                        location="parsed.text",
                        unmatched_quotes=unmatched,
                    )
                else:
                    fatal_issue(
                        "scope_evidence_not_in_source",
                        "audience.scope_evidence",
                        "audience_detail.scope_evidence 不是解析正文的真实子串",
                    )
                    add_evidence(
                        "audience.scope_evidence.source",
                        scope_evidence,
                        STATUS_FATAL,
                        "parsed_text_substring",
                        location="parsed.text",
                    )

    registry = {class_type.class_id: class_type for class_type in organization.class_types}

    def audit_detail_classes(detail: AudienceDetail | None, prefix: str) -> None:
        if detail is None:
            return
        if not detail.class_types:
            add_evidence(
                f"{prefix}.class_types",
                [],
                STATUS_NOT_APPLICABLE,
                "config_declaration",
                location="sources.yaml",
                note="空班型列表表示共享/未知，审计不自动标注",
            )
            return
        for index, class_id in enumerate(detail.class_types):
            field = f"{prefix}.class_types[{index}]"
            add_evidence(
                field,
                class_id,
                STATUS_DECLARED,
                "config_declaration",
                location="sources.yaml",
            )
            class_type = registry.get(class_id)
            if class_type is None:
                fatal_issue(
                    "unknown_class_type",
                    field,
                    f"班型 {class_id} 不在 organization.class_types 注册表",
                )
                add_evidence(
                    f"{field}.source_support",
                    class_id,
                    STATUS_FATAL,
                    "registry_lookup",
                    location="organization.yaml",
                )
                continue
            support = _find_document_class_support(
                parsed,
                title_support,
                class_type,
                organization,
            )
            if support is None:
                fatal_issue(
                    "class_type_source_missing",
                    field,
                    f"班型 {class_id} 在源标题/结构标题/正文中没有名称或别名依据",
                )
                add_evidence(
                    f"{field}.source_support",
                    class_id,
                    STATUS_FATAL,
                    "source_token_search",
                    location="parsed",
                )
            else:
                add_evidence(
                    f"{field}.source_support",
                    class_id,
                    STATUS_VERIFIED,
                    support.method,
                    quote=support.quote,
                    location=support.location,
                    **_locator_extra(support.page, support.bbox),
                )

    def audit_scope(scope: AudienceScope, index: int) -> None:
        field = f"audience_scopes[{index}]"
        add_evidence(
            f"{field}.match",
            scope.match,
            STATUS_DECLARED,
            "config_declaration",
            location="sources.yaml",
        )
        heading, candidate_blocks, blocks = _find_scope_heading(parsed, scope.match)
        section: list[StructureBlock] = []
        section_id = ""
        if heading is None:
            fatal_issue(
                "scope_match_not_in_headings",
                f"{field}.match",
                f"章节匹配文本未出现在任何结构标题：{scope.match!r}",
            )
            add_evidence(
                f"{field}.match.source",
                scope.match,
                STATUS_FATAL,
                "heading_search",
                location="parsed.structure.headings",
            )
        else:
            heading_match = _SourceText(heading.text).find(scope.match)
            if heading_match is None:
                fatal_issue(
                    "scope_match_quote_unavailable",
                    f"{field}.match",
                    "结构标题包含匹配文本，但无法提取原文引句",
                )
                add_evidence(
                    f"{field}.match.source",
                    scope.match,
                    STATUS_FATAL,
                    "heading_contains_match",
                    location=heading.location,
                )
            else:
                add_evidence(
                    f"{field}.match.source",
                    scope.match,
                    STATUS_VERIFIED,
                    "heading_contains_match",
                    quote=heading_match.quote,
                    location=heading.location,
                    **_locator_extra(
                        heading.block.locator.page,
                        heading.block.locator.bbox,
                    ),
                )
            section = _section_blocks(blocks, candidate_blocks, heading.index)
            section_id = heading.block.block_id

        if not section:
            add_evidence(
                f"{field}.evidence",
                scope.evidence,
                STATUS_FATAL,
                "scope_section_unavailable",
                location="parsed.structure",
            )
        else:
            scope_evidence_match = _find_in_blocks(
                section,
                scope.evidence,
                method="scope_section_substring",
                section_id=section_id,
            )
            if scope_evidence_match is not None:
                add_evidence(
                    f"{field}.evidence",
                    scope.evidence,
                    STATUS_VERIFIED,
                    scope_evidence_match.method,
                    quote=scope_evidence_match.quote,
                    location=scope_evidence_match.location,
                    **_locator_extra(
                        scope_evidence_match.page,
                        scope_evidence_match.bbox,
                    ),
                )
            else:
                phrases = _quoted_phrases(scope.evidence)
                if not phrases:
                    global_match = source_text.find(scope.evidence)
                    if global_match is None:
                        fatal_issue(
                            "scope_evidence_not_in_source",
                            f"{field}.evidence",
                            "章节原文依据不是解析正文的真实子串",
                        )
                        add_evidence(
                            f"{field}.evidence",
                            scope.evidence,
                            STATUS_FATAL,
                            "scope_section_substring",
                            location="parsed.text",
                        )
                    else:
                        fatal_issue(
                            "scope_evidence_outside_section",
                            f"{field}.evidence",
                            "章节原文依据出现在其他章节，未在匹配标题的章节内找到",
                        )
                        add_evidence(
                            f"{field}.evidence",
                            scope.evidence,
                            STATUS_FATAL,
                            "scope_section_substring",
                            quote=global_match.quote,
                            location="parsed.text",
                        )
                else:
                    in_section: list[_SourceMatch] = []
                    outside: list[_SourceMatch] = []
                    missing: list[str] = []
                    for phrase in phrases:
                        found = _find_in_blocks(
                            section,
                            phrase,
                            method="scope_section_quote_substring",
                            section_id=section_id,
                        )
                        if found is not None:
                            in_section.append(found)
                            continue
                        outside_match = source_text.find(phrase)
                        if outside_match is None:
                            missing.append(phrase)
                        else:
                            outside.append(outside_match)
                    if in_section:
                        primary = in_section[0]
                        add_evidence(
                            f"{field}.evidence",
                            scope.evidence,
                            STATUS_VERIFIED,
                            "scope_section_quote_substring",
                            quote=primary.quote,
                            location=primary.location,
                            **_locator_extra(primary.page, primary.bbox),
                            matched_quotes=[item.quote for item in in_section],
                            outside_quotes=[item.quote for item in outside],
                            unmatched_quotes=missing,
                        )
                        if outside:
                            fatal_issue(
                                "scope_evidence_outside_section",
                                f"{field}.evidence",
                                "章节依据的引号短语出现在其他章节",
                            )
                            add_evidence(
                                f"{field}.evidence.outside",
                                scope.evidence,
                                STATUS_FATAL,
                                "scope_section_quote_substring",
                                quote=outside[0].quote,
                                location="parsed.text",
                            )
                    elif outside:
                        fatal_issue(
                            "scope_evidence_outside_section",
                            f"{field}.evidence",
                            "章节依据的引号短语出现在其他章节",
                        )
                        add_evidence(
                            f"{field}.evidence",
                            scope.evidence,
                            STATUS_FATAL,
                            "scope_section_quote_substring",
                            quote=outside[0].quote,
                            location="parsed.text",
                        )
                    else:
                        review_issue(
                            "scope_evidence_description_unverified",
                            f"{field}.evidence",
                            "章节依据为说明性文字，引号内短语未在解析正文找到",
                        )
                        add_evidence(
                            f"{field}.evidence",
                            scope.evidence,
                            STATUS_DECLARED,
                            "scope_section_quote_substring",
                            location="parsed.text",
                            unmatched_quotes=missing,
                        )

        if not scope.class_types:
            add_evidence(
                f"{field}.class_types",
                [],
                STATUS_NOT_APPLICABLE,
                "config_declaration",
                location="sources.yaml",
                note="空班型列表表示共享/未知，审计不自动标注",
            )
            return
        for class_index, class_id in enumerate(scope.class_types):
            class_field = f"{field}.class_types[{class_index}]"
            add_evidence(
                class_field,
                class_id,
                STATUS_DECLARED,
                "config_declaration",
                location="sources.yaml",
            )
            class_type = registry.get(class_id)
            if class_type is None:
                fatal_issue(
                    "unknown_class_type",
                    class_field,
                    f"班型 {class_id} 不在 organization.class_types 注册表",
                )
                add_evidence(
                    f"{class_field}.source_support",
                    class_id,
                    STATUS_FATAL,
                    "registry_lookup",
                    location="organization.yaml",
                )
                continue
            support = _find_scope_class_support(section, class_type, organization)
            if support is None:
                fatal_issue(
                    "scope_class_type_source_missing",
                    class_field,
                    f"班型 {class_id} 在匹配标题的章节内没有名称或别名依据",
                )
                add_evidence(
                    f"{class_field}.source_support",
                    class_id,
                    STATUS_FATAL,
                    "scope_section_token_search",
                    location=f"section:{section_id}" if section else "parsed.structure",
                )
            else:
                add_evidence(
                    f"{class_field}.source_support",
                    class_id,
                    STATUS_VERIFIED,
                    support.method,
                    quote=support.quote,
                    location=support.location,
                    **_locator_extra(support.page, support.bbox),
                )

    audit_detail_classes(doc.audience_detail, "document.audience_detail")
    if version.audience_detail is not None:
        audit_detail_classes(version.audience_detail, "version.audience_detail")
    for scope_index, scope in enumerate(doc.audience_scopes):
        audit_scope(scope, scope_index)

    add_evidence(
        "relation",
        doc.relation.value,
        STATUS_DECLARED,
        "config_declaration",
        location="sources.yaml",
        relation_verified=False,
    )
    if doc.relation == PolicyRelation.STANDALONE:
        add_evidence(
            "relation.parent",
            None,
            STATUS_NOT_APPLICABLE,
            "config_declaration",
            location="sources.yaml",
            note="standalone 不声明上位/替代关系",
        )
    else:
        parent_doc_id = doc.parent_doc_id
        parent_version_id = doc.parent_version_id
        if not parent_doc_id:
            fatal_issue("relation_parent_missing", "parent_doc_id", "非 standalone 关系缺少父文档")
            add_evidence(
                "relation.parent",
                None,
                STATUS_FATAL,
                "source_documents_lookup",
                location="source_documents",
            )
        else:
            parent = source_documents.get(parent_doc_id)
            if parent is None:
                fatal_issue(
                    "relation_parent_missing",
                    "parent_doc_id",
                    f"父文档 {parent_doc_id} 不在 source_documents",
                )
                add_evidence(
                    "relation.parent",
                    parent_doc_id,
                    STATUS_FATAL,
                    "source_documents_lookup",
                    location="source_documents",
                )
            elif not parent_version_id:
                fatal_issue(
                    "relation_parent_version_missing",
                    "parent_version_id",
                    f"父文档 {parent_doc_id} 未指定版本",
                )
                add_evidence(
                    "relation.parent_version",
                    None,
                    STATUS_FATAL,
                    "source_documents_lookup",
                    location="source_documents",
                )
            else:
                parent_version = next(
                    (
                        item
                        for item in parent.versions
                        if item.version_id == parent_version_id
                    ),
                    None,
                )
                if parent_version is None:
                    fatal_issue(
                        "relation_parent_version_missing",
                        "parent_version_id",
                        f"父文档 {parent_doc_id} 不存在版本 {parent_version_id}",
                    )
                    add_evidence(
                        "relation.parent_version",
                        parent_version_id,
                        STATUS_FATAL,
                        "source_documents_lookup",
                        location="source_documents",
                    )
                else:
                    add_evidence(
                        "relation.parent",
                        {
                            "doc_id": parent.doc_id,
                            "version_id": parent_version.version_id,
                        },
                        STATUS_DECLARED,
                        "source_documents_lookup",
                        location="source_documents",
                        parent_title=parent.title,
                        parent_title_source="sources.yaml",
                    )
                    if not parent.title.strip():
                        fatal_issue(
                            "relation_parent_title_missing",
                            "parent_title",
                            "父文档缺少标题，无法验证子文档中的正式引用",
                        )
                        add_evidence(
                            "relation.citation",
                            parent.title,
                            STATUS_FATAL,
                            "parent_title_search",
                            location="parsed.text",
                            relation=doc.relation.value,
                            parent_doc_id=parent_doc_id,
                            parent_version_id=parent_version_id,
                            citation_verified=False,
                            relation_verified=False,
                        )
                    else:
                        citation_match = None
                        matched_title = None
                        for variant in _parent_title_variants(parent.title):
                            found = source_text.find(variant)
                            if found is not None:
                                citation_match = found
                                matched_title = variant
                                break
                        if citation_match is None:
                            review_issue(
                                "relation_citation_unverified",
                                "relation",
                                f"子文档解析正文未出现父文档标题 {parent.title!r}，"
                                "关系类型保持声明值",
                            )
                            add_evidence(
                                "relation.citation",
                                parent.title,
                                STATUS_DECLARED,
                                "parent_title_search",
                                location="parsed.text",
                                relation=doc.relation.value,
                                parent_doc_id=parent_doc_id,
                                parent_version_id=parent_version_id,
                                citation_verified=False,
                                relation_verified=False,
                            )
                        else:
                            add_evidence(
                                "relation.citation",
                                parent.title,
                                STATUS_VERIFIED,
                                "parent_title_in_child_parsed_text",
                                quote=citation_match.quote,
                                location="parsed.text",
                                matched_title=matched_title,
                                relation=doc.relation.value,
                                parent_doc_id=parent_doc_id,
                                parent_version_id=parent_version_id,
                                citation_verified=True,
                                relation_verified=False,
                            )

    status = "failed" if fatal else "review" if review else "verified"
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "doc_id": doc.doc_id,
        "version_id": version.version_id,
        "status": status,
        "fatal": fatal,
        "review": review,
        "evidence": evidence,
        "summary": {
            "fatal": len(fatal),
            "review": len(review),
            "verified": sum(item["status"] == STATUS_VERIFIED for item in evidence),
            "declared_unverified": sum(
                item["status"] == STATUS_DECLARED for item in evidence
            ),
        },
        "source": {
            "path": str(source_path),
            "sha256": actual_sha,
            "parsed_sha256": parsed.source_sha256,
            "url": source_url,
            "format": parsed.format,
            "byte_size": len(content) if content is not None else None,
            "acquisition_path": str(provenance_path or sidecar_path),
            "acquisition_status": acquisition_status,
            "provenance_method": provenance_method,
            "acquisition": (
                {
                    field: acquisition.get(field)
                    for field in (
                        RAW_META_VALUE_FIELDS
                        if provenance_kind == "raw_meta"
                        else REQUIRED_ACQUISITION_FIELDS
                    )
                }
                if acquisition is not None
                else None
            ),
        },
        "configured": {
            "title": doc.title,
            "relation": doc.relation.value,
            "parent_doc_id": doc.parent_doc_id,
            "parent_version_id": doc.parent_version_id,
            "date_confidence": version.date_confidence,
            "date_source": version.date_source,
            "scope_status": (
                effective_detail.scope_status.value if effective_detail is not None else None
            ),
            "document_class_types": (
                list(doc.audience_detail.class_types)
                if doc.audience_detail is not None
                else []
            ),
            "version_class_types": (
                list(version.audience_detail.class_types)
                if version.audience_detail is not None
                else []
            ),
            "audience_scopes": [
                {"match": scope.match, "class_types": list(scope.class_types)}
                for scope in doc.audience_scopes
            ],
        },
    }
