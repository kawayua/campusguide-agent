"""条款切分：章/条边界识别、节级/段落回退、前言与附录分块、联系方式脱敏。

切分是 W1 的核心规则，采用纯正则 + 确定性处理：
- 制度类文档优先按“第 X 条”切分，章标题写入 chapter_path；
- 没有“第 X 条”时，先尝试“一、二、三”节级切分（培养方案、指南）；
- 仍无节标题的 guide 按段落切块，保证正文可检索；
- 超过 800 字的条款/节按段落或句子切成多个 part，共享同一个 article_id；
- 重复条款号/节标题追加 ~N 保证 article_id 唯一，并记录告警。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import BlockType, DocType, ScopeStatus
from campusguide.domain.ids import article_index_from_no, make_article_id
from campusguide.domain.models import (
    Article,
    CellProvenance,
    DocumentVersion,
    HeaderColumnProvenance,
)
from campusguide.domain.text import clean_whitespace, normalize_text
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.redact import redact_text
from campusguide.ingest.structure import (
    HeaderColumnBinding,
    TableCell,
    TableRow,
    TableStructure,
    join_row_texts,
    row_leaf_slots,
)

MAX_ARTICLE_CHARS = 800
PREFACE_MIN_CHARS = 20
TAIL_MIN_CHARS = 20

_CN_NUM = "一二三四五六七八九十百千零〇两0-9"
CHAPTER_RE = re.compile(
    rf"(?m)^[ \t　]*(?P<title>第[{_CN_NUM}]{{1,12}}章(?:[ \t　]+[^\n]{{0,80}})?)[ \t　]*$"
)
ARTICLE_RE = re.compile(
    rf"(?m)^[ \t　]*(?P<no>第[{_CN_NUM}]{{1,12}}条(?:之[{_CN_NUM}]{{1,3}})?)"
    rf"(?P<sep>[ \t　]+|[：:、.．]|$)(?P<rest>.*)$"
)
# 部分 PDF 会把“第三条学生应当…”粘在一起；仅在严格规则零命中时回退。
ARTICLE_RE_LOOSE = re.compile(
    rf"(?m)^[ \t　]*(?P<no>第[{_CN_NUM}]{{1,12}}条(?:之[{_CN_NUM}]{{1,3}})?)"
    rf"[ \t　]*(?P<rest>.*)$"
)
# 培养方案、指南常用“一、培养目标”“二、课程设置”的节级标题。
# 只认行首编号 + 不超过 60 字的短标题，避免把正文中的列举项当成节。
SECTION_RE = re.compile(
    r"(?m)^[ \t　]*(?P<no>[一二三四五六七八九十百]+)[、.．][ \t　]*"
    r"(?P<title>[^\n]{1,60})[ \t　]*$"
)
# “第一章”“第一部分”等只写入 chapter_path，不单独作为检索单位。
PART_RE = re.compile(
    rf"(?m)^[ \t　]*(?P<title>第[{_CN_NUM}]{{1,12}}(?:章|部分)[^\n]{{0,80}})[ \t　]*$"
)
# 合订本/单专业方案的专业标题，用于 chapter_path 的第一级。
# 丁颖班等标题不带“专业”二字（如“农学（丁颖创新班）人才培养方案”），
# 因此同时接受“人才培养方案”结尾，但不接受泛化的“第三章 培养方案”。
MAJOR_HEADING_RE = re.compile(
    r"(?m)^[ \t　]*(?P<title>"
    r"(?:[^\n]{2,50}?专业(?:人才培养|培养)?方案"
    r"|[^\n]{2,50}?人才培养方案)"
    r"(?:[（(]\d{4}\s*版[）)])?)[ \t　]*$"
)

# 只清理明确的页码标记；裸数字可能是年份、金额或文号，不能丢弃。
PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:[-—–]\s*\d{1,4}\s*[-—–]|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+)\s*$"
)
SENTENCE_RE = re.compile(r"(?<=[。！？；!?;])")
# 近一年栏目元数据按“日期｜标题｜链接”一行一条；日期行必须独立成块，
# 否则段落回退会把多行合并进同一个 800 字块，单条就无法精确引用。
ROW_LINE_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*[｜|]")
# 文末落款：空行之后的一到三行，最后一行是“YYYY年M月D日”。
# 落款不进入条款库，避免把发布日期粘到最后一条正文里。
SIGNATURE_TAIL_RE = re.compile(
    r"\n[ \t　]*\n"
    r"(?P<sig>(?:[^\n]{0,80}\n){1,3}[^\n]{0,80}"
    r"(?:19|20)\d{2}\s*年\s*(?:0?[1-9]|1[0-2])\s*月\s*"
    r"(?:0?[1-9]|[12]\d|3[01])\s*日[^\n]{0,10})[ \t　]*\n?[ \t　]*$"
)


@dataclass
class SegmentResult:
    articles: list[Article] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_article_indices: list[int] = field(default_factory=list)
    duplicate_article_nos: list[str] = field(default_factory=list)
    redaction_counts: dict[str, int] = field(default_factory=dict)
    used_loose_article_rule: bool = False
    used_section_rule: bool = False
    used_paragraph_rule: bool = False

    @property
    def indexable_articles(self) -> list[Article]:
        return [
            article
            for article in self.articles
            if article.block_type in {BlockType.ARTICLE, BlockType.SECTION}
            and article.text_normalized
        ]

    @property
    def blocks(self) -> list[Article]:
        return [
            article
            for article in self.articles
            if article.block_type not in {BlockType.ARTICLE, BlockType.SECTION}
        ]

    def merge_redaction_counts(self, counts: dict[str, int]) -> None:
        for key, value in counts.items():
            self.redaction_counts[key] = self.redaction_counts.get(key, 0) + value


def _append_redaction_warning(result: SegmentResult) -> None:
    if result.redaction_counts:
        result.warnings.append(
            "索引文本已脱敏："
            + ", ".join(f"{key}={value}" for key, value in sorted(result.redaction_counts.items()))
        )


def _split_text(text: str, max_chars: int) -> list[str]:
    paragraphs = [paragraph.strip() for paragraph in text.split("\n") if paragraph.strip()]
    has_rows = any(ROW_LINE_RE.match(paragraph) for paragraph in paragraphs)
    if len(text) <= max_chars and not has_rows:
        return [text] if text else []

    units: list[tuple[bool, str]] = []
    for paragraph in paragraphs:
        if ROW_LINE_RE.match(paragraph):
            units.append((True, paragraph))
            continue
        if len(paragraph) <= max_chars:
            units.append((False, paragraph))
            continue
        for sentence in SENTENCE_RE.split(paragraph):
            sentence = sentence.strip()
            while len(sentence) > max_chars:
                units.append((False, sentence[:max_chars]))
                sentence = sentence[max_chars:]
            if sentence:
                units.append((False, sentence))

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current.strip())
            current = ""

    for is_row, unit in units:
        if is_row:
            flush()
            if len(unit) <= max_chars:
                chunks.append(unit)
            else:
                chunks.extend(
                    unit[index : index + max_chars] for index in range(0, len(unit), max_chars)
                )
            continue
        candidate = f"{current}\n{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
        else:
            flush()
            current = unit
    flush()
    return chunks or [text]


_WRAPPED_LINE_RE = re.compile(r"(?<=[\u4e00-\u9fff，、；：,;:0-9])\n(?=[\u4e00-\u9fff])")


def _clean_article_body(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if PAGE_NUMBER_RE.match(stripped):
            continue
        lines.append(stripped)
    body = "\n".join(lines)
    # PDF 常按版面宽度硬换行；中文行尾直接续排，避免引文出现“之一 的”。
    body = _WRAPPED_LINE_RE.sub("", body)
    return clean_whitespace(body)


def _scoped_audience_detail(
    version: DocumentVersion,
    chapter_path: list[str],
) -> AudienceDetail | None:
    """合订本按章节标题收窄专业/班型；未命中章节时沿用文档级细目。

    只匹配 chapter_path 中的原文标题，避免正文偶然出现班名就改写整篇适用性。
    命中后以章节声明为准，不继承文档级专业并集（否则种子板块会带上农学）。
    """
    detail = version.audience_detail
    if not version.audience_scopes:
        return detail
    path = [clean_whitespace(item) for item in chapter_path if item]
    for scope in version.audience_scopes:
        match = clean_whitespace(scope.match)
        if not match or not any(match in item for item in path):
            continue
        base = detail or AudienceDetail()
        return base.model_copy(
            update={
                "majors": list(scope.majors),
                "class_types": list(scope.class_types),
                "scope_status": ScopeStatus.EXACT,
                "scope_evidence": scope.evidence,
            }
        )
    return detail


def _build_article(
    *,
    version: DocumentVersion,
    article_no: str,
    article_index: int,
    chapter_path: list[str],
    text: str,
    block_type: BlockType,
    part_no: int,
    duplicate: int,
    topic_tags: list[str],
    flow_ids: list[str],
    source_format: str,
) -> Article:
    return Article(
        article_id=make_article_id(
            version.doc_id,
            version.version_id,
            article_no,
            duplicate=duplicate,
        ),
        doc_id=version.doc_id,
        version_id=version.version_id,
        doc_title=version.title,
        doc_type=version.doc_type,
        authority_level=version.authority_level,
        issuer_level=version.issuer_level,
        issuer_college_id=version.issuer_college_id,
        parent_doc_id=version.parent_doc_id,
        parent_version_id=version.parent_version_id,
        relation=version.relation,
        chapter_path=chapter_path,
        article_no=article_no,
        article_index=article_index,
        part_no=part_no,
        block_type=block_type,
        text=text,
        text_normalized=normalize_text(text),
        text_raw_ref=f"{version.doc_id}/{version.version_id}/source.{source_format}",
        audience=version.audience,
        audience_detail=_scoped_audience_detail(version, chapter_path),
        status=version.status,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        source_url=version.source_url,
        published_at=version.published_at,
        date_confidence=version.date_confidence,
        date_source=version.date_source,
        issuing_department=version.issuing_department,
        topic_tags=topic_tags,
        flow_ids=flow_ids,
        source_sha256=version.source_sha256,
        metadata_source_status=version.metadata_source_status,
        metadata_source_audit_sha256=version.metadata_source_audit_sha256,
    )


def _add_parts(
    result: SegmentResult,
    *,
    version: DocumentVersion,
    article_no: str,
    article_index: int,
    chapter_path: list[str],
    text: str,
    block_type: BlockType,
    duplicate: int,
    topic_tags: list[str],
    flow_ids: list[str],
    source_format: str,
    max_chars: int,
    redact_contacts: bool = True,
) -> None:
    redacted = redact_text(_clean_article_body(text), redact_contacts=redact_contacts)
    result.merge_redaction_counts(redacted.counts)
    parts = _split_text(redacted.text, max_chars)
    if not parts:
        result.warnings.append(f"{article_no} 正文为空")
    for part_no, part in enumerate(parts, start=1):
        result.articles.append(
            _build_article(
                version=version,
                article_no=article_no,
                article_index=article_index,
                chapter_path=chapter_path,
                text=part,
                block_type=block_type,
                part_no=part_no,
                duplicate=duplicate,
                topic_tags=topic_tags,
                flow_ids=flow_ids,
                source_format=source_format,
            )
        )


def _plausible_section(match: re.Match[str]) -> bool:
    """过滤正文中形似“一、……”的句子，只保留短标题。"""
    title = match.group("title").strip()
    if not title or title.endswith(("。", "！", "？", "；", "，", ",", ".", ";")):
        return False
    return len(re.findall(r"[\u4e00-\u9fff]", title)) >= 2


def _context_at(
    position: int,
    parts: list[re.Match[str]],
    majors: list[re.Match[str]],
) -> list[str]:
    context: list[str] = []
    major: str | None = None
    for match in majors:
        if match.start() < position:
            major = clean_whitespace(match.group("title"))
        else:
            break
    if major:
        context.append(major)

    part: str | None = None
    for match in parts:
        if match.start() < position:
            part = clean_whitespace(match.group("title"))
        else:
            break
    if part:
        context.append(part)
    return context


def _major_context_at(position: int, majors: list[re.Match[str]]) -> list[str]:
    major: str | None = None
    for match in majors:
        if match.start() < position:
            major = clean_whitespace(match.group("title"))
        else:
            break
    return [major] if major else []


def _add_sections(
    result: SegmentResult,
    *,
    parsed: ParsedDocument,
    version: DocumentVersion,
    sections: list[re.Match[str]],
    parts: list[re.Match[str]],
    majors: list[re.Match[str]],
    topic_tags: list[str],
    flow_ids: list[str],
    max_chars: int,
    redact_contacts: bool = True,
) -> None:
    text = parsed.text
    first_start = sections[0].start()
    preface = text[:first_start]
    if len(normalize_text(preface)) >= PREFACE_MIN_CHARS:
        _add_parts(
            result,
            version=version,
            article_no="前言",
            article_index=0,
            chapter_path=[],
            text=preface,
            block_type=BlockType.PREFACE,
            duplicate=1,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )

    duplicate_counts: dict[str, int] = {}
    block_order = 0
    preamble_blocks = 0
    for order, match in enumerate(sections, start=1):
        heading = clean_whitespace(f"{match.group('no')}、{match.group('title')}")
        body_start = match.end()
        body_end = sections[order].start() if order < len(sections) else len(text)
        raw_body_end = body_end

        # 章/部分标题后、下一节标题前的内容（如“第二章 培养方案”的学制、
        # 学分表）不属于上一节；单独建块，避免把表格挂到“学位论文要求”下。
        preamble = None
        for candidate in [*parts, *majors]:
            if body_start <= candidate.start() < body_end and (
                preamble is None or candidate.start() > preamble.start()
            ):
                preamble = candidate
        if preamble is not None:
            if len(normalize_text(text[preamble.end() : raw_body_end])) < PREFACE_MIN_CHARS:
                preamble = None
            else:
                body_end = preamble.start()

        duplicate_counts[heading] = duplicate_counts.get(heading, 0) + 1
        duplicate = duplicate_counts[heading]
        if duplicate > 1:
            result.warnings.append(f"节标题重复：{heading}（第 {duplicate} 次出现）")

        block_order += 1
        _add_parts(
            result,
            version=version,
            article_no=heading,
            article_index=block_order,
            chapter_path=_context_at(match.start(), parts, majors),
            text=text[body_start:body_end],
            block_type=BlockType.SECTION,
            duplicate=duplicate,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )

        if preamble is not None:
            preamble_heading = clean_whitespace(preamble.group("title"))
            duplicate_counts[preamble_heading] = duplicate_counts.get(preamble_heading, 0) + 1
            preamble_duplicate = duplicate_counts[preamble_heading]
            if preamble_duplicate > 1:
                result.warnings.append(
                    f"章节标题重复：{preamble_heading}（第 {preamble_duplicate} 次出现）"
                )
            block_order += 1
            preamble_blocks += 1
            _add_parts(
                result,
                version=version,
                article_no=preamble_heading,
                article_index=block_order,
                chapter_path=_major_context_at(preamble.start(), majors),
                text=text[preamble.end() : raw_body_end],
                block_type=BlockType.SECTION,
                duplicate=preamble_duplicate,
                topic_tags=topic_tags,
                flow_ids=flow_ids,
                source_format=parsed.format,
                max_chars=max_chars,
                redact_contacts=redact_contacts,
            )

    result.used_section_rule = True
    summary = f"未识别到“第X条”，已按节切分：{len(sections)} 节"
    if preamble_blocks:
        summary += f"，章节导语单独建块 {preamble_blocks} 处"
    result.warnings.append(summary)


def _add_paragraph_chunks(
    result: SegmentResult,
    *,
    parsed: ParsedDocument,
    version: DocumentVersion,
    topic_tags: list[str],
    flow_ids: list[str],
    max_chars: int,
    redact_contacts: bool = True,
) -> None:
    chunks = _split_text(_clean_article_body(parsed.text), max_chars)
    if not chunks:
        result.warnings.append("正文为空，未生成段落块")
        return
    for order, chunk in enumerate(chunks, start=1):
        _add_parts(
            result,
            version=version,
            article_no=f"段落{order}",
            article_index=order,
            chapter_path=[],
            text=chunk,
            block_type=BlockType.SECTION,
            duplicate=1,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )
    result.used_paragraph_rule = True
    result.warnings.append(f"未识别到条款或节标题，已按段落切分：{len(chunks)} 段")


def segment_document(
    parsed: ParsedDocument,
    version: DocumentVersion,
    *,
    topic_tags: list[str] | None = None,
    flow_ids: list[str] | None = None,
    max_article_chars: int = MAX_ARTICLE_CHARS,
    redact_contacts: bool = True,
) -> SegmentResult:
    return _segment_document(
        parsed,
        version,
        topic_tags=topic_tags or [],
        flow_ids=flow_ids or [],
        max_chars=max_article_chars,
        redact_contacts=redact_contacts,
    )


def _table_spans(parsed: ParsedDocument) -> list[tuple[int, int, str]]:
    """定位正文中的表格占位块，切正文前剔除，避免表格与行块重复。"""
    if parsed.structure is None:
        return []
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for block in parsed.structure.blocks:
        if block.kind != "table" or not block.table_id:
            continue
        start = block.locator.char_start
        end = block.locator.char_end
        if start is None or end is None or start < 0 or end <= start:
            found = parsed.text.find(block.text, cursor)
            if found < 0:
                continue
            start, end = found, found + len(block.text)
        spans.append((start, end, block.table_id))
        cursor = end
    return spans


_TABLE_CONTEXT_CUE_RE = re.compile(r"(下列|如下|以下|提交|附)[^。！？；;]{0,40}[：:]\s*$")


def _table_kept_inline(parsed: ParsedDocument, start: int) -> bool:
    """短条款直接引出表格时保留表文，避免“下列材料”与表格脱节。

    长章节/培养方案里的表格仍从正文剔除，只留行块，防止大表重复进入正文检索。
    """
    prefix = parsed.text[:start]
    article_matches = list(ARTICLE_RE.finditer(prefix)) or list(ARTICLE_RE_LOOSE.finditer(prefix))
    lead_start = article_matches[-1].start() if article_matches else 0
    chapters = list(CHAPTER_RE.finditer(prefix))
    if chapters:
        lead_start = max(lead_start, chapters[-1].start())
    lead = normalize_text(prefix[lead_start:])
    if len(lead) > 300:
        return False
    return bool(_TABLE_CONTEXT_CUE_RE.search(lead))


def _table_free_text(parsed: ParsedDocument, spans: list[tuple[int, int, str]]) -> str:
    if not spans:
        return parsed.text
    parts: list[str] = []
    cursor = 0
    removed = False
    for start, end, table_id in spans:
        if start < cursor:
            continue
        table = (
            parsed.structure.table_by_id(table_id)
            if parsed.structure is not None
            else None
        )
        # 短引语本可保留表文，但敏感列值在正文里没有列头上下文，只能整表
        # 从正文剔除，交给已按列脱敏的父表/行块承载。
        force_remove = table is not None and bool(_sensitive_columns(table))
        if not force_remove and _table_kept_inline(parsed, start):
            continue
        parts.append(parsed.text[cursor:start])
        parts.append("\n")
        cursor = end
        removed = True
    if not removed:
        return parsed.text
    parts.append(parsed.text[cursor:])
    return "".join(parts)


def _chapter_for_table(parsed: ParsedDocument, table: TableStructure) -> list[str]:
    if parsed.structure is None:
        return []
    index = next(
        (
            position
            for position, block in enumerate(parsed.structure.blocks)
            if block.table_id == table.table_id
        ),
        -1,
    )
    if index < 0:
        return []
    major: str | None = None
    chapter: str | None = None
    section: str | None = None
    for block in parsed.structure.blocks[:index]:
        for line in block.text.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if MAJOR_HEADING_RE.match(candidate):
                major = clean_whitespace(candidate)
            if CHAPTER_RE.match(candidate):
                chapter = clean_whitespace(candidate)
                section = None
                continue
            section_match = SECTION_RE.match(candidate)
            if section_match and _plausible_section(section_match):
                section = clean_whitespace(candidate)
    return [value for value in (major, chapter, section) if value]


def _redacted(text: str, *, redact_contacts: bool) -> tuple[str, dict[str, int]]:
    redacted = redact_text(clean_whitespace(text), redact_contacts=redact_contacts)
    return redacted.text, redacted.counts


# 学号/学籍号列的值在行文本里没有字段标签，只能靠表头识别；按值形态猜测
# 会误伤课程代码、文号和公开座机。学生证号同属学生身份字段。
_STUDENT_ID_HEADER_RE = re.compile(r"学号|学籍号|学生证号")
STUDENT_ID_PLACEHOLDER = "[学号已脱敏]"


def _column_labels(table: TableStructure, col: int) -> list[str]:
    labels: list[str] = []
    if col < len(table.column_headers):
        labels.append(table.column_headers[col])
    if col < len(table.header_paths):
        labels.extend(table.header_paths[col])
    for row in table.rows:
        if not row.is_header or col >= len(row.cells):
            continue
        cell = row.cells[col]
        if cell.covered:
            origin_row = (
                table.rows[cell.origin_row] if cell.origin_row < len(table.rows) else None
            )
            if origin_row is not None and cell.origin_col < len(origin_row.cells):
                cell = origin_row.cells[cell.origin_col]
        if cell.text.strip():
            labels.append(cell.text)
    return labels


def _sensitive_columns(table: TableStructure) -> set[int]:
    """按真实列头/多层 header_paths 识别学生身份列，不按单元格值猜测。"""
    column_count = table.column_count or max(
        (len(row.cells) for row in table.rows), default=0
    )
    return {
        col
        for col in range(column_count)
        if any(
            _STUDENT_ID_HEADER_RE.search(re.sub(r"\s+", "", label))
            for label in _column_labels(table, col)
        )
    }


def _cell_in_sensitive_column(cell: TableCell, sensitive: set[int]) -> bool:
    if cell.col in sensitive:
        return True
    start = cell.origin_col
    return any(col in sensitive for col in range(start, start + max(1, cell.colspan)))


def _column_redacted_row_text(
    table: TableStructure,
    row: TableRow,
    sensitive: set[int],
) -> tuple[str, int]:
    """按 leaf col 重建行文本，并在生成父表/行块前替换敏感列。

    没有敏感列时直接返回原始 ``row.text``，保持既有行文本逐字节不变。
    """
    if not sensitive or row.is_header:
        return row.text, 0
    column_count = table.column_count or max(
        (len(table_row.cells) for table_row in table.rows), default=0
    )
    if column_count <= 0:
        return row.text, 0

    def resolve_origin(origin_row: int, origin_col: int) -> TableCell | None:
        if not 0 <= origin_row < len(table.rows):
            return None
        cells = table.rows[origin_row].cells
        if not 0 <= origin_col < len(cells):
            return None
        return cells[origin_col]

    slots = row_leaf_slots(
        row.cells,
        row_index=row.row_index,
        column_count=column_count,
        resolve_origin=resolve_origin,
    )
    texts: list[str] = []
    redactions = 0
    for cell in slots:
        if cell is None:
            texts.append("")
            continue
        text = cell.text
        if text.strip() and _cell_in_sensitive_column(cell, sensitive):
            text = STUDENT_ID_PLACEHOLDER
            redactions += 1
        texts.append(text)
    return join_row_texts(texts), redactions


def _cell_provenance(
    cells: list[TableCell],
    *,
    redact_contacts: bool,
    sensitive_columns: set[int] | None = None,
) -> list[CellProvenance]:
    """保留完整列位与单元格坐标；provenance 随公开 payload 返回，必须同步脱敏。"""
    sensitive = sensitive_columns or set()
    provenances: list[CellProvenance] = []
    for cell in cells:
        text = clean_whitespace(cell.text)
        if text and not cell.is_header and _cell_in_sensitive_column(cell, sensitive):
            text = STUDENT_ID_PLACEHOLDER
        else:
            text = redact_text(text, redact_contacts=redact_contacts).text
        provenances.append(
            CellProvenance(
                cell_id=cell.cell_id,
                row=cell.row,
                col=cell.col,
                text=text,
                rowspan=cell.rowspan,
                colspan=cell.colspan,
                origin_row=cell.origin_row,
                origin_col=cell.origin_col,
                covered=cell.covered,
                is_header=cell.is_header,
                bbox=list(cell.bbox) if cell.bbox else None,
                dom_path=cell.dom_path,
                text_source=cell.text_source,
                needs_review=cell.needs_review,
            )
        )
    return provenances


def _relation_scope_problem(identifier: str, parsed: ParsedDocument) -> str | None:
    prefix, separator, _ = identifier.partition("#")
    if not separator or "@" not in prefix:
        return None
    expected = f"{parsed.doc_id}@{parsed.version_id}"
    if prefix != expected:
        return f"属于其他文档/版本（{prefix}）"
    return None


def _trusted_header_provenance(
    table: TableStructure,
    *,
    tables_by_id: dict[str, list[TableStructure]],
    parsed: ParsedDocument,
    result: SegmentResult,
) -> tuple[str | None, str | None, int | None]:
    """只复用解析器在同一结构内建立的续表头引用，无法追溯时整体丢弃。"""
    continuation_of = table.continuation_of
    source_id = table.header_source_table_id
    source_page = table.header_source_page
    if continuation_of is None and source_id is None and source_page is None:
        return None, None, None

    def reject(reason: str) -> tuple[None, None, None]:
        result.warnings.append(
            f"表格 {table.table_id} 的续表头来源不可追溯（{reason}），已丢弃相关元数据"
        )
        return None, None, None

    if not continuation_of or continuation_of != continuation_of.strip():
        return reject("continuation_of 为空或含首尾空白")
    if not source_id or source_id != source_id.strip():
        return reject("header_source_table_id 为空或含首尾空白")
    if continuation_of == table.table_id or source_id == table.table_id:
        return reject("指向当前表自身")
    for identifier in (continuation_of, source_id):
        scope_problem = _relation_scope_problem(identifier, parsed)
        if scope_problem:
            return reject(f"{identifier} {scope_problem}")
    precursors = tables_by_id.get(continuation_of, [])
    sources = tables_by_id.get(source_id, [])
    if len(precursors) != 1:
        return reject(f"前表 {continuation_of} 不在当前解析结构中或 ID 重复")
    if len(sources) != 1:
        return reject(f"表头来源 {source_id} 不在当前解析结构中或 ID 重复")
    precursor = precursors[0]
    source = sources[0]
    if precursor.kind == "layout" or source.kind == "layout":
        return reject("引用了 layout 表")
    expected_source = precursor.header_source_table_id or precursor.table_id
    if expected_source != source_id:
        return reject(f"表头来源 {source_id} 与前表链 {expected_source} 不一致")
    if source_page is not None:
        if (
            isinstance(source_page, bool)
            or not isinstance(source_page, int)
            or source_page <= 0
        ):
            return reject(f"header_source_page={source_page!r} 不是正整数")
        actual_page = (
            source.header_source_page
            if source.header_source_page is not None
            else source.page
        )
        if actual_page is not None and source_page != actual_page:
            return reject(
                f"header_source_page={source_page} 与来源表实际页 {actual_page} 不一致"
            )
    return continuation_of, source_id, source_page


# 与 structure._HEADER_INTERVAL_MATCH_TOLERANCE 一致：续表列继承只接受
# 来源抽取器自身的严格物理匹配，不在这里放宽。
_HEADER_INTERVAL_MATCH_TOLERANCE = 1.0
_BINDING_BBOX_EPSILON = 1e-6
_LABELED_STATUSES = frozenset({"bound", "shared"})
_UNBOUND_STATUSES = frozenset({"unresolved", "offset", "ambiguous"})
_PHYSICAL_BINDING_METHODS = frozenset(
    {"physical_interval", "trailing_empty_recovery"}
)


class HeaderBindingError(ValueError):
    """结构层表头绑定无法与原文逐项对上；必须显式失败而不是静默降级。"""


def _binding_bbox_rect(
    bbox: list[float] | None,
) -> tuple[float, float, float, float] | None:
    if bbox is None or len(bbox) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in bbox):
        return None
    values = tuple(float(item) for item in bbox)
    if not all(math.isfinite(item) for item in values):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _binding_bbox_interval(bbox: list[float] | None) -> tuple[float, float] | None:
    """2 元值只取显式 x 区间，4 元值取矩形 x 区间；不把区间当矩形。"""
    if bbox is None or len(bbox) not in (2, 4):
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in bbox):
        return None
    values = [float(item) for item in bbox]
    if not all(math.isfinite(item) for item in values):
        return None
    left, right = (values[0], values[1]) if len(values) == 2 else (values[0], values[2])
    if right <= left:
        return None
    return (left, right)


def _binding_bbox_equal(left: list[float] | None, right: list[float] | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if len(left) != len(right):
        return False
    return all(
        abs(float(a) - float(b)) <= _BINDING_BBOX_EPSILON
        for a, b in zip(left, right, strict=True)
    )


def _binding_edge_delta(
    left: tuple[float, float], right: tuple[float, float]
) -> float:
    return max(abs(left[0] - right[0]), abs(left[1] - right[1]))


def _binding_table_scope(table_id: str) -> str:
    return table_id.split("#", 1)[0].strip()


def _resolved_binding_table(
    identifier: str | None,
    *,
    tables_by_id: dict[str, list[TableStructure]],
    parsed: ParsedDocument,
    role: str,
) -> TableStructure:
    if not identifier or identifier != identifier.strip():
        raise HeaderBindingError(f"{role}为空或含首尾空白")
    expected = f"{parsed.doc_id}@{parsed.version_id}"
    if _binding_table_scope(identifier) != expected:
        raise HeaderBindingError(f"{role} {identifier} 不属于当前文档/版本 {expected}")
    matches = tables_by_id.get(identifier, [])
    if len(matches) != 1:
        raise HeaderBindingError(f"{role} {identifier} 不在当前解析结构中或 ID 重复")
    table = matches[0]
    if table.kind == "layout":
        raise HeaderBindingError(f"{role} {identifier} 指向 layout 表")
    return table


def _table_column_intervals(
    table: TableStructure,
) -> list[tuple[float, float] | None]:
    """镜像 structure._table_column_intervals，用于物理列区间复核。"""
    bounds: list[tuple[float, float] | None] = [None] * max(0, table.column_count)
    header_rows = [row for row in table.rows if row.is_header]
    if not header_rows:
        header_rows = [
            table.rows[index]
            for index in table.header_row_indices
            if 0 <= index < len(table.rows)
        ]
    sources = header_rows + [row for row in table.rows if not row.is_header]
    for row in sources:
        for cell in row.cells:
            if not 0 <= cell.col < len(bounds) or bounds[cell.col] is not None:
                continue
            if cell.covered or cell.colspan != 1:
                continue
            interval = _binding_bbox_interval(cell.bbox)
            if interval is not None:
                bounds[cell.col] = interval
    return bounds


def _binding_cell_labels(cells: list[CellProvenance]) -> list[str]:
    labels: list[str] = []
    for cell in cells:
        text = clean_whitespace(cell.text)
        if text and (not labels or labels[-1] != text):
            labels.append(text)
    return labels


def _hydrate_origin_header_cells(
    *,
    cell_ids: list[str],
    row_indices: list[int],
    source: TableStructure,
    location: str,
    allow_review: bool = False,
    require_review: bool = False,
) -> list[tuple[TableCell, CellProvenance]]:
    """按声明顺序从真实来源表取 origin 表头格；缺一个就失败，不补造。"""
    if not cell_ids:
        raise HeaderBindingError(f"{location}缺少来源表头单元格")
    if not row_indices:
        raise HeaderBindingError(f"{location}缺少来源表头行号")
    if len(set(row_indices)) != len(row_indices):
        raise HeaderBindingError(f"{location}来源表头行号重复")
    for row_index in row_indices:
        if (
            isinstance(row_index, bool)
            or not isinstance(row_index, int)
            or not 0 <= row_index < len(source.rows)
            or not source.rows[row_index].is_header
        ):
            raise HeaderBindingError(
                f"{location}来源表头行 {row_index} 不存在或不是表头行"
            )
    cells_by_id: dict[str, list[TableCell]] = {}
    for row in source.rows:
        for cell in row.cells:
            cells_by_id.setdefault(cell.cell_id, []).append(cell)
    hydrated: list[tuple[TableCell, CellProvenance]] = []
    seen_ids: set[str] = set()
    previous_row = -1
    for cell_id in cell_ids:
        matches = cells_by_id.get(cell_id, [])
        if len(matches) != 1:
            raise HeaderBindingError(
                f"{location}来源单元格 {cell_id} 缺失或重复"
            )
        cell = matches[0]
        if not cell_id.startswith(f"{source.table_id}#"):
            raise HeaderBindingError(
                f"{location}来源单元格 {cell_id} 不属于来源表 {source.table_id}"
            )
        if cell.covered or cell.origin_row != cell.row or cell.origin_col != cell.col:
            raise HeaderBindingError(
                f"{location}来源单元格 {cell_id} 不是表头 origin 格"
            )
        if not cell.is_header:
            raise HeaderBindingError(f"{location}来源单元格 {cell_id} 不是表头格")
        if cell.row not in row_indices:
            raise HeaderBindingError(
                f"{location}来源单元格 {cell_id} 的行 {cell.row} 未声明为表头行"
            )
        if cell.row < previous_row:
            raise HeaderBindingError(f"{location}来源表头单元格顺序倒置")
        previous_row = cell.row
        if cell_id in seen_ids:
            raise HeaderBindingError(f"{location}来源单元格 {cell_id} 重复引用")
        seen_ids.add(cell_id)
        if cell.needs_review and not allow_review:
            raise HeaderBindingError(
                f"{location}来源单元格 {cell_id} 标记 needs_review，不能提供列语义"
            )
        hydrated.append(
            (cell, _cell_provenance([cell], redact_contacts=False)[0])
        )
    if require_review and not any(
        provenance.needs_review for _, provenance in hydrated
    ):
        raise HeaderBindingError(f"{location}缺少 needs_review 来源审计格")
    return hydrated


def _hydrate_binding_cells(
    binding: HeaderColumnBinding,
    source: TableStructure,
    *,
    table_id: str,
) -> list[CellProvenance]:
    hydrated = _hydrate_origin_header_cells(
        cell_ids=list(binding.source_header_cell_ids),
        row_indices=list(binding.source_header_row_indices),
        source=source,
        location=f"表格 {table_id} 第 {binding.target_col} 列",
    )
    return [provenance for _, provenance in hydrated]


def _review_source_fields_present(binding: HeaderColumnBinding) -> bool:
    return bool(
        binding.review_source_table_id is not None
        or binding.review_source_page is not None
        or binding.review_source_col is not None
        or binding.review_source_header_cell_ids
        or binding.review_source_header_row_indices
        or binding.review_source_bbox is not None
        or binding.review_source_text
    )


def _hydrate_review_binding_cells(
    binding: HeaderColumnBinding,
    *,
    parsed: ParsedDocument,
    tables_by_id: dict[str, list[TableStructure]],
    table_id: str,
) -> list[CellProvenance]:
    location = f"表格 {table_id} 第 {binding.target_col} 列复核来源"
    if binding.status != "unresolved":
        raise HeaderBindingError(f"{location}只允许 unresolved 状态携带复核审计")
    review_table_id = binding.review_source_table_id
    if not review_table_id or review_table_id != review_table_id.strip():
        raise HeaderBindingError(f"{location}缺少有效的 review_source_table_id")
    source = _resolved_binding_table(
        review_table_id,
        tables_by_id=tables_by_id,
        parsed=parsed,
        role="复核来源表",
    )
    review_page = binding.review_source_page
    if review_page is not None and (
        isinstance(review_page, bool) or not isinstance(review_page, int)
    ):
        raise HeaderBindingError(f"{location}review_source_page 必须是整数或 None")
    if review_page != source.page:
        raise HeaderBindingError(f"{location}复核来源页与来源表实际页不一致")
    review_col = binding.review_source_col
    if (
        isinstance(review_col, bool)
        or not isinstance(review_col, int)
        or not 0 <= review_col < source.column_count
    ):
        raise HeaderBindingError(
            f"{location}review_source_col {review_col!r} 超出来源表列数 "
            f"{source.column_count}"
        )
    if binding.review_source_bbox is not None and _binding_bbox_rect(
        binding.review_source_bbox
    ) is None:
        raise HeaderBindingError(f"{location}review_source_bbox 必须是有限非退化四元矩形")
    hydrated = _hydrate_origin_header_cells(
        cell_ids=list(binding.review_source_header_cell_ids),
        row_indices=list(binding.review_source_header_row_indices),
        source=source,
        location=location,
        allow_review=True,
        require_review=True,
    )
    review_cell = next(
        (
            provenance
            for _, provenance in hydrated
            if provenance.needs_review and provenance.col == review_col
        ),
        None,
    )
    if review_cell is None:
        raise HeaderBindingError(
            f"{location}声明的 review_source_col={review_col} 没有 needs_review 来源格"
        )
    if binding.review_source_bbox is not None and not _binding_bbox_equal(
        binding.review_source_bbox, review_cell.bbox
    ):
        raise HeaderBindingError(
            f"{location}review_source_bbox 与 needs_review 来源格实际坐标不一致"
        )
    expected_text = " > ".join(
        cell.text.strip() for cell, _ in hydrated if cell.text.strip()
    )
    # 新复核路径写完整路径拼接；旧快照降级路径只写被标记单元格自身文本。
    # 两者都必须来自真实 origin 格，不能接受任意伪造字符串。
    source_texts = {expected_text}
    source_texts.update(
        cell.text.strip() for cell, _ in hydrated if cell.text.strip()
    )
    if binding.review_source_text not in source_texts:
        raise HeaderBindingError(
            f"{location}review_source_text 与 needs_review 来源格原文不一致"
        )
    return [provenance for _, provenance in hydrated]


def _validate_unbound_binding(
    binding: HeaderColumnBinding,
    *,
    table_id: str,
    parsed: ParsedDocument,
    tables_by_id: dict[str, list[TableStructure]],
) -> HeaderColumnProvenance:
    location = f"表格 {table_id} 第 {binding.target_col} 列"
    if binding.method != "none":
        raise HeaderBindingError(f"{location}未绑定列必须使用 method=none")
    if (
        binding.source_table_id is not None
        or binding.source_via_table_id is not None
        or binding.source_page is not None
        or binding.source_col is not None
        or binding.source_header_cell_ids
        or binding.source_header_row_indices
        or binding.source_bbox is not None
    ):
        raise HeaderBindingError(f"{location}未绑定列不能携带来源证据")
    if binding.header_path or clean_whitespace(binding.header_text):
        raise HeaderBindingError(f"{location}未绑定列必须保留空表头路径")
    if binding.covered or binding.shared_scope:
        raise HeaderBindingError(f"{location}未绑定列不能标记覆盖或共享")
    if binding.target_bbox is not None and (
        (len(binding.target_bbox) == 4
        and _binding_bbox_rect(binding.target_bbox) is None)
        or (len(binding.target_bbox) == 2
        and _binding_bbox_interval(binding.target_bbox) is None)
        or len(binding.target_bbox) not in (2, 4)
    ):
        raise HeaderBindingError(f"{location}target_bbox 非有限或退化")
    review_cells: list[CellProvenance] = []
    if _review_source_fields_present(binding):
        review_cells = _hydrate_review_binding_cells(
            binding,
            parsed=parsed,
            tables_by_id=tables_by_id,
            table_id=table_id,
        )
    return HeaderColumnProvenance(
        target_col=binding.target_col,
        status=binding.status,
        method=binding.method,
        target_bbox=(
            list(binding.target_bbox) if binding.target_bbox is not None else None
        ),
        reason=binding.reason,
        review_source_table_id=binding.review_source_table_id,
        review_source_page=binding.review_source_page,
        review_source_col=binding.review_source_col,
        review_source_header_cell_ids=list(
            binding.review_source_header_cell_ids
        ),
        review_source_header_row_indices=list(
            binding.review_source_header_row_indices
        ),
        review_source_bbox=(
            list(binding.review_source_bbox)
            if binding.review_source_bbox is not None
            else None
        ),
        review_source_text=binding.review_source_text,
        review_source_header_cells=review_cells,
    )


def _binding_provenance(
    table: TableStructure,
    binding: HeaderColumnBinding,
    *,
    tables_by_id: dict[str, list[TableStructure]],
    parsed: ParsedDocument,
    continuation_of: str | None,
    header_source_id: str | None,
    header_source_page: int | None,
    target_intervals: list[tuple[float, float] | None],
) -> HeaderColumnProvenance:
    target_col = binding.target_col
    if not 0 <= target_col < table.column_count:
        raise HeaderBindingError(
            f"表格 {table.table_id} 的表头绑定列 {target_col} "
            f"超出列数 {table.column_count}"
        )
    location = f"表格 {table.table_id} 第 {target_col} 列"
    status = binding.status
    method = binding.method
    if status in _LABELED_STATUSES:
        if method == "none":
            raise HeaderBindingError(f"{location}已绑定列缺少绑定方法")
    elif status == "gap":
        if method != "local_header":
            raise HeaderBindingError(f"{location}空表头缺口必须来自本地表头")
    elif status not in _UNBOUND_STATUSES:
        raise HeaderBindingError(f"{location}未知绑定状态 {status!r}")
    else:
        return _validate_unbound_binding(
            binding,
            table_id=table.table_id,
            parsed=parsed,
            tables_by_id=tables_by_id,
        )

    table_path = (
        [clean_whitespace(part) for part in table.header_paths[target_col]]
        if target_col < len(table.header_paths)
        else []
    )
    table_header = (
        table.column_headers[target_col]
        if target_col < len(table.column_headers)
        else ""
    )
    binding_path = [clean_whitespace(part) for part in binding.header_path]
    if status in _LABELED_STATUSES:
        if not binding_path or any(not part for part in binding_path):
            raise HeaderBindingError(f"{location}已绑定列缺少完整表头路径")
        if binding_path != table_path:
            raise HeaderBindingError(f"{location}表头路径与表格列头不一致")
        expected_header = " > ".join(table_path)
        if (
            clean_whitespace(table_header) != clean_whitespace(expected_header)
            or clean_whitespace(table_header) != clean_whitespace(" > ".join(binding_path))
        ):
            raise HeaderBindingError(f"{location}扁平列头与表头路径不一致")
        if not clean_whitespace(binding.header_text):
            raise HeaderBindingError(f"{location}已绑定列缺少表头文本")
        if status == "shared":
            if not binding.shared_scope:
                raise HeaderBindingError(f"{location}shared 列必须标记 shared_scope")
        elif binding.shared_scope:
            raise HeaderBindingError(f"{location}bound 列不能标记 shared_scope")
    else:
        if binding_path or clean_whitespace(binding.header_text):
            raise HeaderBindingError(f"{location}空表头缺口不能携带表头文本")
        if table_path or clean_whitespace(table_header):
            raise HeaderBindingError(f"{location}空表头缺口与表格列头不一致")

    if binding.source_page is not None and binding.source_page <= 0:
        raise HeaderBindingError(f"{location}来源页号 {binding.source_page!r} 非正数")
    if method == "local_header":
        if binding.source_table_id != table.table_id:
            raise HeaderBindingError(f"{location}本地表头来源必须是当前表")
        if binding.source_via_table_id is not None:
            raise HeaderBindingError(f"{location}本地表头不能有链上来源表")
        if (
            table.continuation_of is not None
            or table.header_source_table_id is not None
            or table.header_source_page is not None
        ):
            raise HeaderBindingError(f"{location}本地表头与续表来源元数据冲突")
        source = table
        if binding.source_page != table.page:
            raise HeaderBindingError(f"{location}来源页与当前表页不一致")
        if status in _LABELED_STATUSES and (
            binding.source_page is None or table.page is None
        ):
            raise HeaderBindingError(f"{location}已绑定列缺少来源页")
    else:
        if binding.source_table_id == table.table_id:
            raise HeaderBindingError(f"{location}继承来源不能指向当前表")
        if (
            continuation_of is None
            or header_source_id is None
            or header_source_page is None
        ):
            raise HeaderBindingError(f"{location}续表头引用未通过可信校验")
        if header_source_id != binding.source_table_id:
            raise HeaderBindingError(f"{location}继承来源与可信续表头来源不一致")
        if header_source_page != binding.source_page:
            raise HeaderBindingError(f"{location}继承来源页与可信续表头页不一致")
        if binding.source_page is None:
            raise HeaderBindingError(f"{location}继承列缺少来源页")
        source = _resolved_binding_table(
            binding.source_table_id,
            tables_by_id=tables_by_id,
            parsed=parsed,
            role="表头来源表",
        )
        if binding.source_via_table_id is not None:
            if binding.source_via_table_id in {table.table_id, source.table_id}:
                raise HeaderBindingError(f"{location}链上来源表指向自身或 origin")
            if continuation_of != binding.source_via_table_id:
                raise HeaderBindingError(f"{location}链上来源表与前表不一致")
            via = _resolved_binding_table(
                binding.source_via_table_id,
                tables_by_id=tables_by_id,
                parsed=parsed,
                role="链上表",
            )
            if (via.header_source_table_id or via.table_id) != source.table_id:
                raise HeaderBindingError(f"{location}链上来源表未指向 origin")
        elif continuation_of != source.table_id:
            raise HeaderBindingError(f"{location}直接继承来源与前表不一致")
        actual_page = (
            source.header_source_page
            if source.header_source_page is not None
            else source.page
        )
        if binding.source_page != actual_page:
            raise HeaderBindingError(f"{location}来源页与 origin 表实际页不一致")
        if table.page is not None and binding.source_page >= table.page:
            raise HeaderBindingError(f"{location}继承来源页必须早于当前表页")

    if (
        source.column_count <= 0
        or binding.source_col is None
        or not 0 <= binding.source_col < source.column_count
    ):
        raise HeaderBindingError(
            f"{location}来源列 {binding.source_col!r} 超出来源表列数 "
            f"{source.column_count}"
        )
    if binding.source_bbox is not None and _binding_bbox_rect(binding.source_bbox) is None:
        raise HeaderBindingError(f"{location}source_bbox 必须是有限非退化四元矩形")
    if binding.target_bbox is not None:
        valid_target = (
            _binding_bbox_rect(binding.target_bbox)
            if len(binding.target_bbox) == 4
            else _binding_bbox_interval(binding.target_bbox)
        )
        if len(binding.target_bbox) not in (2, 4) or valid_target is None:
            raise HeaderBindingError(f"{location}target_bbox 非有限或退化")
    if method in _PHYSICAL_BINDING_METHODS:
        source_interval = _binding_bbox_interval(binding.source_bbox)
        target_interval = (
            target_intervals[target_col]
            if target_col < len(target_intervals)
            else None
        )
        if source_interval is None or target_interval is None:
            raise HeaderBindingError(f"{location}物理列继承缺少来源或目标列区间")
        if (
            _binding_edge_delta(target_interval, source_interval)
            > _HEADER_INTERVAL_MATCH_TOLERANCE
        ):
            raise HeaderBindingError(f"{location}物理列区间超出严格匹配容差")

    cells = _hydrate_binding_cells(binding, source, table_id=table.table_id)
    labels = _binding_cell_labels(cells)
    if status in _LABELED_STATUSES:
        if not labels or labels != binding_path:
            raise HeaderBindingError(f"{location}来源表头文本与声明路径不一致")
        if labels[-1] != clean_whitespace(binding.header_text):
            raise HeaderBindingError(f"{location}来源标签与 header_text 不一致")
        label_cell = next(
            cell for cell in reversed(cells) if clean_whitespace(cell.text)
        )
    else:
        if labels:
            raise HeaderBindingError(f"{location}空表头缺口却带有非空来源标签")
        label_cell = cells[-1]
    if label_cell.col != binding.source_col:
        raise HeaderBindingError(
            f"{location}source_col={binding.source_col} 与来源标签列 "
            f"{label_cell.col} 不一致"
        )
    if binding.source_bbox is not None and not _binding_bbox_equal(
        binding.source_bbox, label_cell.bbox
    ):
        raise HeaderBindingError(f"{location}source_bbox 与来源标签格实际坐标不一致")
    if method in _PHYSICAL_BINDING_METHODS and binding.source_bbox is None:
        raise HeaderBindingError(f"{location}物理列继承缺少 source_bbox")

    return HeaderColumnProvenance(
        target_col=target_col,
        status=status,
        method=method,
        source_table_id=binding.source_table_id,
        source_via_table_id=binding.source_via_table_id,
        source_page=binding.source_page,
        source_col=binding.source_col,
        source_header_cell_ids=list(binding.source_header_cell_ids),
        source_header_row_indices=list(binding.source_header_row_indices),
        source_bbox=(
            list(binding.source_bbox) if binding.source_bbox is not None else None
        ),
        target_bbox=(
            list(binding.target_bbox) if binding.target_bbox is not None else None
        ),
        header_text=binding.header_text,
        header_path=list(binding.header_path),
        covered=binding.covered,
        shared_scope=binding.shared_scope,
        reason=binding.reason,
        source_header_cells=cells,
    )


def _validate_physical_binding_uniqueness(
    table: TableStructure,
    bindings: list[HeaderColumnBinding],
    target_intervals: list[tuple[float, float] | None],
) -> None:
    source_intervals: list[tuple[int, tuple[float, float]]] = []
    for binding in bindings:
        if (
            binding.status not in _LABELED_STATUSES
            or binding.method not in _PHYSICAL_BINDING_METHODS
        ):
            continue
        interval = _binding_bbox_interval(binding.source_bbox)
        if interval is not None:
            source_intervals.append((binding.target_col, interval))
    for binding in bindings:
        if (
            binding.status not in _LABELED_STATUSES
            or binding.method not in _PHYSICAL_BINDING_METHODS
        ):
            continue
        target_interval = (
            target_intervals[binding.target_col]
            if binding.target_col < len(target_intervals)
            else None
        )
        matches = [
            target_col
            for target_col, interval in source_intervals
            if target_interval is not None
            and _binding_edge_delta(target_interval, interval)
            <= _HEADER_INTERVAL_MATCH_TOLERANCE
        ]
        if matches != [binding.target_col]:
            raise HeaderBindingError(
                f"表格 {table.table_id} 第 {binding.target_col} 列物理区间"
                f"匹配不唯一或缺失（匹配 {matches}）"
            )


def _binding_provenances(
    table: TableStructure,
    *,
    tables_by_id: dict[str, list[TableStructure]],
    parsed: ParsedDocument,
    continuation_of: str | None,
    header_source_id: str | None,
    header_source_page: int | None,
) -> list[HeaderColumnProvenance]:
    bindings = list(table.column_bindings)
    if not bindings:
        return []
    if table.column_count <= 0 or len(bindings) != table.column_count:
        raise HeaderBindingError(
            f"表格 {table.table_id} 的表头绑定数 {len(bindings)} "
            f"与列数 {table.column_count} 不一致"
        )
    targets = [binding.target_col for binding in bindings]
    if sorted(targets) != list(range(table.column_count)):
        raise HeaderBindingError(
            f"表格 {table.table_id} 的表头绑定列不唯一或未覆盖全部 leaf 列"
        )
    target_intervals = _table_column_intervals(table)
    provenances = [
        _binding_provenance(
            table,
            binding,
            tables_by_id=tables_by_id,
            parsed=parsed,
            continuation_of=continuation_of,
            header_source_id=header_source_id,
            header_source_page=header_source_page,
            target_intervals=target_intervals,
        )
        for binding in bindings
    ]
    _validate_physical_binding_uniqueness(table, bindings, target_intervals)
    return provenances


def _clone_binding_provenances(
    provenances: list[HeaderColumnProvenance],
) -> list[HeaderColumnProvenance]:
    return [item.model_copy(deep=True) for item in provenances]


def _build_table_articles(
    result: SegmentResult,
    *,
    parsed: ParsedDocument,
    version: DocumentVersion,
    topic_tags: list[str],
    flow_ids: list[str],
    max_chars: int,
    redact_contacts: bool = True,
) -> list[Article]:
    if parsed.structure is None or not parsed.structure.tables:
        return []
    tables_by_id: dict[str, list[TableStructure]] = {}
    for candidate in parsed.structure.tables:
        tables_by_id.setdefault(candidate.table_id, []).append(candidate)
    articles: list[Article] = []
    row_counter = 0
    for table in parsed.structure.tables:
        if table.kind == "layout" or not table.rows:
            continue
        continuation_of, header_source_id, header_source_page = _trusted_header_provenance(
            table,
            tables_by_id=tables_by_id,
            parsed=parsed,
            result=result,
        )
        binding_provenances = _binding_provenances(
            table,
            tables_by_id=tables_by_id,
            parsed=parsed,
            continuation_of=continuation_of,
            header_source_id=header_source_id,
            header_source_page=header_source_page,
        )
        chapter_path = _chapter_for_table(parsed, table)
        parent_id = f"{version.doc_id}@{version.version_id}#table{table.table_index}"
        sensitive = _sensitive_columns(table)
        parent_rows: list[str] = []
        column_redactions = 0
        for row in table.rows:
            row_value, redactions = _column_redacted_row_text(table, row, sensitive)
            if row_value.strip():
                parent_rows.append(row_value)
                column_redactions += redactions
        parent_text, parent_counts = _redacted(
            "\n".join(parent_rows), redact_contacts=redact_contacts
        )
        if column_redactions:
            parent_counts["student_id"] = (
                parent_counts.get("student_id", 0) + column_redactions
            )
        result.merge_redaction_counts(parent_counts)
        if parent_text:
            parent = _build_article(
                version=version,
                article_no=f"表格{table.table_index}（完整表）",
                article_index=9000 + table.table_index,
                chapter_path=chapter_path,
                text=f"【表格】\n{parent_text}",
                block_type=BlockType.TABLE,
                part_no=1,
                duplicate=1,
                topic_tags=topic_tags,
                flow_ids=flow_ids,
                source_format=parsed.format,
            )
            parent.article_id = parent_id
            parent.block_kind = "table_parent"
            parent.table_id = table.table_id
            parent.parent_table_id = table.table_id
            parent.table_headers = list(table.column_headers)
            parent.header_paths = [list(path) for path in table.header_paths]
            parent.table_column_bindings = _clone_binding_provenances(
                binding_provenances
            )
            parent.page = table.page
            parent.bbox = table.bbox
            parent.dom_path = table.locator.dom_path
            parent.table_continuation_of = continuation_of
            parent.table_header_source_id = header_source_id
            parent.table_header_source_page = header_source_page
            articles.append(parent)

        headers = " | ".join(table.column_headers)
        data_rows = [row for row in table.rows if not row.is_header]
        for row in data_rows:
            if not row.text.strip():
                continue
            row_counter += 1
            prefix = "表格类型：适用性矩阵\n" if table.kind == "matrix" else ""
            header_line = f"表头：{headers}" if headers else ""
            row_value, column_redactions = _column_redacted_row_text(table, row, sensitive)
            row_line = f"行：{row_value}"
            raw_text = "\n".join(part for part in (prefix + header_line, row_line) if part)
            row_text, row_counts = _redacted(
                raw_text, redact_contacts=redact_contacts
            )
            if column_redactions:
                row_counts["student_id"] = (
                    row_counts.get("student_id", 0) + column_redactions
                )
            result.merge_redaction_counts(row_counts)
            # 表格行是完整语义单元：拆开会把单元格/列头绑定切断，宁可保留
            # 稍长的一块，也不按通用段落规则切开。
            parts = [row_text]
            if len(row_text) > max_chars:
                result.warnings.append(
                    f"表格 {table.table_id} 第 {row.row_index + 1} 行超过 {max_chars} 字，"
                    "为保留列绑定未拆分，请人工确认"
                )
            if any(cell.needs_review for cell in row.cells):
                result.warnings.append(
                    f"表格 {table.table_id} 第 {row.row_index + 1} 行含待复核单元格，已保留原生文本"
                )
            for part_no, part in enumerate(parts, start=1):
                article = _build_article(
                    version=version,
                    article_no=f"表格{table.table_index}第{row.row_index + 1}行",
                    article_index=10000 + row_counter,
                    chapter_path=chapter_path,
                    text=part,
                    block_type=BlockType.SECTION,
                    part_no=part_no,
                    duplicate=1,
                    topic_tags=topic_tags,
                    flow_ids=flow_ids,
                    source_format=parsed.format,
                )
                article.article_id = (
                    f"{parent_id}.row{row.row_index}"
                    + (f".part{part_no}" if len(parts) > 1 else "")
                )
                article.block_kind = "matrix_row" if table.kind == "matrix" else "table_row"
                article.parent_article_id = parent_id
                article.parent_table_id = table.table_id
                article.table_id = table.table_id
                article.row_id = row.row_id
                article.row_index = row.row_index
                article.table_headers = list(table.column_headers)
                article.header_paths = [list(path) for path in table.header_paths]
                article.table_column_bindings = _clone_binding_provenances(
                    binding_provenances
                )
                article.cell_provenance = _cell_provenance(
                    row.cells,
                    redact_contacts=redact_contacts,
                    sensitive_columns=sensitive,
                )
                article.page = table.page if table.page is not None else row.locator.page
                article.bbox = row.locator.bbox or table.bbox
                article.dom_path = row.locator.dom_path or table.locator.dom_path
                article.table_continuation_of = continuation_of
                article.table_header_source_id = header_source_id
                article.table_header_source_page = header_source_page
                cell_ids: list[str] = []
                for cell in row.cells:
                    source = cell
                    if cell.covered:
                        origin_row = (
                            table.rows[cell.origin_row]
                            if cell.origin_row < len(table.rows)
                            else None
                        )
                        if origin_row is not None and cell.origin_col < len(origin_row.cells):
                            source = origin_row.cells[cell.origin_col]
                    if source.text.strip() and source.cell_id not in cell_ids:
                        cell_ids.append(source.cell_id)
                article.source_cell_ids = cell_ids
                articles.append(article)
        if table.kind == "matrix":
            result.warnings.append(
                f"表格 {table.table_id} 识别为适用性矩阵，已按行块保存，未按课程清单解析"
            )
        if not data_rows:
            result.warnings.append(f"表格 {table.table_id} 没有数据行，仅保留父表块")
    return articles


def _segment_body(
    parsed: ParsedDocument,
    version: DocumentVersion,
    *,
    topic_tags: list[str],
    flow_ids: list[str],
    max_chars: int,
    redact_contacts: bool = True,
) -> SegmentResult:
    result = SegmentResult()
    text = parsed.text
    chapters = list(CHAPTER_RE.finditer(text))
    articles = list(ARTICLE_RE.finditer(text))
    if not articles:
        articles = list(ARTICLE_RE_LOOSE.finditer(text))
        if articles:
            result.used_loose_article_rule = True
            result.warnings.append("严格条款边界零命中，已回退到宽松规则，请人工抽查")
    else:
        # 少数 PDF/网页会漏掉“第二十四条学生…”中的分隔符。
        # 只补充严格规则未命中的缺失序号，避免把目录行或交叉引用当成正文条款。
        strict_starts = {match.start() for match in articles}
        strict_indices = {article_index_from_no(match.group("no")) for match in articles}
        max_index = max(strict_indices, default=0)
        missing = {index for index in range(1, max_index + 1) if index not in strict_indices}
        supplements = []
        for match in ARTICLE_RE_LOOSE.finditer(text):
            if match.start() in strict_starts:
                continue
            index = article_index_from_no(match.group("no"))
            if index in missing:
                supplements.append(match)
                missing.discard(index)
        if supplements:
            articles = sorted([*articles, *supplements], key=lambda match: match.start())
            result.used_loose_article_rule = True
            recovered = "、".join(match.group("no").strip() for match in supplements)
            result.warnings.append(f"已用宽松规则补充无分隔符条款：{recovered}")

    if not articles:
        sections = [match for match in SECTION_RE.finditer(text) if _plausible_section(match)]
        if sections:
            _add_sections(
                result,
                parsed=parsed,
                version=version,
                sections=sections,
                parts=list(PART_RE.finditer(text)),
                majors=list(MAJOR_HEADING_RE.finditer(text)),
                topic_tags=topic_tags,
                flow_ids=flow_ids,
                max_chars=max_chars,
                redact_contacts=redact_contacts,
            )
            if result.indexable_articles:
                _append_redaction_warning(result)
                return result
            result.articles.clear()
            result.warnings.append("节标题下没有可用正文，回退为整篇块")

        if version.doc_type is DocType.GUIDE and len(normalize_text(text)) >= PREFACE_MIN_CHARS:
            _add_paragraph_chunks(
                result,
                parsed=parsed,
                version=version,
                topic_tags=topic_tags,
                flow_ids=flow_ids,
                max_chars=max_chars,
                redact_contacts=redact_contacts,
            )
            if result.indexable_articles:
                _append_redaction_warning(result)
                return result
            result.articles.clear()

        result.warnings.append("未识别到任何“第 X 条”，整篇作为不可检索块保存")
        _add_parts(
            result,
            version=version,
            article_no="全文",
            article_index=0,
            chapter_path=[],
            text=text,
            block_type=BlockType.OTHER,
            duplicate=1,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )
        _append_redaction_warning(result)
        return result

    chapter_starts = [match.start() for match in chapters]

    def chapter_path_at(position: int) -> list[str]:
        path: list[str] = []
        for match in chapters:
            if match.start() < position:
                path = [match.group("title").strip()]
            else:
                break
        return path

    first_article_start = articles[0].start()
    first_chapter = next((start for start in chapter_starts if start < first_article_start), None)
    preface_end = first_chapter if first_chapter is not None else first_article_start
    preface = text[:preface_end]
    if len(normalize_text(preface)) >= PREFACE_MIN_CHARS:
        _add_parts(
            result,
            version=version,
            article_no="前言",
            article_index=0,
            chapter_path=[],
            text=preface,
            block_type=BlockType.PREFACE,
            duplicate=1,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )

    article_starts = [match.start() for match in articles]
    duplicate_counts: dict[str, int] = {}
    seen_indices: list[int] = []
    last_end = first_article_start

    signature = SIGNATURE_TAIL_RE.search(text)
    tail_start = len(text)
    if signature is not None and signature.start() > article_starts[-1]:
        tail_start = signature.start()

    for index, match in enumerate(articles):
        article_no = match.group("no").strip()
        article_index = article_index_from_no(article_no)
        seen_indices.append(article_index)

        next_article = article_starts[index + 1] if index + 1 < len(article_starts) else len(text)
        next_chapter = next((start for start in chapter_starts if start > match.start()), len(text))
        body_end = min(next_article, next_chapter)
        if index == len(articles) - 1:
            body_end = min(body_end, tail_start)
        body_start = match.start("rest")
        body = text[body_start:body_end]
        last_end = max(last_end, body_end)

        duplicate_counts[article_no] = duplicate_counts.get(article_no, 0) + 1
        duplicate = duplicate_counts[article_no]
        if duplicate > 1:
            result.duplicate_article_nos.append(article_no)
            result.warnings.append(f"条款号重复：{article_no}（第 {duplicate} 次出现）")

        _add_parts(
            result,
            version=version,
            article_no=article_no,
            article_index=article_index,
            chapter_path=chapter_path_at(match.start()),
            text=body,
            block_type=BlockType.ARTICLE,
            duplicate=duplicate,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )

    if result.duplicate_article_nos:
        result.duplicate_article_nos = sorted(set(result.duplicate_article_nos))

    valid_indices = sorted({index for index in seen_indices if index > 0})
    if valid_indices:
        present = set(valid_indices)
        missing = [index for index in range(1, max(valid_indices) + 1) if index not in present]
        result.missing_article_indices = missing
        if missing:
            preview = "、".join(str(index) for index in missing[:10])
            result.warnings.append(f"条款序号不连续，缺失：{preview}")

    tail = text[last_end:]
    if len(normalize_text(tail)) >= TAIL_MIN_CHARS:
        if any(keyword in tail[:50] for keyword in ("附件", "附表", "附录")):
            block_type = BlockType.APPENDIX
            warning = "尾部附件/附录已单独建块"
        else:
            block_type = BlockType.APPENDIX
            warning = "尾部文本已放入附录块，可能是落款或附件，请人工确认"
        result.warnings.append(warning)
        _add_parts(
            result,
            version=version,
            article_no="附录",
            article_index=9999,
            chapter_path=[],
            text=tail,
            block_type=block_type,
            duplicate=1,
            topic_tags=topic_tags,
            flow_ids=flow_ids,
            source_format=parsed.format,
            max_chars=max_chars,
            redact_contacts=redact_contacts,
        )
    elif normalize_text(tail):
        result.warnings.append("忽略尾部短文本（可能是落款/页码）")

    _append_redaction_warning(result)
    return result


def _sensitive_cell_values(parsed: ParsedDocument) -> set[str]:
    if parsed.structure is None:
        return set()
    values: set[str] = set()
    for table in parsed.structure.tables:
        sensitive = _sensitive_columns(table)
        if not sensitive:
            continue
        for row in table.rows:
            if row.is_header:
                continue
            for cell in row.cells:
                if cell.covered or not _cell_in_sensitive_column(cell, sensitive):
                    continue
                text = clean_whitespace(cell.text)
                if len(text) >= 6:
                    values.add(text)
    return values


def _scrub_sensitive_values(result: SegmentResult, parsed: ParsedDocument) -> None:
    """兜底清理正文中残留的表文副本；列头定位后只替换该列的真实值。"""
    # 只替换独立出现的完整值，避免敏感值恰好是更长课程代码的子串时
    # 把课程代码一起改写。
    patterns = [
        (value, re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"))
        for value in sorted(_sensitive_cell_values(parsed), key=len, reverse=True)
    ]
    if not patterns:
        return
    for article in result.articles:
        text = article.text
        replaced = 0
        for value, pattern in patterns:
            if value not in text:
                continue
            text, occurrences = pattern.subn(STUDENT_ID_PLACEHOLDER, text)
            replaced += occurrences
        if replaced:
            article.text = text
            article.text_normalized = normalize_text(text)
            result.redaction_counts["student_id"] = (
                result.redaction_counts.get("student_id", 0) + replaced
            )


def _segment_document(
    parsed: ParsedDocument,
    version: DocumentVersion,
    *,
    topic_tags: list[str],
    flow_ids: list[str],
    max_chars: int,
    redact_contacts: bool = True,
) -> SegmentResult:
    """正文按原切分规则处理，表格按完整行单独建块并保留父表。

    旧解析缓存没有 ``structure``，走原有路径；新结构才剔除正文表格占位，
    防止同一表格既进入正文块又进入行块。
    """
    spans = _table_spans(parsed)
    body_text = _table_free_text(parsed, spans)
    body_parsed = parsed.model_copy(update={"text": body_text}) if spans else parsed
    result = _segment_body(
        body_parsed,
        version,
        topic_tags=topic_tags,
        flow_ids=flow_ids,
        max_chars=max_chars,
        redact_contacts=redact_contacts,
    )
    table_articles = _build_table_articles(
        result,
        parsed=parsed,
        version=version,
        topic_tags=topic_tags,
        flow_ids=flow_ids,
        max_chars=max_chars,
        redact_contacts=redact_contacts,
    )
    result.articles.extend(table_articles)
    if spans and parsed.structure is not None and parsed.structure.tables and not table_articles:
        result.warnings.append("结构层有表格但未生成父/子块，请检查表格占位与正文定位")
    _scrub_sensitive_values(result, parsed)
    return result
