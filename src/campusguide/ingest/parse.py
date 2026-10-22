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
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup, NavigableString
from pydantic import BaseModel

from campusguide.domain.text import clean_whitespace, count_cjk, normalize_text
from campusguide.ingest.ocr import (
    OCR_MIN_CJK,
    OcrDocument,
    load_ocr_document,
    ocr_output_path,
)

HTML_PARSER_VERSION = "campusguide-html-0.1.0"
PDF_PARSER_VERSION = "campusguide-pdf-0.1.0"
PARSE_SCHEMA_VERSION = "1.0"

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
    r"^\s*(?:[-—–]?\s*\d{1,4}\s*[-—–]?|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+)\s*$"
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


@dataclass
class ParseOutput:
    title: str
    text: str
    format: Literal["html", "pdf"]
    parser_version: str
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    page_count: int | None = None
    ocr_used: bool = False
    ocr_confidence: float | None = None


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


def _tables_to_text(content: BeautifulSoup) -> tuple[list[str], list[str]]:
    table_blocks: list[str] = []
    warnings: list[str] = []
    tables = [table for table in content.find_all("table") if table.find_parent("table") is None]
    for table in tables:
        # 部分老站点把整篇正文放进单个表格单元格；这种布局表格不能压成
        # “单元格 | 单元格”的一行，否则段落和“第 X 条”边界都会丢失。
        table_text = table.get_text("", strip=True)
        if len(table.find_all("p")) >= 3 or re.search(
            r"第[一二三四五六七八九十百千零〇两0-9]{1,12}条", table_text
        ):
            table.unwrap()
            warnings.append("检测到布局表格，已按段落展开")
            continue

        rows: list[str] = []
        for row in table.find_all("tr"):
            cells = [
                clean_whitespace(cell.get_text(" ", strip=True))
                for cell in row.find_all(["th", "td"])
            ]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            rendered = "\n".join(rows)
            table_blocks.append(rendered)
            # 用块级节点占位，保证表格文本留在原位置；直接替换为裸文本
            # 会被 _extract_html_text 提前到全文开头，挂到错误的条款上。
            replacement = table.new_tag("p")
            replacement.string = f"【表格】\n{rendered}"
            table.replace_with(replacement)
        else:
            table.decompose()
    if len(table_blocks) > 3:
        warnings.append(f"检测到 {len(table_blocks)} 个表格，已转为文本，请人工抽查")
    return table_blocks, warnings


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

    学校 CMS 常把年份、文号、金额拆成独立 span；逐 span 取文本会产生
    “华南农办〔 / 2015 / 〕 / 150 / 号”这类碎片。这里以 p/div/li 等
    块级元素为单位，用空分隔符拼接行内片段，再补回“第一条/第一章”后的空格。
    """
    br_sentinel = "\ue000"
    for br in content.find_all("br"):
        br.replace_with(br_sentinel)

    blocks = [element for element in content.find_all(BLOCK_TAGS) if not element.find(BLOCK_TAGS)]
    if content.name in BLOCK_TAGS and not content.find(BLOCK_TAGS):
        blocks.insert(0, content)

    texts: list[str] = []
    seen: set[int] = set()
    for child in content.children:
        if isinstance(child, NavigableString) and child.strip():
            texts.append(str(child).strip())
    for block in blocks:
        if id(block) in seen:
            continue
        seen.add(id(block))
        text = block.get_text("", strip=True).replace(br_sentinel, "\n")
        if text:
            texts.append(text)

    if not texts:
        return content.get_text("\n")

    normalized: list[str] = []
    for text in texts:
        text = ARTICLE_HEADING_RE.sub(r"\1 ", text, count=1)
        text = CHAPTER_HEADING_RE.sub(r"\1 ", text, count=1)
        normalized.append(text)
    return "\n".join(normalized)


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
    tables, table_warnings = _tables_to_text(content)
    warnings.extend(table_warnings)

    text = _drop_noise_lines(
        _extract_html_text(content),
        page_number_re=HTML_PAGE_NUMBER_RE,
    )
    if count_cjk(text) < 200:
        warnings.append("正文 CJK 字符少于 200，可能存在解析不全")
    if not text:
        warnings.append("解析结果为空")

    return ParseOutput(
        title=title or title_hint or "",
        text=text,
        format="html",
        parser_version=HTML_PARSER_VERSION,
        warnings=warnings,
        tables=tables,
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
        if line and len(line) <= 80 and not PAGE_NUMBER_RE.match(line)
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
            if index in edge_positions and line in repeated and not line.startswith("第"):
                continue
            cleaned.append(line)
        cleaned_pages.append(cleaned if cleaned else page)

    if repeated:
        warnings.append(f"PDF 检测到 {len(repeated)} 条重复页眉/页脚文本，已在页边缘清理")
    return cleaned_pages, warnings


def _pdf_tables(document: object, page_texts: list[str]) -> tuple[list[str], list[str]]:
    tables: list[str] = []
    warnings: list[str] = []
    for index, page in enumerate(document, start=1):  # type: ignore[arg-type]
        try:
            found = page.find_tables()
        except (AttributeError, RuntimeError):
            continue
        for table in getattr(found, "tables", []):
            try:
                rows = table.extract()
            except (AttributeError, RuntimeError):
                continue
            rendered_rows = []
            for row in rows:
                cells = [clean_whitespace(str(cell)) for cell in row if cell not in (None, "")]
                if cells:
                    rendered_rows.append(" | ".join(cells))
            if not rendered_rows:
                continue
            rendered = "\n".join(rendered_rows)
            page_normalized = normalize_text(page_texts[index - 1])
            if normalize_text(rendered) and normalize_text(rendered) in page_normalized:
                continue
            tables.append(rendered)
    if tables:
        warnings.append(f"PDF 检测到 {len(tables)} 个表格，已单独提取并在文末重建")
    return tables, warnings


def parse_pdf_file(path: Path, *, title_hint: str | None = None) -> ParseOutput:
    import pymupdf

    warnings: list[str] = []
    document = pymupdf.open(path)
    try:
        metadata_title = (document.metadata or {}).get("title") or ""
        pages: list[list[str]] = []
        page_texts: list[str] = []
        empty_pages = 0
        for page in document:
            text = page.get_text("text", sort=True) or ""
            page_texts.append(text)
            if count_cjk(text) < 10:
                empty_pages += 1
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            pages.append(lines)

        cleaned_pages, header_warnings = _page_number_or_repeated(pages)
        warnings.extend(header_warnings)
        text = "\n\n".join("\n".join(lines) for lines in cleaned_pages if lines)
        text = clean_whitespace(text)

        tables, table_warnings = _pdf_tables(document, page_texts)
        warnings.extend(table_warnings)

        if empty_pages:
            warnings.append(
                f"{empty_pages}/{len(page_texts)} 页几乎没有文本层，可能为扫描件，需要 OCR"
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

        body = text
        if tables:
            body = f"{text}\n\n【表格】\n" + "\n\n".join(tables)

        return ParseOutput(
            title=title or title_hint or "",
            text=body,
            format="pdf",
            parser_version=f"{PDF_PARSER_VERSION}+pymupdf-{pymupdf.__version__}",
            warnings=warnings,
            tables=tables,
            page_count=len(page_texts),
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
) -> ParseOutput:
    if source_format == "html":
        return parse_html_file(path, title_hint=title_hint, content_type=content_type)
    if source_format == "pdf":
        return parse_pdf_file(path, title_hint=title_hint)
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
        source_sha256 = str(
            meta.get("sha256") or hashlib.sha256(source_path.read_bytes()).hexdigest()
        )
        try:
            parsed = parse_file(
                source_path,
                source_format,
                title_hint=str(meta.get("title") or ""),
                content_type=meta.get("content_type"),
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
            )
        )
    return results


def load_parsed_document(path: Path) -> ParsedDocument:
    return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))
