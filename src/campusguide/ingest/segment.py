"""条款切分：章/条边界识别、节级/段落回退、前言与附录分块、联系方式脱敏。

切分是 W1 的核心规则，采用纯正则 + 确定性处理：
- 制度类文档优先按“第 X 条”切分，章标题写入 chapter_path；
- 没有“第 X 条”时，先尝试“一、二、三”节级切分（培养方案、指南）；
- 仍无节标题的 guide 按段落切块，保证正文可检索；
- 超过 800 字的条款/节按段落或句子切成多个 part，共享同一个 article_id；
- 重复条款号/节标题追加 ~N 保证 article_id 唯一，并记录告警。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from campusguide.domain.enums import BlockType, DocType
from campusguide.domain.ids import article_index_from_no, make_article_id
from campusguide.domain.models import Article, DocumentVersion
from campusguide.domain.text import clean_whitespace, normalize_text
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.redact import redact_text

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
MAJOR_HEADING_RE = re.compile(
    r"(?m)^[ \t　]*(?P<title>[^\n]{2,50}?专业(?:人才培养)?方案"
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
        chapter_path=chapter_path,
        article_no=article_no,
        article_index=article_index,
        part_no=part_no,
        block_type=block_type,
        text=text,
        text_normalized=normalize_text(text),
        text_raw_ref=f"{version.doc_id}/{version.version_id}/source.{source_format}",
        audience=version.audience,
        audience_detail=version.audience_detail,
        status=version.status,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        source_url=version.source_url,
        published_at=version.published_at,
        date_confidence=version.date_confidence,
        issuing_department=version.issuing_department,
        topic_tags=topic_tags,
        flow_ids=flow_ids,
        source_sha256=version.source_sha256,
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


def _segment_document(
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
