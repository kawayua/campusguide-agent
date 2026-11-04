"""HTML/PDF 解析：提取正文、标题、表格，记录解析告警。

HTML 采用“候选正文容器 + CJK 字符数最多”策略；PDF 使用 PyMuPDF 文本层，
并做页眉页脚、页码的重复行清理。无文本层的扫描件在存在 OCR 旁路缓存
（`data/processed/ocr/{doc_id}/{version_id}.json`）且 SHA-256 匹配时，
改用 OCR 文本；解析过程本身不依赖 OCR 运行时。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel

from campusguide.domain.text import clean_whitespace, count_cjk, normalize_text
from campusguide.ingest.ocr import (
    OCR_MIN_CJK,
    OcrDocument,
    load_ocr_document,
    ocr_output_path,
)
from campusguide.ingest.structure import (
    AssetResource,
    PageStructure,
    ParsedStructure,
    SourceLocator,
    StructureBlock,
    TableStructure,
    build_html_structure,
    classify_table_kind,
    extract_html_assets,
    extract_html_table,
    extract_pdf_table,
    iter_html_blocks,
    restore_continuation_header,
)

HTML_PARSER_VERSION = "campusguide-html-0.2.0"
PDF_PARSER_VERSION = "campusguide-pdf-0.4.0"
PARSE_SCHEMA_VERSION = "2.0"

# 默认 find_tables 会把带合并表头的宽表拆成大量空列；仅对疑似拆分的表格，
# 按原 bbox 比例外扩后改用 lines_strict 重提取。候选必须覆盖原区域大部分
# 文本和数字，且表头覆盖更好，才替换默认结果。
PDF_TABLE_CLIP_PADDING_RATIO = 0.08
PDF_TABLE_CLIP_PADDING_MIN = 4.0
PDF_TABLE_CLIP_PADDING_MAX = 40.0
PDF_TABLE_RECOVERY_MIN_GAIN = 0.5
PDF_TABLE_RECOVERY_HEADER_COVERAGE = 0.75
PDF_TABLE_RECOVERY_EMPTY_COLUMN_RATIO = 0.3
PDF_TABLE_RECOVERY_TEXT_RECALL = 0.99
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*")

# 两表之间出现注记、章节/培养环节标题或班型/专业边界时，跨页续表不再继承表头。
_PDF_CONTINUATION_BREAK_RE = re.compile(
    r"^\s*(?:注\s*[：:]|第[一二三四五六七八九十百千0-9]+章"
    r"|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）"
    r"|\d+(?:\.\d+)+\s*[^\d\s])"
    r"|培养环节|课程设置|教学计划|开课单位|适用专业|适用学院|专业代码"
    r"|学术型|专业型|班级|研究方向"
)
_PDF_TABLE_EDGE_TOP_RATIO = 0.35
_PDF_TABLE_EDGE_BOTTOM_RATIO = 0.65

CONTENT_SELECTORS = (
    "#vsb_content",
    "#vsb_content_100",
    ".wp_articlecontent",
    ".v_news_content",
    ".paging_content",
    ".col_news_list",
    "#xzld",
    ".article-content",
    ".article_content",
    ".article",
    "article",
    "main",
    ".content",
    "td.content",
)

UNWANTED_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "form",
    "button",
    "svg",
    "canvas",
)

UNWANTED_ATTR_RE = re.compile(
    r"(^|[-_])(nav|menu|footer|header|breadcrumb|crumb|sidebar|side|share|print|search|"
    r"copyright|bottom|topbar|toolbar|login|qrcode)([-_]|$)",
    re.IGNORECASE,
)

PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:[-—–]\s*\d{1,4}\s*[-—–]|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+)\s*$"
)
# HTML 正文里的独立数字通常是年份、文号、金额，而不是页码。
# 只有带页码标记的形式才应丢弃，裸数字必须保留。
HTML_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:[-—–]\s*\d{1,4}\s*[-—–]|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+)\s*$"
)

BLOCK_TAGS = (
    "p",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "div",
    "td",
    "th",
)
ARTICLE_HEADING_RE = re.compile(
    r"^(第[一二三四五六七八九十百千零〇两0-9]{1,12}条(?:之[一二三四五六七八九十百千零〇两0-9]{1,3})?)(?=\S)"
)
CHAPTER_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百千零〇两0-9]{1,12}章)(?=\S)")

DROP_LINE_RE = re.compile(
    r"^(上一篇|下一篇|返回列表|打印本页|关闭窗口|分享到|扫一扫|版权所有|Copyright|技术支持)"
)

PDF_TITLE_HINT_RE = re.compile(r"(办法|规定|细则|通知|指南|方案|意见|条例|章程|制度)")


class ParsedDocument(BaseModel):
    schema_version: str = PARSE_SCHEMA_VERSION
    doc_id: str
    version_id: str
    title: str
    text: str
    format: Literal["html", "pdf"]
    source_sha256: str
    source_url: str | None = None
    local_path: str | None = None
    fetched_at: str | None = None
    parser_version: str
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    page_count: int | None = None
    cjk_count: int = 0
    char_count: int = 0
    ocr_used: bool = False
    ocr_confidence: float | None = None
    structure: ParsedStructure | None = None
    vision: dict[str, object] | None = None
    ingestion_mode: Literal["legacy", "native_only", "vision_first"] = "legacy"


@dataclass
class ParseOutput:
    title: str
    text: str
    format: Literal["html", "pdf"]
    parser_version: str
    doc_id: str = ""
    version_id: str = ""
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    page_count: int | None = None
    ocr_used: bool = False
    ocr_confidence: float | None = None
    structure: ParsedStructure | None = None
    vision: dict[str, object] | None = None
    ingestion_mode: Literal["legacy", "native_only", "vision_first"] = "legacy"


@dataclass
class ParseResult:
    doc_id: str
    version_id: str
    status: Literal["parsed", "skipped", "error"]
    output_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    cjk_count: int = 0
    ocr_used: bool = False
    ocr_confidence: float | None = None
    ingestion_mode: str = "legacy"
    vision_pages: int = 0
    source_sha256: str | None = None


def _decode_html(raw: bytes, content_type: str | None) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    encodings: list[str] = []
    if content_type and "charset=" in content_type.lower():
        encodings.append(content_type.lower().split("charset=", 1)[1].split(";")[0].strip())
    encodings.extend(["utf-8-sig", "utf-8", "gb18030"])

    for encoding in dict.fromkeys(encodings):
        try:
            return raw.decode(encoding), encoding, warnings
        except (LookupError, UnicodeDecodeError):
            continue

    warnings.append("HTML 编码无法确定，使用 UTF-8 replacement 解码")
    return raw.decode("utf-8", errors="replace"), "utf-8", warnings


def _remove_unwanted(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(UNWANTED_TAGS):
        tag.decompose()

    to_remove = []
    for element in soup.find_all(True):
        classes = " ".join(element.get("class", []))
        identifier = element.get("id", "")
        if UNWANTED_ATTR_RE.search(classes) or UNWANTED_ATTR_RE.search(identifier):
            to_remove.append(element)
    for element in to_remove:
        if element.parent is not None:
            element.decompose()


def _extract_title(soup: BeautifulSoup, fallback: str | None) -> str:
    for selector in ("h1", ".article-title", ".article_title", ".title", "title"):
        element = soup.select_one(selector)
        if element:
            title = clean_whitespace(element.get_text(" ", strip=True))
            if title:
                for separator in ("_", "—", "|"):
                    if separator in title:
                        title = title.split(separator, 1)[0].strip()
                if title:
                    return title
    return (fallback or "").strip()


def _tables_to_text(
    content: BeautifulSoup,
    *,
    doc_id: str = "",
    version_id: str = "",
) -> tuple[list[str], list[str], list[TableStructure]]:
    table_blocks: list[str] = []
    warnings: list[str] = []
    structures: list[TableStructure] = []
    tables = [table for table in content.find_all("table") if table.find_parent("table") is None]
    for index, table in enumerate(tables, start=1):
        table_structure, rendered = extract_html_table(
            table,
            doc_id=doc_id or "doc",
            version_id=version_id or "version",
            table_index=index,
            block_id=f"{doc_id or 'doc'}@{version_id or 'version'}#tableblock{index}",
            root=content,
        )
        if table_structure.kind == "layout" or not rendered:
            table.unwrap()
            warnings.append("检测到布局表格，已按段落展开")
            continue
        structures.append(table_structure)
        table_blocks.append(rendered)
        # 用块级节点占位，保留表格在正文中的原始位置；结构层通过
        # data-table-id 关联完整网格，正文文本保持旧格式以兼容切分。
        replacement = table.new_tag("p")
        replacement["data-table-id"] = table_structure.table_id
        replacement.string = f"【表格】\n{rendered}"
        table.replace_with(replacement)
    if len(table_blocks) > 3:
        warnings.append(f"检测到 {len(table_blocks)} 个表格，已转为文本，请人工抽查")
    return table_blocks, warnings, structures


def _extract_content(soup: BeautifulSoup) -> tuple[BeautifulSoup, list[str]]:
    warnings: list[str] = []
    candidates = []
    seen: set[int] = set()
    for selector in CONTENT_SELECTORS:
        for element in soup.select(selector):
            if id(element) not in seen:
                seen.add(id(element))
                candidates.append(element)

    if candidates:
        # 学院 CMS 的 .paging_content 是明确的主内容容器；委员会名单等
        # 页面本身只有几十个汉字，且与 .wp_articlecontent 同文，按 50 字
        # 门槛会误回退到带导航的 body。这里优先采用它。
        paging = [
            element for element in candidates if "paging_content" in (element.get("class") or [])
        ]
        if paging:
            paging_score, _, paging_best = max(
                (
                    (count_cjk(element.get_text(" ", strip=True)), index, element)
                    for index, element in enumerate(paging)
                ),
                key=lambda item: (item[0], -item[1]),
            )
            if paging_score >= 20:
                return paging_best, warnings

        scored = [
            (count_cjk(element.get_text(" ", strip=True)), index, element)
            for index, element in enumerate(candidates)
        ]
        score, _, best = max(scored, key=lambda item: (item[0], -item[1]))
        if score >= 50:
            return best, warnings
        warnings.append("候选正文容器过短，回退到 body")
    else:
        warnings.append("未匹配到正文容器，回退到 body")

    body = soup.body or soup
    return body, warnings


def _extract_html_text(content: BeautifulSoup) -> str:
    """按块级元素取正文，保留段落但合并同一段内的行内 span。

    与结构层共用 ``iter_html_blocks``，保证 ``ParsedStructure`` 的块文本
    与切分输入完全一致；表头空格、合并单元格文本均来自原 DOM。
    """
    texts = [text for _element, text, _kind in iter_html_blocks(content)]
    if not texts:
        return content.get_text("\n")
    return "\n".join(texts)


def _drop_noise_lines(
    text: str,
    *,
    page_number_re: re.Pattern[str] = PAGE_NUMBER_RE,
) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if DROP_LINE_RE.search(stripped):
            continue
        if page_number_re.match(stripped):
            continue
        lines.append(stripped)
    return clean_whitespace("\n".join(lines))


def parse_html_file(
    path: Path,
    *,
    title_hint: str | None = None,
    content_type: str | None = None,
    doc_id: str = "",
    version_id: str = "",
) -> ParseOutput:
    raw = path.read_bytes()
    html, encoding, warnings = _decode_html(raw, content_type)
    if encoding not in {"utf-8", "utf-8-sig"}:
        warnings.append(f"HTML 使用 {encoding} 解码")

    soup = BeautifulSoup(html, "lxml")
    _remove_unwanted(soup)
    title = _extract_title(soup, title_hint)
    content, content_warnings = _extract_content(soup)
    warnings.extend(content_warnings)
    tables, table_warnings, table_structures = _tables_to_text(
        content,
        doc_id=doc_id,
        version_id=version_id,
    )
    warnings.extend(table_warnings)
    assets, asset_warnings = extract_html_assets(
        content,
        doc_id=doc_id or "doc",
        version_id=version_id or "version",
        root=content,
    )
    warnings.extend(asset_warnings)

    text = _drop_noise_lines(
        _extract_html_text(content),
        page_number_re=HTML_PAGE_NUMBER_RE,
    )
    if count_cjk(text) < 200:
        warnings.append("正文 CJK 字符少于 200，可能存在解析不全")
    if not text:
        warnings.append("解析结果为空")

    structure = build_html_structure(
        content,
        doc_id=doc_id or "doc",
        version_id=version_id or "version",
        text=text,
        tables=table_structures,
        assets=assets,
        warnings=[],
        parser_version=HTML_PARSER_VERSION,
    )
    for table in structure.tables:
        table.kind = classify_table_kind(table)
    structure.coverage.update(
        {
            "table_rows": sum(table.row_count for table in structure.tables),
            "data_tables": sum(1 for table in structure.tables if table.kind == "data"),
            "matrix_tables": sum(1 for table in structure.tables if table.kind == "matrix"),
            "attachments": sum(1 for asset in assets if asset.kind == "attachment"),
        }
    )

    return ParseOutput(
        title=title or title_hint or "",
        text=text,
        format="html",
        parser_version=HTML_PARSER_VERSION,
        doc_id=doc_id,
        version_id=version_id,
        warnings=warnings,
        tables=tables,
        structure=structure,
        ingestion_mode="native_only",
    )


def _page_number_or_repeated(
    pages: list[list[str]],
) -> tuple[list[list[str]], list[str]]:
    warnings: list[str] = []
    if not pages:
        return pages, warnings

    counts = Counter(
        line
        for page in pages
        for line in page
        if line and len(line) <= 80 and not PAGE_NUMBER_RE.match(line) and not line.isdigit()
    )
    threshold = max(2, math.ceil(len(pages) * 0.5))
    repeated = {line for line, count in counts.items() if count >= threshold}

    cleaned_pages: list[list[str]] = []
    for page in pages:
        non_empty_positions = [index for index, line in enumerate(page) if line]
        edge_positions = set(non_empty_positions[:2] + non_empty_positions[-2:])
        cleaned = []
        for index, line in enumerate(page):
            if not line:
                continue
            if PAGE_NUMBER_RE.match(line):
                continue
            if line.isdigit():
                # 裸数字可能是年份、金额、表格值，不能当作页码删除。
                cleaned.append(line)
                continue
            if index in edge_positions and line in repeated and not line.startswith("第"):
                continue
            cleaned.append(line)
        cleaned_pages.append(cleaned if cleaned else page)

    if repeated:
        warnings.append(f"PDF 检测到 {len(repeated)} 条重复页眉/页脚文本，已在页边缘清理")
    return cleaned_pages, warnings


def _bbox_area(bbox: list[float] | tuple[float, ...]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def _intersection_area(
    left: list[float] | tuple[float, ...],
    right: list[float] | tuple[float, ...],
) -> float:
    width = max(0.0, min(float(left[2]), float(right[2])) - max(float(left[0]), float(right[0])))
    height = max(0.0, min(float(left[3]), float(right[3])) - max(float(left[1]), float(right[1])))
    return width * height


def _mostly_inside(
    block_bbox: list[float] | tuple[float, ...],
    table_bbox: list[float] | tuple[float, ...] | None,
) -> bool:
    if table_bbox is None:
        return False
    block_area = _bbox_area(block_bbox)
    if block_area <= 0:
        return False
    return _intersection_area(block_bbox, table_bbox) / block_area >= 0.6


def _pdf_table_bbox(table: Any) -> list[float] | None:
    bbox = getattr(table, "bbox", None)
    if bbox is None:
        return None
    try:
        values = [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
    except (TypeError, ValueError, IndexError):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _pdf_table_extract(table: Any) -> list[list[Any]]:
    try:
        extracted = table.extract()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return []
    rows: list[list[Any]] = []
    for row in extracted or []:
        rows.append(list(row or []))
    return rows


def _pdf_table_text(extracted: list[list[Any]]) -> str:
    return "".join(
        str(value).strip()
        for row in extracted
        for value in row
        if value is not None and str(value).strip()
    )


def _pdf_table_stats(
    table: Any,
    extracted: list[list[Any]],
    *,
    reference_text: str,
) -> dict[str, float]:
    column_count = int(getattr(table, "col_count", 0) or 0)
    if not column_count:
        column_count = max((len(row) for row in extracted), default=0)
    row_count = max(len(extracted), int(getattr(table, "row_count", 0) or 0))
    if column_count <= 0 or row_count <= 0:
        return {
            "column_count": 0.0,
            "row_count": 0.0,
            "header_coverage": 0.0,
            "header_numeric_ratio": 1.0,
            "column_fill": 0.0,
            "body_density": 0.0,
            "empty_column_ratio": 1.0,
            "char_recall": 0.0,
            "number_recall": 0.0,
        }
    header_rows = int(getattr(table, "header_rows", 0) or 0)
    header_rows = max(1, min(header_rows or 1, len(extracted) or 1))
    header = extracted[:header_rows]
    body = extracted[header_rows:]
    header_nonempty = [False] * column_count
    header_cells = 0
    header_numeric = 0
    for row in header:
        for col, value in enumerate(row[:column_count]):
            text = str(value).strip() if value is not None else ""
            if not text:
                continue
            header_nonempty[col] = True
            header_cells += 1
            if re.fullmatch(r"[\d.,%/\-]+", "".join(text.split())):
                header_numeric += 1
    header_coverage = sum(header_nonempty) / column_count
    header_numeric_ratio = header_numeric / max(1, header_cells)
    column_filled: list[int] = []
    empty_columns = 0
    body_filled = 0
    for col in range(column_count):
        nonempty = sum(
            1
            for row in body
            if col < len(row) and row[col] is not None and str(row[col]).strip()
        )
        column_filled.append(nonempty)
        body_filled += nonempty
        if nonempty == 0 and not header_nonempty[col]:
            empty_columns += 1
    body_len = max(1, len(body))
    column_fill = sum(min(1.0, value / body_len) for value in column_filled) / column_count
    body_density = body_filled / max(1, len(body) * column_count)
    empty_column_ratio = empty_columns / column_count
    text = _pdf_table_text(extracted)
    chars = {char for char in text if not char.isspace()}
    reference_chars = {char for char in reference_text if not char.isspace()}
    char_recall = (
        len(chars & reference_chars) / len(reference_chars) if reference_chars else 0.0
    )
    numbers = set(_NUMBER_TOKEN_RE.findall(text))
    reference_numbers = set(_NUMBER_TOKEN_RE.findall(reference_text))
    number_recall = (
        len(numbers & reference_numbers) / len(reference_numbers)
        if reference_numbers
        else 1.0
    )
    return {
        "column_count": float(column_count),
        "row_count": float(row_count),
        "header_coverage": header_coverage,
        "header_numeric_ratio": header_numeric_ratio,
        "column_fill": column_fill,
        "body_density": body_density,
        "empty_column_ratio": empty_column_ratio,
        "char_recall": char_recall,
        "number_recall": number_recall,
    }


def _pdf_table_score(stats: dict[str, float]) -> float:
    return (
        2.5 * stats["header_coverage"]
        + 1.2 * stats["char_recall"]
        + 1.0 * stats["number_recall"]
        + 1.5 * stats["column_fill"]
        + 0.5 * stats["body_density"]
        - 1.0 * stats["empty_column_ratio"]
        - 1.5 * stats["header_numeric_ratio"]
    )


def _pdf_table_needs_recovery(stats: dict[str, float]) -> bool:
    return int(stats["column_count"]) >= 4 and (
        stats["header_coverage"] < PDF_TABLE_RECOVERY_HEADER_COVERAGE
        or stats["empty_column_ratio"] > PDF_TABLE_RECOVERY_EMPTY_COLUMN_RATIO
    )


def _pdf_table_padding_candidates(
    bbox: list[float],
    *,
    padding_ratio: float,
) -> list[float]:
    short_side = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
    base = min(
        PDF_TABLE_CLIP_PADDING_MAX,
        max(PDF_TABLE_CLIP_PADDING_MIN, short_side * padding_ratio),
    )
    values: list[float] = []
    for factor in (0.5, 1.0, 1.5):
        value = round(base * factor, 2)
        if value >= PDF_TABLE_CLIP_PADDING_MIN and value not in values:
            values.append(value)
    return values


def _pdf_page_bounds(page: Any) -> tuple[float, float, float, float] | None:
    rect = getattr(page, "rect", None)
    if rect is None:
        return None
    try:
        values = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    except (AttributeError, TypeError, ValueError):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _pdf_padded_clip(
    bbox: list[float],
    padding: float,
    bounds: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    x0 -= padding
    y0 -= padding
    x1 += padding
    y1 += padding
    if bounds is not None:
        x0 = max(bounds[0], x0)
        y0 = max(bounds[1], y0)
        x1 = min(bounds[2], x1)
        y1 = min(bounds[3], y1)
    return (x0, y0, x1, y1)


def _pdf_find_tables(page: Any, **kwargs: Any) -> Any | None:
    """兼容不同 PyMuPDF/页面替身：kwargs 不支持时逐级降级，不中断整页解析。"""
    attempts: list[dict[str, Any]] = [dict(kwargs)]
    if "use_layout" in kwargs:
        attempts.append({key: value for key, value in kwargs.items() if key != "use_layout"})
    if "strategy" in attempts[-1]:
        attempts.append(
            {key: value for key, value in attempts[-1].items() if key != "strategy"}
        )
    tried: list[tuple[str, ...]] = []
    for attempt in attempts:
        signature = tuple(sorted(attempt))
        if signature in tried:
            continue
        tried.append(signature)
        try:
            return page.find_tables(**attempt)
        except TypeError:
            continue
        except (AttributeError, RuntimeError, ValueError):
            return None
    return None


def _pdf_recover_split_table(
    page: Any,
    table: Any,
    *,
    page_no: int,
    page_bounds: tuple[float, float, float, float] | None,
    blocked_bboxes: list[list[float]],
    padding_ratio: float,
) -> tuple[Any, list[str]] | None:
    bbox = _pdf_table_bbox(table)
    if bbox is None:
        return None
    extracted = _pdf_table_extract(table)
    reference_text = _pdf_table_text(extracted)
    if not reference_text:
        return None
    base_stats = _pdf_table_stats(
        table,
        extracted,
        reference_text=reference_text,
    )
    base_score = _pdf_table_score(base_stats)
    best: tuple[float, float, Any, dict[str, float]] | None = None
    for padding in _pdf_table_padding_candidates(bbox, padding_ratio=padding_ratio):
        clip = _pdf_padded_clip(bbox, padding, page_bounds)
        found = _pdf_find_tables(
            page,
            clip=clip,
            use_layout=False,
            strategy="lines_strict",
        )
        for candidate in getattr(found, "tables", []) or []:
            candidate_bbox = _pdf_table_bbox(candidate)
            if candidate_bbox is None:
                continue
            coverage = _intersection_area(candidate_bbox, bbox) / max(
                1.0, _bbox_area(bbox)
            )
            if coverage < 0.75:
                continue
            if _bbox_area(candidate_bbox) > _bbox_area(bbox) * 2.2:
                continue
            if candidate_bbox[3] - candidate_bbox[1] > (bbox[3] - bbox[1]) * 1.6:
                continue
            if any(
                _intersection_area(candidate_bbox, blocked)
                / max(1.0, _bbox_area(candidate_bbox))
                >= 0.2
                for blocked in blocked_bboxes
            ):
                continue
            extracted = _pdf_table_extract(candidate)
            stats = _pdf_table_stats(
                candidate,
                extracted,
                reference_text=reference_text,
            )
            if (
                stats["char_recall"] < PDF_TABLE_RECOVERY_TEXT_RECALL
                or stats["number_recall"] < PDF_TABLE_RECOVERY_TEXT_RECALL
            ):
                continue
            score = _pdf_table_score(stats)
            if best is None or score > best[0]:
                best = (score, padding, candidate, stats)
    if best is None or best[0] < base_score + PDF_TABLE_RECOVERY_MIN_GAIN:
        return None
    score, padding, candidate, stats = best
    warnings = [
        f"第 {page_no} 页表格默认检测疑似拆分列，已按原区域外扩 {padding:.0f}pt 重新提取"
        f"（{int(stats['column_count'])} 列 / {int(stats['row_count'])} 行）"
    ]
    if (
        stats["header_coverage"] < PDF_TABLE_RECOVERY_HEADER_COVERAGE
        or stats["char_recall"] < 0.995
    ):
        warnings.append("重新提取的表格结构仍需复核：表头或文本覆盖不完整")
    return candidate, warnings


def _pdf_page_tables(
    page: Any,
    *,
    page_no: int,
    table_recovery: bool,
    padding_ratio: float,
) -> list[tuple[Any, list[str]]]:
    found = _pdf_find_tables(page)
    raw_tables = list(getattr(found, "tables", []) or [])
    if not raw_tables:
        return []
    page_bounds = _pdf_page_bounds(page)
    default_bboxes = [_pdf_table_bbox(raw) for raw in raw_tables]
    selected: list[tuple[Any, list[str]]] = []
    for index, raw in enumerate(raw_tables):
        choice = raw
        recovery_warnings: list[str] = []
        if table_recovery:
            extracted = _pdf_table_extract(raw)
            stats = _pdf_table_stats(
                raw,
                extracted,
                reference_text=_pdf_table_text(extracted),
            )
            if _pdf_table_needs_recovery(stats):
                blocked = [
                    bbox
                    for position, bbox in enumerate(default_bboxes)
                    if position != index and bbox is not None
                ]
                recovered = _pdf_recover_split_table(
                    page,
                    raw,
                    page_no=page_no,
                    page_bounds=page_bounds,
                    blocked_bboxes=blocked,
                    padding_ratio=padding_ratio,
                )
                if recovered is not None:
                    choice, recovery_warnings = recovered
        selected.append((choice, recovery_warnings))
    return selected


def _pdf_table_near_page_edge(
    table: TableStructure, *, page_height: float | None, at_top: bool
) -> bool:
    if table.bbox is None or len(table.bbox) != 4 or not page_height:
        return False
    if at_top:
        return float(table.bbox[1]) <= page_height * _PDF_TABLE_EDGE_TOP_RATIO
    return float(table.bbox[3]) >= page_height * _PDF_TABLE_EDGE_BOTTOM_RATIO


def _pdf_continuation_blocked(
    previous: TableStructure,
    current: TableStructure,
    previous_blocks: list[StructureBlock],
    current_blocks: list[StructureBlock],
) -> bool:
    """两表之间出现注记、章节/培养环节或班型边界时断开续表。"""
    if previous.bbox is None or current.bbox is None:
        return True
    previous_bottom = float(previous.bbox[3])
    current_top = float(current.bbox[1])
    for block in previous_blocks:
        if block.kind != "paragraph" or not block.text.strip():
            continue
        bbox = block.locator.bbox
        if bbox is None or len(bbox) != 4 or float(bbox[3]) < previous_bottom - 1.0:
            continue
        if _PDF_CONTINUATION_BREAK_RE.search(block.text):
            return True
    for block in current_blocks:
        if block.kind != "paragraph" or not block.text.strip():
            continue
        bbox = block.locator.bbox
        if bbox is None or len(bbox) != 4 or float(bbox[1]) > current_top + 1.0:
            continue
        if _PDF_CONTINUATION_BREAK_RE.search(block.text):
            return True
    return False


def _pdf_page_parts(
    page: object,
    *,
    page_no: int,
    doc_id: str,
    version_id: str,
    table_index_start: int,
    block_order_start: int,
    table_recovery: bool = True,
    table_clip_padding_ratio: float = PDF_TABLE_CLIP_PADDING_RATIO,
) -> tuple[
    list[str],
    list[StructureBlock],
    list[TableStructure],
    list[AssetResource],
    PageStructure,
    list[str],
    int,
    int,
]:
    """把单页拆成按 y 坐标排序的正文/表格块；表格留在原页位置。"""
    pymupdf_warnings: list[str] = []
    raw_text = str(page.get_text("text", sort=True) or "")  # type: ignore[attr-defined]
    page_rect = page.rect  # type: ignore[attr-defined]
    page_tables: list[TableStructure] = []
    table_bboxes: list[list[float] | None] = []
    table_index = table_index_start
    try:
        raw_tables = _pdf_page_tables(
            page,
            page_no=page_no,
            table_recovery=table_recovery,
            padding_ratio=table_clip_padding_ratio,
        )
    except (AttributeError, RuntimeError, ValueError):
        raw_tables = []
    for raw_table, recovery_warnings in raw_tables:
        table_index += 1
        structure, rendered = extract_pdf_table(
            raw_table,
            doc_id=doc_id,
            version_id=version_id,
            table_index=table_index,
            page=page_no,
            block_id=f"{doc_id}@{version_id}#tableblock{table_index}",
            source_page=page,
        )
        if not any(cell.text for row in structure.rows for cell in row.cells):
            continue
        structure.kind = classify_table_kind(structure)
        if recovery_warnings:
            structure.warnings.extend(recovery_warnings)
            pymupdf_warnings.extend(recovery_warnings)
        page_tables.append(structure)
        table_bboxes.append(structure.bbox)
        if structure.bbox and normalize_text(rendered) and normalize_text(rendered) in normalize_text(
            raw_text
        ):
            structure.warnings.append(
                "表格文本也出现在页面文本层；已尝试按表格 bbox 去重，请核对是否残留"
            )

    items: list[tuple[float, float, str, bool, list[float] | None]] = []
    blocks = page.get_text("blocks", sort=True)  # type: ignore[attr-defined]
    for block in blocks:
        if len(block) < 5:
            continue
        bbox = [float(block[0]), float(block[1]), float(block[2]), float(block[3])]
        block_type = int(block[6]) if len(block) > 6 else 0
        if block_type != 0:
            continue
        if any(_mostly_inside(bbox, table_bbox) for table_bbox in table_bboxes):
            continue
        text = str(block[4]).strip()
        if text:
            items.append((bbox[1], bbox[0], text, False, bbox))
    for structure in page_tables:
        rendered = "\n".join(
            row.text for row in structure.rows if row.text.strip()
        )
        if not rendered:
            continue
        bbox = structure.bbox
        y0 = bbox[1] if bbox else 0.0
        x0 = bbox[0] if bbox else 0.0
        items.append((y0, x0, f"【表格】\n{rendered}", True, bbox))
    items.sort(key=lambda item: (item[0], item[1]))

    lines: list[str] = []
    structure_blocks: list[StructureBlock] = []
    block_order = block_order_start
    for _y, _x, text, is_table, bbox in items:
        locator = SourceLocator(format="pdf", page=page_no, bbox=bbox)
        if is_table:
            table = next(
                (
                    item
                    for item in page_tables
                    if item.bbox == bbox
                ),
                None,
            )
            lines.append(text)
            block = StructureBlock(
                block_id=f"{doc_id}@{version_id}#block{block_order}",
                order=block_order,
                kind="table",
                text=text,
                locator=locator,
                table_id=table.table_id if table else None,
            )
            if table is not None:
                table.block_id = block.block_id
            structure_blocks.append(block)
        else:
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
            structure_blocks.append(
                StructureBlock(
                    block_id=f"{doc_id}@{version_id}#block{block_order}",
                    order=block_order,
                    kind="paragraph",
                    text=text,
                    locator=locator,
                )
            )
        block_order += 1

    assets: list[AssetResource] = []
    image_count = 0
    try:
        images = page.get_images(full=True)  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        images = []
    for image in images:
        if not image:
            continue
        xref = image[0]
        try:
            rects = page.get_image_rects(xref)  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError):
            rects = []
        for rect in rects:
            image_count += 1
            bbox = [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]
            decorative = rect.width <= 48 and rect.height <= 48
            assets.append(
                AssetResource(
                    asset_id=f"{doc_id}@{version_id}#pdf{page_no}img{image_count}",
                    kind="image",
                    src=f"pdf:page{page_no}:xref{xref}",
                    locator=SourceLocator(format="pdf", page=page_no, bbox=bbox),
                    factual=not decorative,
                    recognized=decorative,
                    note="PDF 图片内容未做 OCR，需视觉识别" if not decorative else "小图标/装饰图",
                )
            )
    if image_count:
        pymupdf_warnings.append(f"第 {page_no} 页含 {image_count} 张图片，内容需视觉识别")

    native_text = raw_text + "\n" + "\n".join(
        "\n".join(row.text for row in table.rows) for table in page_tables
    )
    needs_ocr = count_cjk(native_text) < 10
    page_structure = PageStructure(
        page=page_no,
        width=float(page_rect.width),
        height=float(page_rect.height),
        text_chars=count_cjk(native_text),
        has_text_layer=not needs_ocr,
        needs_ocr=needs_ocr,
        warnings=["页面几乎没有文本层，需要 OCR/视觉识别"] if needs_ocr else [],
    )
    return (
        lines,
        structure_blocks,
        page_tables,
        assets,
        page_structure,
        pymupdf_warnings,
        table_index,
        block_order,
    )


def parse_pdf_file(
    path: Path,
    *,
    title_hint: str | None = None,
    doc_id: str = "",
    version_id: str = "",
    table_recovery: bool = True,
    table_clip_padding_ratio: float = PDF_TABLE_CLIP_PADDING_RATIO,
) -> ParseOutput:
    import pymupdf

    warnings: list[str] = []
    document = pymupdf.open(path)
    try:
        metadata_title = (document.metadata or {}).get("title") or ""
        pages: list[list[str]] = []
        page_texts: list[str] = []
        page_structures: list[PageStructure] = []
        structure_tables: list[TableStructure] = []
        structure_blocks: list[StructureBlock] = []
        page_block_lists: list[list[StructureBlock]] = []
        assets: list[AssetResource] = []
        empty_pages = 0
        table_index = 0
        block_order = 0
        previous_table: TableStructure | None = None
        doc_label = doc_id or path.stem
        version_label = version_id or "version"
        for page_no, page in enumerate(document, start=1):
            raw_text = str(page.get_text("text", sort=True) or "")
            page_texts.append(raw_text)
            (
                lines,
                blocks,
                page_tables,
                page_assets,
                page_structure,
                page_warnings,
                table_index,
                block_order,
            ) = _pdf_page_parts(
                page,
                page_no=page_no,
                doc_id=doc_label,
                version_id=version_label,
                table_index_start=table_index,
                block_order_start=block_order,
                table_recovery=table_recovery,
                table_clip_padding_ratio=table_clip_padding_ratio,
            )
            previous_height = (
                page_structures[-1].height if page_structures else None
            )
            previous_blocks = page_block_lists[-1] if page_block_lists else []
            for table in page_tables:
                if (
                    previous_table is not None
                    and previous_table.page is not None
                    and table.page is not None
                    and table.page == previous_table.page + 1
                    and _pdf_table_near_page_edge(
                        previous_table, page_height=previous_height, at_top=False
                    )
                    and _pdf_table_near_page_edge(
                        table, page_height=page_structure.height, at_top=True
                    )
                    and not _pdf_continuation_blocked(
                        previous_table, table, previous_blocks, blocks
                    )
                ):
                    restore_continuation_header(previous_table, table)
                previous_table = table
            warnings.extend(page_warnings)
            pages.append(lines)
            page_block_lists.append(blocks)
            structure_tables.extend(page_tables)
            structure_blocks.extend(blocks)
            assets.extend(page_assets)
            page_structures.append(page_structure)
            if page_structure.needs_ocr:
                empty_pages += 1

        cleaned_pages, header_warnings = _page_number_or_repeated(pages)
        warnings.extend(header_warnings)
        text = "\n\n".join("\n".join(lines) for lines in cleaned_pages if lines)
        text = clean_whitespace(text)

        if empty_pages:
            warnings.append(
                f"{empty_pages}/{len(page_texts)} 页几乎没有文本层，可能为扫描件，需要 OCR/视觉识别"
            )
        if structure_tables:
            warnings.append(
                f"PDF 检测到 {len(structure_tables)} 个表格，已保留在原页位置并按完整网格重建"
            )
        if count_cjk(text) < 200:
            warnings.append("PDF 文本 CJK 字符少于 200，可能存在解析不全")
        if not text:
            warnings.append("PDF 未提取到文本")

        title = metadata_title.strip()
        if not title:
            for line in text.split("\n")[:20]:
                if line and PDF_TITLE_HINT_RE.search(line) and len(line) <= 80:
                    title = line
                    break

        structure = ParsedStructure(
            extractor_version=f"{PDF_PARSER_VERSION}+pymupdf-{pymupdf.__version__}",
            format="pdf",
            blocks=structure_blocks,
            tables=structure_tables,
            pages=page_structures,
            assets=assets,
            reading_order=[block.block_id for block in structure_blocks],
            warnings=[
                warning for page in page_structures for warning in page.warnings
            ],
            coverage={
                "pages": len(page_structures),
                "pages_with_text": sum(
                    1 for page in page_structures if page.has_text_layer
                ),
                "pages_needing_ocr": sum(1 for page in page_structures if page.needs_ocr),
                "tables": len(structure_tables),
                "table_rows": sum(table.row_count for table in structure_tables),
                "images": len(assets),
            },
            vision=None,
        )
        return ParseOutput(
            title=title or title_hint or "",
            text=text,
            format="pdf",
            parser_version=f"{PDF_PARSER_VERSION}+pymupdf-{pymupdf.__version__}",
            doc_id=doc_id,
            version_id=version_id,
            warnings=warnings,
            tables=[
                "\n".join(
                    row.text for row in table.rows if row.text.strip()
                )
                for table in structure_tables
            ],
            page_count=len(page_texts),
            structure=structure,
            ingestion_mode="native_only",
        )
    finally:
        document.close()


def apply_ocr_document(
    parsed: ParseOutput,
    ocr: OcrDocument,
    *,
    title_hint: str | None = None,
) -> ParseOutput:
    """用 OCR 旁路文本替换空文本层的结果，保留原始解析告警。"""
    pages = [
        [line.strip() for line in page.text.splitlines() if line.strip()] for page in ocr.pages
    ]
    cleaned_pages, cleanup_warnings = _page_number_or_repeated(pages)
    text = clean_whitespace("\n\n".join("\n".join(lines) for lines in cleaned_pages if lines))

    warnings: list[str] = []
    for warning in parsed.warnings:
        if warning == "PDF 未提取到文本" or warning.startswith("PDF 文本 CJK 字符少于 200"):
            continue
        if "页几乎没有文本层" in warning:
            warning = warning.replace("需要 OCR", "已用 OCR 旁路补全")
        warnings.append(warning)
    warnings.extend(warning.replace("PDF 检测到", "OCR 检测到") for warning in cleanup_warnings)
    warnings.append(
        f"使用 OCR 旁路：{ocr.page_count} 页，平均置信度 {ocr.mean_confidence:.3f}；"
        "文本未经逐字校对，表格按 OCR 行序保留"
    )
    warnings.extend(
        warning if warning.startswith("多尺度 OCR") else f"OCR：{warning}"
        for warning in ocr.warnings
    )
    if count_cjk(text) < 200:
        warnings.append("OCR 文本 CJK 字符少于 200，可能识别不全")

    title = (title_hint or "").strip() or parsed.title
    if not title:
        for line in text.split("\n")[:20]:
            if line and PDF_TITLE_HINT_RE.search(line) and len(line) <= 80:
                title = line
                break

    model = f"{ocr.model}-{ocr.model_version}" if ocr.model_version else ocr.model
    parsed.title = title
    parsed.text = text
    parsed.page_count = ocr.page_count
    parsed.ocr_used = True
    parsed.ocr_confidence = round(ocr.mean_confidence, 4)
    parsed.parser_version = f"{PDF_PARSER_VERSION}+ocr-{model}"
    parsed.warnings = warnings
    if parsed.structure is not None:
        parsed.structure.pages = [
            PageStructure(
                page=page.page_no,
                text_chars=count_cjk(page.text),
                has_text_layer=True,
                needs_ocr=False,
                ocr_available=True,
                vision_status=None,
            )
            for page in ocr.pages
        ]
        ocr_blocks = [
            StructureBlock(
                block_id=f"{parsed.doc_id or 'doc'}@{parsed.version_id or 'version'}#ocr{order}",
                order=order,
                kind="paragraph",
                text=line,
                locator=SourceLocator(format="pdf", page=page.page_no),
            )
            for order, page in enumerate(ocr.pages, start=1)
            for line in page.text.splitlines()
            if line.strip()
        ]
        if ocr_blocks:
            parsed.structure.blocks = ocr_blocks
            parsed.structure.reading_order = [block.block_id for block in ocr_blocks]
        parsed.structure.coverage.update(
            {
                "ocr_pages": ocr.page_count,
                "pages_needing_ocr": 0,
                "ocr_confidence": round(ocr.mean_confidence, 4),
            }
        )
        parsed.structure.warnings.append("OCR 旁路文本；表格与原图位置需视觉复核")
    return parsed


def apply_ocr_sidecar(
    parsed: ParseOutput,
    *,
    doc_id: str,
    version_id: str,
    source_sha256: str,
    ocr_dir: Path,
    title_hint: str | None = None,
) -> ParseOutput:
    """文本层过短时尝试读取 SHA 匹配的 OCR 缓存；缺失或不匹配则保持原样。"""
    if parsed.format != "pdf" or count_cjk(parsed.text) >= OCR_MIN_CJK:
        return parsed

    path = ocr_output_path(ocr_dir, doc_id, version_id)
    if not path.exists():
        parsed.warnings.append("未找到 OCR 缓存；扫描件需先运行 scripts/ocr_scans.py")
        return parsed
    try:
        ocr = load_ocr_document(path)
    except (OSError, ValueError) as exc:
        parsed.warnings.append(f"OCR 缓存无法读取，已忽略：{exc}")
        return parsed
    if ocr.source_sha256 != source_sha256:
        parsed.warnings.append("OCR 缓存与当前 PDF 的 SHA-256 不一致，已忽略")
        return parsed
    return apply_ocr_document(parsed, ocr, title_hint=title_hint)


def parse_file(
    path: Path,
    source_format: str,
    *,
    title_hint: str | None = None,
    content_type: str | None = None,
    doc_id: str = "",
    version_id: str = "",
) -> ParseOutput:
    if source_format == "html":
        return parse_html_file(
            path,
            title_hint=title_hint,
            content_type=content_type,
            doc_id=doc_id,
            version_id=version_id,
        )
    if source_format == "pdf":
        return parse_pdf_file(
            path,
            title_hint=title_hint,
            doc_id=doc_id,
            version_id=version_id,
        )
    raise ValueError(f"不支持的格式：{source_format}")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _selected(doc_id: str, version_id: str, only: set[str] | None) -> bool:
    if not only:
        return True
    return doc_id in only or f"{doc_id}@{version_id}" in only


def parse_corpus(
    *,
    raw_dir: Path,
    processed_dir: Path,
    only: Iterable[str] | None = None,
    ocr_dir: Path | None = None,
    vision_preparser: Any | None = None,
    require_vision: bool = False,
) -> list[ParseResult]:
    only_set = set(only) if only else None
    ocr_root = ocr_dir if ocr_dir is not None else processed_dir / "ocr"
    results: list[ParseResult] = []
    for meta_path in sorted(raw_dir.glob("*/*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            results.append(
                ParseResult(
                    doc_id=meta_path.parent.parent.name,
                    version_id=meta_path.parent.name,
                    status="error",
                    error=f"读取 meta.json 失败：{exc}",
                )
            )
            continue

        doc_id = str(meta.get("doc_id") or meta_path.parent.parent.name)
        version_id = str(meta.get("version_id") or meta_path.parent.name)
        if not _selected(doc_id, version_id, only_set):
            continue

        version_dir = meta_path.parent
        source_name = meta.get("source_file")
        source_path = version_dir / str(source_name) if source_name else None
        if source_path is None or not source_path.exists():
            candidates = sorted(version_dir.glob("source.*"))
            source_path = candidates[0] if candidates else None
        if source_path is None or not source_path.exists():
            results.append(
                ParseResult(
                    doc_id=doc_id,
                    version_id=version_id,
                    status="error",
                    error="找不到原始快照文件",
                )
            )
            continue

        source_format = str(meta.get("format") or source_path.suffix.lstrip("."))
        output_path = processed_dir / "parsed" / doc_id / f"{version_id}.json"
        # 以原始文件字节为准重算哈希，并与 meta 交叉校验；避免 meta 陈旧
        # 或源文件被替换后仍沿用视觉/OCR 缓存。
        actual_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        declared_sha256 = str(meta.get("sha256") or "").strip().lower()
        if declared_sha256 and declared_sha256 != actual_sha256:
            message = (
                f"原始文件 SHA-256 与 meta 不一致（meta={declared_sha256[:12]}…，"
                f"实际={actual_sha256[:12]}…），拒绝使用可能陈旧的解析/视觉缓存"
            )
            results.append(
                ParseResult(
                    doc_id=doc_id,
                    version_id=version_id,
                    status="error",
                    error=message,
                    warnings=[message],
                    source_sha256=actual_sha256,
                )
            )
            continue
        source_sha256 = actual_sha256
        try:
            parsed = parse_file(
                source_path,
                source_format,
                title_hint=str(meta.get("title") or ""),
                content_type=meta.get("content_type"),
                doc_id=doc_id,
                version_id=version_id,
            )
        except Exception as exc:
            results.append(
                ParseResult(
                    doc_id=doc_id,
                    version_id=version_id,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        parsed = apply_ocr_sidecar(
            parsed,
            doc_id=doc_id,
            version_id=version_id,
            source_sha256=source_sha256,
            ocr_dir=ocr_root,
            title_hint=str(meta.get("title") or ""),
        )

        if require_vision:
            if vision_preparser is None:
                results.append(
                    ParseResult(
                        doc_id=doc_id,
                        version_id=version_id,
                        status="error",
                        error=(
                            "vision_first 未通过：未配置视觉预解析器。"
                            "禁止假报识图成功后继续正式入库。"
                        ),
                        warnings=["vision_first 未通过：未配置视觉预解析器"],
                    )
                )
                continue
            try:
                vision_result = vision_preparser.prepare(
                    source_path=source_path,
                    doc_id=doc_id,
                    version_id=version_id,
                    source_sha256=source_sha256,
                    source_format=source_format,
                    parsed=parsed,
                )
            except Exception as exc:
                results.append(
                    ParseResult(
                        doc_id=doc_id,
                        version_id=version_id,
                        status="error",
                        error=f"vision_first 未通过：{type(exc).__name__}: {exc}",
                        warnings=[f"vision_first 未通过：{type(exc).__name__}: {exc}"],
                    )
                )
                continue
            if not getattr(vision_result, "ok", False):
                results.append(
                    ParseResult(
                        doc_id=doc_id,
                        version_id=version_id,
                        status="error",
                        error=f"vision_first 未通过：{getattr(vision_result, 'error', '视觉服务失败')}",
                        warnings=["vision_first 未通过，未写入 parsed 结果"],
                    )
                )
                continue
            from campusguide.ingest.visual import apply_vision_result

            try:
                parsed = apply_vision_result(parsed, vision_result)
            except Exception as exc:
                results.append(
                    ParseResult(
                        doc_id=doc_id,
                        version_id=version_id,
                        status="error",
                        error=f"vision_first 未通过：应用视觉结果失败 {type(exc).__name__}: {exc}",
                        warnings=["vision_first 未通过，未写入 parsed 结果"],
                    )
                )
                continue
            publishable = (parsed.vision or {}).get("publishable")
            if publishable is not True:
                results.append(
                    ParseResult(
                        doc_id=doc_id,
                        version_id=version_id,
                        status="error",
                        error=(
                            "vision_first 未通过：视觉结果未明确 publishable=True"
                            "（关键冲突未解决或未完成区域复识）"
                        ),
                        warnings=["vision_first 未通过，未写入 parsed 结果"],
                    )
                )
                continue
            parsed.ingestion_mode = "vision_first"
        elif parsed.structure is not None:
            parsed.ingestion_mode = "native_only"

        document = ParsedDocument(
            doc_id=doc_id,
            version_id=version_id,
            title=parsed.title,
            text=parsed.text,
            format=parsed.format,
            source_sha256=source_sha256,
            source_url=meta.get("final_url") or meta.get("source_url"),
            local_path=meta.get("local_path"),
            fetched_at=meta.get("fetched_at"),
            parser_version=parsed.parser_version,
            warnings=parsed.warnings,
            tables=parsed.tables,
            page_count=parsed.page_count,
            cjk_count=count_cjk(parsed.text),
            char_count=len(parsed.text),
            ocr_used=parsed.ocr_used,
            ocr_confidence=parsed.ocr_confidence,
            structure=parsed.structure,
            vision=parsed.vision,
            ingestion_mode=parsed.ingestion_mode,
        )
        _atomic_write_json(output_path, document.model_dump(mode="json"))
        results.append(
            ParseResult(
                doc_id=doc_id,
                version_id=version_id,
                status="parsed",
                output_path=output_path,
                warnings=parsed.warnings,
                cjk_count=document.cjk_count,
                ocr_used=parsed.ocr_used,
                ocr_confidence=parsed.ocr_confidence,
                ingestion_mode=parsed.ingestion_mode,
                vision_pages=int((parsed.vision or {}).get("pages_processed", 0)),
                source_sha256=source_sha256,
            )
        )
    return results


def write_parse_report(
    path: Path,
    results: list[ParseResult],
    *,
    raw_dir: Path,
    processed_dir: Path,
) -> Path:
    """记录本次解析的成功来源与哈希，供 build 拒绝混入旧 parsed 文件。"""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "raw_dir": str(raw_dir),
        "processed_dir": str(processed_dir),
        "counts": {
            status: sum(1 for result in results if result.status == status)
            for status in ("parsed", "skipped", "error")
        },
        "results": [
            {
                "doc_id": result.doc_id,
                "version_id": result.version_id,
                "status": result.status,
                "error": result.error,
                "source_sha256": result.source_sha256,
                "ingestion_mode": result.ingestion_mode,
                "vision_pages": result.vision_pages,
                "cjk_count": result.cjk_count,
                "warnings": result.warnings,
            }
            for result in results
        ],
    }
    _atomic_write_json(path, payload)
    return path


def load_parsed_document(path: Path) -> ParsedDocument:
    return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))
