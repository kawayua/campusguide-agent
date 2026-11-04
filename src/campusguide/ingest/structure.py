"""原生结构层：保留页面/DOM 位置、块顺序、表格网格与资源清单。

结构对象是 ``ParsedDocument`` 的可选扩展；旧解析缓存没有该字段时按
``None`` 加载，继续走原有切分逻辑。所有 ID 均由原始结构确定，不从
扁平文本猜测。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any, Literal

from bs4 import Comment, NavigableString, Tag
from pydantic import BaseModel, Field, field_validator

STRUCTURE_SCHEMA_VERSION = "2.1"
HTML_STRUCTURE_VERSION = "html-structure-0.2.0"
PDF_STRUCTURE_VERSION = "pdf-structure-0.2.0"

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
BR_SENTINEL = "\ue000"
ATTACHMENT_SUFFIXES = (
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".rar",
    ".7z",
)
DECORATIVE_RE = re.compile(
    r"(avatar|head[-_]?shot|portrait|logo|icon|badge|qrcode|qr[-_]?code|banner|"
    r"decoration|spacer|blank|dot|arrow|微信|二维码|头像|图标|装饰|校徽)",
    re.IGNORECASE,
)


class SourceLocator(BaseModel):
    format: Literal["html", "pdf"]
    page: int | None = None
    bbox: list[float] | None = None
    dom_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None

    @field_validator("bbox")
    @classmethod
    def _bbox_shape(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and len(value) != 4:
            raise ValueError("bbox 必须是 [x0, y0, x1, y1]")
        return value


class AssetResource(BaseModel):
    asset_id: str
    kind: Literal["image", "table_image", "attachment", "unknown"]
    src: str
    alt: str | None = None
    locator: SourceLocator
    factual: bool = True
    recognized: bool = False
    note: str | None = None


class TableCell(BaseModel):
    cell_id: str
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    origin_row: int
    origin_col: int
    covered: bool = False
    text: str = ""
    # 原生/视觉/最终采用文本分开保留，冲突不能被静默覆盖。
    native_text: str | None = None
    vision_text: str | None = None
    text_source: Literal["native", "clip_text", "vision", "vision+native"] | None = None
    needs_review: bool = False
    is_header: bool = False
    bbox: list[float] | None = None
    dom_path: str | None = None


class HeaderColumnBinding(BaseModel):
    """单个 leaf 列的表头来源；未继承列保留空标签而不是猜测列名。"""

    target_col: int
    status: Literal[
        "bound",
        "shared",
        "gap",
        "unresolved",
        "ambiguous",
        "offset",
    ] = "unresolved"
    method: Literal[
        "local_header",
        "physical_interval",
        "trailing_empty_recovery",
        "none",
    ] = "none"
    source_table_id: str | None = None
    source_via_table_id: str | None = None
    source_page: int | None = None
    source_col: int | None = None
    source_header_cell_ids: list[str] = Field(default_factory=list)
    source_header_row_indices: list[int] = Field(default_factory=list)
    source_bbox: list[float] | None = None
    target_bbox: list[float] | None = None
    header_text: str = ""
    header_path: list[str] = Field(default_factory=list)
    covered: bool = False
    shared_scope: bool = False
    # needs_review 来源的原始证据仅供审计/复核，不参与列语义；
    # 这类列的 method 固定为 none，标准 source_*/header_* 保持未绑定空值。
    review_source_table_id: str | None = None
    review_source_page: int | None = None
    review_source_col: int | None = None
    review_source_header_cell_ids: list[str] = Field(default_factory=list)
    review_source_header_row_indices: list[int] = Field(default_factory=list)
    review_source_bbox: list[float] | None = None
    review_source_text: str = ""
    reason: str = ""


class TableRow(BaseModel):
    row_id: str
    row_index: int
    is_header: bool = False
    cells: list[TableCell] = Field(default_factory=list)
    text: str = ""
    locator: SourceLocator


class TableStructure(BaseModel):
    table_id: str
    block_id: str
    table_index: int
    page: int | None = None
    bbox: list[float] | None = None
    rows: list[TableRow] = Field(default_factory=list)
    header_row_indices: list[int] = Field(default_factory=list)
    column_count: int = 0
    row_count: int = 0
    column_headers: list[str] = Field(default_factory=list)
    header_paths: list[list[str]] = Field(default_factory=list)
    # 每个 leaf 列的物理表头来源；旧快照缺省为空，HTML 表不生成绑定。
    column_bindings: list[HeaderColumnBinding] = Field(default_factory=list)
    caption: str | None = None
    kind: Literal["data", "layout", "matrix"] = "data"
    source: Literal["html_dom", "pymupdf", "vision", "vision+native"] = "html_dom"
    locator: SourceLocator
    warnings: list[str] = Field(default_factory=list)
    vision: dict[str, Any] | None = None
    # 跨页续表：本表没有自己的表头行时，记录被继承表头的来源关系。
    continuation_of: str | None = None
    header_source_table_id: str | None = None
    header_source_page: int | None = None

    @property
    def header_rows(self) -> list[TableRow]:
        return [row for row in self.rows if row.is_header]

    def data_rows(self) -> list[TableRow]:
        return [row for row in self.rows if not row.is_header]


def _grid_cell(
    grid: list[list[TableCell | None]], row: int, col: int
) -> TableCell | None:
    if not 0 <= row < len(grid):
        return None
    cells = grid[row]
    if not 0 <= col < len(cells):
        return None
    return cells[col]


def row_leaf_slots(
    cells: list[TableCell],
    *,
    row_index: int,
    column_count: int,
    resolve_origin: Callable[[int, int], TableCell | None],
) -> list[TableCell | None]:
    """每个 leaf col 恰好一个位置：origin 自身、纵向继承的 origin 或空位。

    covered 只在 origin 来自更早的非表头行时把真实标签放回 origin_col；
    同一行 colspan 及其他 covered 位置保持空位，后续列不会左移。
    """
    slots: list[TableCell | None] = [None] * max(0, column_count)
    inherited: set[tuple[int, int]] = set()
    for cell in cells:
        if not 0 <= cell.col < column_count:
            continue
        if not cell.covered:
            slots[cell.col] = cell
            continue
        origin_key = (cell.origin_row, cell.origin_col)
        origin = resolve_origin(cell.origin_row, cell.origin_col)
        if (
            cell.origin_row < row_index
            and cell.col == cell.origin_col
            and origin is not None
            and not origin.covered
            and not origin.is_header
            and origin.text.strip()
            and origin_key not in inherited
        ):
            slots[cell.col] = origin
            inherited.add(origin_key)
    return slots


def join_row_texts(texts: list[str]) -> str:
    """空行仍为空串；有内容时用分隔符保留每个 leaf col 的空占位。"""
    if not any(text.strip() for text in texts):
        return ""
    return " | ".join(texts)


class StructureBlock(BaseModel):
    block_id: str
    order: int
    kind: Literal[
        "heading",
        "paragraph",
        "list_item",
        "table",
        "image",
        "attachment",
        "raw_text",
    ]
    text: str = ""
    level: int | None = None
    locator: SourceLocator
    table_id: str | None = None
    element_id: str | None = None


class PageStructure(BaseModel):
    page: int
    width: float | None = None
    height: float | None = None
    text_chars: int = 0
    has_text_layer: bool = True
    needs_ocr: bool = False
    ocr_available: bool = False
    rendered: bool = False
    render_sha256: str | None = None
    vision_status: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ParsedStructure(BaseModel):
    schema_version: str = STRUCTURE_SCHEMA_VERSION
    extractor_version: str = ""
    format: Literal["html", "pdf"]
    blocks: list[StructureBlock] = Field(default_factory=list)
    tables: list[TableStructure] = Field(default_factory=list)
    pages: list[PageStructure] = Field(default_factory=list)
    assets: list[AssetResource] = Field(default_factory=list)
    reading_order: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    vision: dict[str, Any] | None = None

    def table_by_id(self, table_id: str) -> TableStructure | None:
        return next((table for table in self.tables if table.table_id == table_id), None)


def _clean(text: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", text).strip()


def _cell_text(cell: Tag) -> str:
    for br in cell.find_all("br"):
        br.replace_with(BR_SENTINEL)
    text = cell.get_text("", strip=False).replace(BR_SENTINEL, "\n")
    lines = [_clean(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def dom_path(element: Tag, root: Tag | None = None) -> str:
    parts: list[str] = []
    current: Tag | None = element
    while current is not None and current.name not in {"[document]"}:
        parent = current.parent if isinstance(current.parent, Tag) else None
        if parent is not None:
            siblings = [child for child in parent.children if isinstance(child, Tag)]
            # BeautifulSoup 的 Tag 相等按内容比较；相同文本的兄弟节点会得到
            # 同一个 nth-child。这里必须按节点身份定位。
            index = next(
                (position for position, sibling in enumerate(siblings, start=1) if sibling is current),
                1,
            )
            parts.append(f"{current.name}:nth-child({index})")
        else:
            parts.append(current.name)
        if root is not None and current is root:
            break
        current = parent
    return "/".join(reversed(parts))


def _table_rows(table: Tag) -> list[Tag]:
    return [
        row
        for row in table.find_all("tr")
        if row.find_parent("table") is table
    ]


def _row_cells(row: Tag) -> list[Tag]:
    return [cell for cell in row.find_all(["th", "td"], recursive=False)]


def _int_attr(cell: Tag, name: str) -> int:
    try:
        value = int(str(cell.get(name, "1")).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(value, 100))


def extract_html_table(
    table: Tag,
    *,
    doc_id: str,
    version_id: str,
    table_index: int,
    block_id: str,
    root: Tag | None = None,
) -> tuple[TableStructure, str]:
    """把 HTML 表格展开为完整网格，保留 rowspan/colspan 与空单元格位。"""
    table_id = f"{doc_id}@{version_id}#table{table_index}"
    rows = _table_rows(table)
    grid: list[list[TableCell | None]] = []
    origin_cells: list[tuple[int, int, TableCell]] = []
    dom_prefix = dom_path(table, root)
    warnings: list[str] = []
    for row_index, row in enumerate(rows):
        while len(grid) <= row_index:
            grid.append([])
        cells = _row_cells(row)
        col = 0
        for cell_index, cell in enumerate(cells):
            while col < len(grid[row_index]) and grid[row_index][col] is not None:
                col += 1
            rowspan = _int_attr(cell, "rowspan")
            colspan = _int_attr(cell, "colspan")
            text = _cell_text(cell)
            is_header = cell.name == "th"
            cell_id = f"{table_id}#r{row_index}c{col}"
            origin = TableCell(
                cell_id=cell_id,
                row=row_index,
                col=col,
                rowspan=rowspan,
                colspan=colspan,
                origin_row=row_index,
                origin_col=col,
                text=text,
                is_header=is_header,
                dom_path=f"{dom_prefix}/tr[{row_index + 1}]/td[{cell_index + 1}]",
            )
            origin_cells.append((row_index, col, origin))
            for row_offset in range(rowspan):
                target_row = row_index + row_offset
                while len(grid) <= target_row:
                    grid.append([])
                for col_offset in range(colspan):
                    target_col = col + col_offset
                    while len(grid[target_row]) <= target_col:
                        grid[target_row].append(None)
                    if grid[target_row][target_col] is not None:
                        warnings.append(
                            f"表格单元格重叠：r{target_row}c{target_col}，以先出现者为准"
                        )
                        continue
                    origin_slot = row_offset == 0 and col_offset == 0
                    grid[target_row][target_col] = TableCell(
                        cell_id=f"{table_id}#r{target_row}c{target_col}",
                        row=target_row,
                        col=target_col,
                        rowspan=rowspan,
                        colspan=colspan,
                        origin_row=row_index,
                        origin_col=col,
                        covered=not origin_slot,
                        text=text if origin_slot else "",
                        text_source="native" if origin_slot and text else None,
                        is_header=is_header,
                        dom_path=origin.dom_path,
                    )
            col += colspan

    column_count = max((len(row) for row in grid), default=0)
    for row in grid:
        while len(row) < column_count:
            row.append(None)

    header_indices: list[int] = []
    for index, row in enumerate(rows):
        has_header = any(cell.name == "th" for cell in _row_cells(row))
        if has_header:
            header_indices.append(index)
        elif index == 0 and not header_indices:
            header_indices.append(0)
    header_set = set(header_indices)
    if not header_indices and grid:
        header_set = {0}
        header_indices = [0]

    table_rows: list[TableRow] = []
    for row_index, _row_tag in enumerate(rows):
        filled = grid[row_index]
        cells = [
            cell
            if cell is not None
            else TableCell(
                cell_id=f"{table_id}#r{row_index}c{col}",
                row=row_index,
                col=col,
                origin_row=row_index,
                origin_col=col,
                text="",
                is_header=row_index in header_set,
            )
            for col, cell in enumerate(filled)
        ]
        slots = row_leaf_slots(
            cells,
            row_index=row_index,
            column_count=column_count,
            resolve_origin=lambda origin_row, origin_col: _grid_cell(
                grid, origin_row, origin_col
            ),
        )
        row_text = join_row_texts(
            [cell.text if cell is not None else "" for cell in slots]
        )
        table_rows.append(
            TableRow(
                row_id=f"{table_id}#row{row_index}",
                row_index=row_index,
                is_header=row_index in header_set,
                cells=cells,
                text=row_text,
                locator=SourceLocator(
                    format="html",
                    dom_path=f"{dom_prefix}/tr[{row_index + 1}]",
                ),
            )
        )

    header_paths: list[list[str]] = []
    for col in range(column_count):
        path: list[str] = []
        for row_index in header_indices:
            if row_index >= len(table_rows):
                continue
            cell = table_rows[row_index].cells[col]
            if cell.covered:
                origin = table_rows[cell.origin_row].cells[cell.origin_col]
                cell = origin
            text = cell.text.strip()
            if text and (not path or path[-1] != text):
                path.append(text)
        header_paths.append(path)
    column_headers = [" > ".join(path) for path in header_paths]

    paragraph_count = len(table.find_all("p"))
    table_text = table.get_text("", strip=True)
    layout = column_count <= 1 and (
        paragraph_count >= 3
        or bool(re.search(r"第[一二三四五六七八九十百千零〇两0-9]{1,12}条", table_text))
    )
    if layout:
        warnings.append("单列表格且包含段落/条款，判定为布局表格")
    if any(cell.rowspan > 1 or cell.colspan > 1 for _r, _c, cell in origin_cells):
        warnings.append("表格含合并单元格，已按跨行/跨列展开并保留原始跨度")
    empty_slots = sum(
        1
        for row in table_rows
        for cell in row.cells
        if not cell.text and not cell.covered
    )
    if empty_slots:
        warnings.append(f"表格保留 {empty_slots} 个空单元格位")

    rendered_rows = [row.text for row in table_rows if row.text.strip()]
    rendered = "\n".join(rendered_rows)
    structure = TableStructure(
        table_id=table_id,
        block_id=block_id,
        table_index=table_index,
        rows=table_rows,
        header_row_indices=header_indices,
        column_count=column_count,
        row_count=len(table_rows),
        column_headers=column_headers,
        header_paths=header_paths,
        kind="layout" if layout else "data",
        source="html_dom",
        locator=SourceLocator(format="html", dom_path=dom_prefix),
        warnings=warnings,
    )
    return structure, rendered


def extract_html_assets(
    content: Tag,
    *,
    doc_id: str,
    version_id: str,
    root: Tag | None = None,
) -> tuple[list[AssetResource], list[str]]:
    """保留正文图片/附件定位；头像与装饰图不计入事实资源。"""
    assets: list[AssetResource] = []
    warnings: list[str] = []
    index = 0
    for image in content.find_all("img"):
        index += 1
        src = str(image.get("src") or image.get("data-src") or "").strip()
        if not src:
            warnings.append("正文图片缺少 src，未计入可定位资源")
            continue
        alt = _clean(str(image.get("alt") or "")) or None
        in_table = image.find_parent("table") is not None
        decorative = bool(DECORATIVE_RE.search(" ".join(filter(None, [src, alt or ""]))))
        width = image.get("width")
        height = image.get("height")
        try:
            if width and height and int(str(width)) <= 48 and int(str(height)) <= 48:
                decorative = True
        except ValueError:
            pass
        kind: Literal["image", "table_image", "attachment", "unknown"] = (
            "table_image" if in_table else "image"
        )
        asset = AssetResource(
            asset_id=f"{doc_id}@{version_id}#image{index}",
            kind=kind,
            src=src,
            alt=alt,
            locator=SourceLocator(format="html", dom_path=dom_path(image, root)),
            factual=not decorative,
            recognized=decorative,
            note="头像/装饰图，不计入事实遗漏" if decorative else "图片内容未做 OCR，需视觉识别",
        )
        assets.append(asset)
    for link in content.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        if not href:
            continue
        lower = href.lower().split("?", 1)[0]
        is_attachment = lower.endswith(ATTACHMENT_SUFFIXES)
        text = _clean(link.get_text(" ", strip=True))
        if not is_attachment and "附件" not in text:
            continue
        index += 1
        assets.append(
            AssetResource(
                asset_id=f"{doc_id}@{version_id}#attachment{index}",
                kind="attachment",
                src=href,
                alt=text or None,
                locator=SourceLocator(format="html", dom_path=dom_path(link, root)),
                factual=True,
                recognized=False,
                note="附件未下载/未解析，保留可追踪补采入口",
            )
        )
    if any(asset.kind == "attachment" for asset in assets):
        warnings.append("正文含附件链接；附件内容未纳入索引，需按清单补采")
    return assets, warnings


def _block_kind(element: Tag) -> Literal["heading", "paragraph", "list_item", "table", "raw_text"]:
    if element.get("data-table-id"):
        return "table"
    if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return "heading"
    if element.name == "li":
        return "list_item"
    return "paragraph"


def iter_html_blocks(content: Tag) -> list[tuple[Tag | NavigableString, str, str]]:
    """按真实 DOM 顺序返回正文块，块间直接文本不得被整体提前。

    只把“没有块级后代”的块元素当作原子块；含块级后代的容器递归遍历，
    因此 ``前文<p>中间</p>后文`` 会按可见阅读顺序输出。
    """
    for br in content.find_all("br"):
        br.replace_with(BR_SENTINEL)

    items: list[tuple[Tag | NavigableString, str, str]] = []

    def emit_text(node: NavigableString) -> None:
        if isinstance(node, Comment):
            return
        text = str(node).strip().replace(BR_SENTINEL, "\n")
        if text:
            items.append((node, text, "raw_text"))

    def visit(node: Tag | NavigableString) -> None:
        if isinstance(node, NavigableString):
            emit_text(node)
            return
        if not isinstance(node, Tag):
            return
        if node.name in BLOCK_TAGS and not node.find(BLOCK_TAGS):
            text = node.get_text("", strip=True).replace(BR_SENTINEL, "\n")
            if text:
                text = ARTICLE_HEADING_RE.sub(r"\1 ", text, count=1)
                text = CHAPTER_HEADING_RE.sub(r"\1 ", text, count=1)
                items.append((node, text, _block_kind(node)))
            return
        for child in node.children:
            visit(child)

    visit(content)
    return items


def build_html_structure(
    content: Tag,
    *,
    doc_id: str,
    version_id: str,
    text: str,
    tables: list[TableStructure],
    assets: list[AssetResource],
    warnings: list[str],
    parser_version: str,
) -> ParsedStructure:
    table_by_element: dict[int, TableStructure] = {}
    for table in tables:
        marker = table.locator.dom_path
        for element in content.find_all(attrs={"data-table-id": table.table_id}):
            table_by_element[id(element)] = table
        if marker is None:
            continue
    blocks: list[StructureBlock] = []
    cursor = 0
    for order, (element, block_text, kind) in enumerate(iter_html_blocks(content)):
        block_id = f"{doc_id}@{version_id}#block{order}"
        char_start = text.find(block_text, cursor)
        if char_start < 0:
            char_start = text.find(block_text)
        char_end = char_start + len(block_text) if char_start >= 0 else None
        if char_start >= 0:
            cursor = char_end or cursor
        table = None
        if isinstance(element, Tag):
            table = table_by_element.get(id(element))
            if table is None and element.get("data-table-id"):
                table = next(
                    (item for item in tables if item.table_id == element.get("data-table-id")),
                    None,
                )
        if table is not None:
            block = StructureBlock(
                block_id=block_id,
                order=order,
                kind="table",
                text=block_text,
                locator=SourceLocator(
                    format="html",
                    dom_path=dom_path(element) if isinstance(element, Tag) else None,
                    char_start=char_start if char_start >= 0 else None,
                    char_end=char_end,
                ),
                table_id=table.table_id,
                element_id=str(element.get("id")) if isinstance(element, Tag) and element.get("id") else None,
            )
            # 表格块 ID 以 origin 结构为准，便于父/子关系稳定。
            table.block_id = block_id
            for row in table.rows:
                row_id = row.row_id
                row.locator.char_start = None
                row.locator.char_end = None
                row.row_id = row_id
        else:
            level = None
            if isinstance(element, Tag) and element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                level = int(element.name[1])
            block = StructureBlock(
                block_id=block_id,
                order=order,
                kind=kind,  # type: ignore[arg-type]
                text=block_text,
                level=level,
                locator=SourceLocator(
                    format="html",
                    dom_path=dom_path(element) if isinstance(element, Tag) else None,
                    char_start=char_start if char_start >= 0 else None,
                    char_end=char_end,
                ),
                element_id=str(element.get("id")) if isinstance(element, Tag) and element.get("id") else None,
            )
        blocks.append(block)

    if text and not any(block.locator.char_start is not None for block in blocks):
        warnings.append("结构块未能映射回正文位置，引用定位回退到块文本")
    return ParsedStructure(
        extractor_version=HTML_STRUCTURE_VERSION,
        format="html",
        blocks=blocks,
        tables=tables,
        assets=assets,
        reading_order=[block.block_id for block in blocks],
        warnings=warnings,
        coverage={
            "blocks": len(blocks),
            "tables": len(tables),
            "assets": len(assets),
            "char_coverage": sum(
                1 for block in blocks if block.locator.char_start is not None
            ),
        },
        vision=None,
    )


def _finite_rect(
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    if not all(math.isfinite(value) for value in rect):
        return None
    return rect


def _rect_values(value: Any) -> tuple[float, float, float, float] | None:
    """公开 API 的单元格坐标可能是 4 元 tuple 或 Rect；统一为浮点四元组。"""
    if value is None:
        return None
    if hasattr(value, "x0") and hasattr(value, "y1"):
        try:
            return _finite_rect(
                (float(value.x0), float(value.y0), float(value.x1), float(value.y1))
            )
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return _finite_rect(
                (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
            )
        except (TypeError, ValueError):
            return None
    bbox = getattr(value, "bbox", None)
    if bbox is not None and bbox is not value:
        return _rect_values(bbox)
    return None


def _bbox_values(value: Any) -> list[float] | None:
    rect = _rect_values(value)
    return list(rect) if rect is not None else None


def _cell_bbox(cell: TableCell | None) -> list[float] | None:
    if cell is None:
        return None
    return _bbox_values(cell.bbox)


def _table_cell_bbox(table: Any, row: int, col: int) -> list[float] | None:
    try:
        cells = table.rows[row].cells
    except (AttributeError, IndexError, TypeError):
        return None
    if col >= len(cells):
        return None
    rect = _rect_values(cells[col])
    return list(rect) if rect is not None else None


def _cluster_positions(values: list[float], tolerance: float = 1.0) -> list[float]:
    ordered = sorted(float(value) for value in values)
    clusters: list[list[float]] = []
    for value in ordered:
        if clusters and abs(value - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _number_tokens(text: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:\.\d+)?", text))


def _pdf_clip_text(source_page: Any, bbox: list[float]) -> str | None:
    """按原页坐标重新读取单元格文本；坐标裁剪是校验原生 extract 的依据。"""
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        raw = source_page.get_text("text", clip=pymupdf.Rect(bbox), sort=True)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    lines = [_clean(line) for line in str(raw or "").splitlines()]
    return "\n".join(line for line in lines if line) or None


def _pdf_header_labels(header: Any, extracted: list[Any]) -> list[str]:
    """真实 header.names 优先；缺省时才把首个提取行当作候选表头标签。

    标签保持列位对齐，空名称不能用 None 字符串或删位来改变列索引。
    """
    names = [
        "" if name is None else str(name).strip()
        for name in (getattr(header, "names", None) or [])
    ]
    if not any(names) and extracted:
        names = [
            "" if value is None else str(value).strip() for value in extracted[0]
        ]
    return names


def _course_code_column(labels: list[str]) -> int | None:
    """只在表头明确是课程代码列且有课程名称/学分学时列证据时返回该列。"""
    compact = [re.sub(r"\s+", "", label or "") for label in labels]
    code_columns = [
        index
        for index, label in enumerate(compact)
        if "课程" in label and ("代码" in label or "编号" in label)
    ]
    if not code_columns:
        return None
    has_name = any("课程" in label and "名称" in label for label in compact)
    has_measure = any("学分" in label or "学时" in label for label in compact)
    if not (has_name and has_measure):
        return None
    return code_columns[0]


def _course_name_column(labels: list[str]) -> int | None:
    """只在表头明确是课程名称列且有课程代码/学分学时列证据时返回该列。"""
    compact = [re.sub(r"\s+", "", label or "") for label in labels]
    name_columns = [
        index
        for index, label in enumerate(compact)
        if "课程" in label and "名称" in label
    ]
    if not name_columns:
        return None
    has_code = any(
        "课程" in label and ("代码" in label or "编号" in label)
        for label in compact
    )
    has_measure = any("学分" in label or "学时" in label for label in compact)
    if not (has_code and has_measure):
        return None
    return name_columns[0]


def _course_identity_columns(
    labels: list[str],
) -> tuple[int | None, int | None]:
    """续表继承只认唯一的课程代码列与课程名称列。"""
    code_column = _course_code_column(labels)
    name_column = _course_name_column(labels)
    if code_column is None or name_column is None or code_column == name_column:
        return None, None
    compact = [re.sub(r"\s+", "", label or "") for label in labels]
    code_columns = [
        index
        for index, label in enumerate(compact)
        if "课程" in label and ("代码" in label or "编号" in label)
    ]
    name_columns = [
        index
        for index, label in enumerate(compact)
        if "课程" in label and "名称" in label
    ]
    if len(code_columns) != 1 or len(name_columns) != 1:
        return None, None
    return code_column, name_column


def _first_course_code_row(column: int, extracted: list[Any]) -> int | None:
    """首个独立 6/14 位课程代码所在行；学号/金额列没有课程表头证据。"""
    for index, row in enumerate(extracted):
        if column >= len(row):
            continue
        value = row[column]
        if value is None:
            continue
        if re.fullmatch(r"(?:\d{14}|\d{6})", re.sub(r"\s+", "", str(value))):
            return index
    return None


_COURSE_CODE_VALUE_RE = re.compile(r"(?:\d{14}|\d{6})")
_NON_COURSE_TABLE_RE = re.compile(
    r"学号|学籍号|学生证号|身份证|金额|费用|缴费|工资|卡号|账号"
)
_CURRENCY_RE = re.compile(r"[¥￥]|\d\s*(?:万?元)")
_SEASON_VALUE_RE = re.compile(
    r"^(?:春|秋|夏|冬|春季|秋季|夏季|冬季|"
    r"第?[一二三四五六七八九十0-9]{1,2}学期)$"
)
_NATURE_VALUE_RE = re.compile(
    r"^(?:必修|选修|限选|任选|学位课|非学位课|专业必修|专业选修|公共必修|公共选修)$"
)
_SUBHEADER_DATA_VALUE_RE = re.compile(
    r"第?[一二三四五六七八九十0-9]{1,2}(?:学期|学年)"
    r"|\d+(?:\.\d+)?(?:周|天|日|月|年|课时|学时|学分)(?:内|以内)?"
    r"|\d+[.、．)）]\S+"
    r"|(?:入学|开学|毕业|放假|报到|离校)[^\n]{0,12}"
    r"|\d{4}\s*[-—–]\s*\d{4}\s*学年?"
    r"|\d{1,4}[./年-]\d{1,2}(?:[./月-]\d{1,2})?"
)
_SUBHEADER_HEADER_RE = re.compile(
    r"考核|成绩|评分|分值|比例|权重|标准|形式|方式|类型|性质|模块|内容|要求|"
    r"环节|时间|地点|教师|学分|学时|备注|名称|编号|代码|类别|方向|层次|专业|"
    r"学院|序号|理论|实践|实验|实训|实习|平时|期末|期中|过程|结课|总评|"
    r"必修|选修|限选|任选"
)
_FRAGMENT_HEADER_GUARD = re.compile(
    r"课程|科目|代码|编号|名称|学分|学时|学期|性质|备注|类别|方向"
    r"|序号|模块|内容|教师|考核|单位|适用"
)


def _text_values(values: list[Any]) -> list[str]:
    return [_clean("" if value is None else str(value)) for value in values]


def _course_like_values(values: list[Any]) -> bool:
    """课程行形态只用于反证表头，不据此猜列名或字段。

    必须是 6/14 位代码、汉字课名与学期/性质的组合；金额、学号和培养环节
    行不具备这一组合，不会被当成课程数据。
    """
    texts = _text_values(list(values))
    if not any(texts):
        return False
    if any(_CURRENCY_RE.search(text) for text in texts):
        return False
    code_index = next(
        (
            index
            for index, text in enumerate(texts)
            if _COURSE_CODE_VALUE_RE.fullmatch(text)
        ),
        None,
    )
    if code_index is None:
        return False
    rest = [
        text for index, text in enumerate(texts) if index != code_index and text
    ]
    has_name = any(
        len(re.findall(r"[\u4e00-\u9fff]", text)) >= 2
        and not re.fullmatch(r"[\d.,%/\-—–]+", text)
        for text in rest
    )
    has_credit = any(
        re.fullmatch(r"(?:\d{1,2}(?:\.\d{1,2})?|\.\d{1,2})", text)
        and 0 < float(text) <= 30
        for text in rest
    )
    if not (has_name and has_credit):
        return False
    has_term = any(_SEASON_VALUE_RE.match(text) for text in rest)
    has_nature = any(_NATURE_VALUE_RE.match(text) for text in rest)
    return has_term or has_nature


def _course_name_value(text: str) -> bool:
    """课程名至少两个汉字，且不是学期/性质/金额等占位值。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact or _CURRENCY_RE.search(text or ""):
        return False
    if _COURSE_CODE_VALUE_RE.fullmatch(compact):
        return False
    if _SEASON_VALUE_RE.match(compact) or _NATURE_VALUE_RE.match(compact):
        return False
    if re.fullmatch(r"[\d.,%/\-—–]+", compact):
        return False
    return len(re.findall(r"[\u4e00-\u9fff]", compact)) >= 2


def _row_value_at(row: TableRow, col: int) -> str:
    if not 0 <= col < len(row.cells):
        return ""
    cell = row.cells[col]
    if cell.covered:
        return ""
    return _clean(cell.text)


def _course_identity_row_compatible(
    table: TableStructure, code_column: int, name_column: int
) -> bool:
    """实际数据行必须在代码/名称列位出现真实课程身份，而非任意数字。"""
    for row in table.rows:
        if row.is_header:
            continue
        code = re.sub(r"\s+", "", _row_value_at(row, code_column))
        if not _COURSE_CODE_VALUE_RE.fullmatch(code):
            continue
        if _course_name_value(_row_value_at(row, name_column)):
            return True
    return False


def _first_course_data_row(extracted: list[Any]) -> int | None:
    for index, row in enumerate(extracted):
        if _course_like_values(list(row or [])):
            return index
    return None


def _non_course_labels(labels: list[str], extracted: list[Any]) -> bool:
    """学号/金额表的表头或首行出现身份、费用词时不按课程数据消解表头。"""
    probes = [label for label in labels if label]
    if extracted:
        probes.extend(
            "" if value is None else str(value) for value in extracted[0]
        )
    return any(
        _NON_COURSE_TABLE_RE.search(re.sub(r"\s+", "", probe))
        for probe in probes
        if probe
    )


def _first_row_fragment(extracted: list[Any], boundary: int | None) -> bool:
    """首门课程前一行只有课程名/方向碎片时，它是跨页残留而不是表头。"""
    if boundary != 1 or len(extracted) < 2:
        return False
    texts = [text for text in _text_values(list(extracted[0])) if text]
    if not texts:
        return False
    if any(_FRAGMENT_HEADER_GUARD.search(text) for text in texts):
        return False
    if any(
        _COURSE_CODE_VALUE_RE.fullmatch(re.sub(r"\s+", "", text))
        for text in texts
    ):
        return False
    if not any(re.search(r"[\u4e00-\u9fff]", text) for text in texts):
        return False
    return _course_like_values(list(extracted[1]))


def _raw_span_width(raw_row: list[Any], col: int) -> int:
    """原始行里该 origin 单元格向右覆盖的 leaf 列数；None 视为被覆盖。"""
    if not 0 <= col < len(raw_row) or _rect_values(raw_row[col]) is None:
        return 0
    width = 1
    while col + width < len(raw_row) and raw_row[col + width] is None:
        width += 1
    return width


def _subheader_label_evidence(text: str) -> bool:
    """子表头标签必须不是相对时间/编号阶段/金额等数据形态，且有表头语义。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False
    if re.fullmatch(r"[\d.,%/\-—–]+", compact):
        return False
    if _SEASON_VALUE_RE.match(compact):
        return False
    if _SUBHEADER_DATA_VALUE_RE.fullmatch(compact):
        return False
    if _CURRENCY_RE.search(text or ""):
        return False
    return bool(_SUBHEADER_HEADER_RE.search(compact))


def _pdf_subheader_evidence(
    raw_rows: list[list[Any]], extracted: list[Any]
) -> bool:
    """跨列分组表头下必须有独立子表头 origin 单元格，才把次行并入表头。

    只依据列位/单元格结构判断；不会从数据单元格文本生成列名，也不会把
    单格里的“全日制/非全日制”等文本拆成不存在的来源单元格。次行文本若
    是“第三学期/第四学期”或编号阶段等真实数据，即使字符串不同也不构成
    子表头语义证据。
    """
    if len(raw_rows) < 2:
        return False
    next_row = raw_rows[1]
    row_values = extracted[1] if len(extracted) > 1 else []
    for col in range(len(raw_rows[0])):
        span = _raw_span_width(raw_rows[0], col)
        if span < 2:
            continue
        labels: list[str] = []
        semantic = True
        for offset in range(span):
            target = col + offset
            if target >= len(next_row) or _rect_values(next_row[target]) is None:
                continue
            text = (
                "" if target >= len(row_values) or row_values[target] is None
                else str(row_values[target]).strip()
            )
            if not text:
                continue
            if not _subheader_label_evidence(text):
                semantic = False
                break
            labels.append(text)
        if semantic and len(labels) >= 2 and len(set(labels)) >= 2:
            return True
    return False


def _pdf_header_indices(
    table: Any,
    raw_rows: list[list[Any]],
    extracted: list[Any],
    row_tops: list[float],
) -> tuple[list[int], list[str], int | None]:
    """恢复多层表头：优先使用原生 header 边界，再保守识别跨列分组表头。

    原生 header.names/header.bbox 可能本身就是数据；没有明确课程表头标签
    时，课程行形态只用于阻止数据被吞成表头，不据此生成列名。
    """
    declared = int(getattr(table, "header_rows", 0) or 0)
    try:
        header = table.header
    except (AttributeError, RuntimeError, TypeError, ValueError):
        header = None
    labels = _pdf_header_labels(header, extracted)
    external = bool(getattr(header, "external", False)) if header is not None else False
    # 外置 header 的 bbox 属于上一页/上一表，不能用它圈定本表的数据行。
    header_bbox = (
        _rect_values(getattr(header, "bbox", None))
        if header is not None and not external
        else None
    )
    indices: list[int] = []
    if declared > 0:
        indices = list(range(min(declared, len(raw_rows))))
    elif header_bbox is not None:
        # header.bbox 覆盖的整行都是表头；被跨行表头覆盖的下一行也由几何位置纳入。
        indices = [
            index
            for index, top in enumerate(row_tops)
            if top < float(header_bbox[3]) - 0.5
        ]
    # 外置 header 不在本表行内，不能把首行数据猜成本地表头。
    if not indices and raw_rows and not external:
        indices = [0]
    # 有明确课程表头证据时，第一门课所在行是硬边界：declared/header.bbox
    # 覆盖过来的多个课程代码行不能被吞成表头。
    code_column = _course_code_column(labels)
    boundary = (
        _first_course_code_row(code_column, extracted)
        if code_column is not None
        else None
    )
    if boundary is not None:
        indices = [index for index in indices if index < boundary]
    # 没有可靠课程表头标签时，原生 header.names/bbox 可能整块都是数据；
    # 课程行形态只用于反证，不据此生成列名。
    data_boundary = (
        _first_course_data_row(extracted)
        if code_column is None and not _non_course_labels(labels, extracted)
        else None
    )
    notes: list[str] = []
    fragment_row: int | None = None
    if data_boundary == 0:
        if indices:
            notes.append(
                "原生表头边界覆盖课程数据行，已保留为数据并等待可靠表头继承"
            )
        indices = []
    elif data_boundary is not None:
        if _first_row_fragment(extracted, data_boundary):
            indices = []
            fragment_row = 0
            notes.append(
                "续表首行是跨页课程名碎片，已原样保留为数据，未补造课程字段"
            )
        else:
            clamped = [index for index in indices if index < data_boundary]
            if len(clamped) != len(indices):
                notes.append(
                    f"原生表头边界越过首个课程数据行 r{data_boundary}，只保留其前的表头行"
                )
            indices = clamped
    if (
        indices == [0]
        and len(raw_rows) > 1
        and boundary != 1
        and data_boundary is None
    ):
        group_header = any(
            _raw_span_width(raw_rows[0], col) >= 2
            for col in range(len(raw_rows[0]))
        )
        # 仅凭 colspan 不能证明次行是子表头：培养环节编号、相对时间等
        # 数据也会落在分组下方。必须有独立 origin 子表头单元格。
        if group_header:
            if _pdf_subheader_evidence(raw_rows, extracted):
                indices = [0, 1]
                notes.append("跨列分组表头下有独立子表头 origin 单元格，已把次行纳入表头")
            else:
                notes.append("跨列分组表头下没有独立子表头 origin 单元格，次行保留为数据")
    return (
        [index for index in dict.fromkeys(indices) if 0 <= index < len(raw_rows)],
        notes,
        fragment_row,
    )


def _pdf_placements_grid(
    table: Any,
    *,
    column_count: int,
    row_count: int,
    header_set: set[int],
    table_id: str,
) -> list[list[TableCell]] | None:
    """展开 refine=True 的 packed SpanCell 网格；None 位置由跨度占用推导。"""
    placements = getattr(table, "placements", None)
    if not placements:
        return None
    grid: list[list[TableCell | None]] = [[None] * column_count for _ in range(row_count)]
    occupied: set[tuple[int, int]] = set()
    for row_index, row in enumerate(placements):
        col_index = 0
        for placed in row:
            while (row_index, col_index) in occupied:
                col_index += 1
            rowspan = max(1, int(getattr(placed, "rowspan", 1) or 1))
            colspan = max(1, int(getattr(placed, "colspan", 1) or 1))
            bbox = _rect_values(getattr(placed, "bbox", None))
            text = _clean(str(getattr(placed, "text", "") or ""))
            origin = TableCell(
                cell_id=f"{table_id}#r{row_index}c{col_index}",
                row=row_index,
                col=col_index,
                rowspan=rowspan,
                colspan=colspan,
                origin_row=row_index,
                origin_col=col_index,
                text=text,
                native_text=text,
                text_source="native",
                is_header=row_index in header_set,
                bbox=list(bbox) if bbox is not None else None,
            )
            for row_offset in range(rowspan):
                for col_offset in range(colspan):
                    target_row = row_index + row_offset
                    target_col = col_index + col_offset
                    if target_row >= row_count or target_col >= column_count:
                        continue
                    covered = bool(row_offset or col_offset)
                    if grid[target_row][target_col] is not None:
                        continue
                    grid[target_row][target_col] = (
                        TableCell(
                            cell_id=f"{table_id}#r{target_row}c{target_col}",
                            row=target_row,
                            col=target_col,
                            rowspan=rowspan,
                            colspan=colspan,
                            origin_row=row_index,
                            origin_col=col_index,
                            covered=True,
                            text="",
                            is_header=origin.is_header,
                            bbox=origin.bbox,
                        )
                        if covered
                        else origin
                    )
                    occupied.add((target_row, target_col))
            col_index += colspan
    return [
        [
            cell
            if cell is not None
            else _empty_pdf_cell(table_id, row_index, col_index, header_set)
            for col_index, cell in enumerate(row_cells)
        ]
        for row_index, row_cells in enumerate(grid)
    ]


def _empty_pdf_cell(
    table_id: str,
    row: int,
    col: int,
    header_set: set[int],
    bbox: list[float] | None = None,
    text: str = "",
) -> TableCell:
    return TableCell(
        cell_id=f"{table_id}#r{row}c{col}",
        row=row,
        col=col,
        origin_row=row,
        origin_col=col,
        text=text,
        native_text=text or None,
        text_source="native" if text else None,
        is_header=row in header_set,
        bbox=bbox,
    )


def extract_pdf_table(
    table: Any,
    *,
    doc_id: str,
    version_id: str,
    table_index: int,
    page: int,
    block_id: str,
    source_page: Any | None = None,
) -> tuple[TableStructure, str]:
    """从 PyMuPDF 公开 Table 对象保存网格；空列、合并格与真实坐标都不丢失。

    ``source_page`` 提供原始页面时，用单元格 bbox 的坐标裁剪文本校验
    ``table.extract()`` 的字符顺序；检出差异只保留原生值并标记复核，或仅在
    字符集合完全一致（纯顺序差异）时采用裁剪文本，绝不静默覆盖原生证据。
    """
    table_id = f"{doc_id}@{version_id}#table{table_index}"
    warnings: list[str] = []
    try:
        extracted = table.extract()
    except (AttributeError, RuntimeError, TypeError):
        extracted = []
    raw_rows = [list(getattr(row, "cells", []) or []) for row in getattr(table, "rows", [])]
    if not raw_rows:
        raw_rows = [
            [None for _ in range(len(row))] for row in extracted
        ]
    column_count = int(getattr(table, "col_count", 0) or 0)
    if not column_count:
        column_count = max((len(row) for row in extracted), default=0)
    row_count = max(len(raw_rows), len(extracted))
    while len(raw_rows) < row_count:
        raw_rows.append([])

    row_tops: list[float] = []
    for row in getattr(table, "rows", []):
        rect = _rect_values(getattr(row, "bbox", None))
        row_tops.append(rect[1] if rect is not None else 0.0)
    if len(row_tops) < row_count:
        row_tops.extend([0.0] * (row_count - len(row_tops)))
    header_indices, header_notes, fragment_row = _pdf_header_indices(
        table, raw_rows, extracted, row_tops
    )
    warnings.extend(header_notes)
    header_set = set(header_indices)

    placement_grid = _pdf_placements_grid(
        table,
        column_count=column_count,
        row_count=row_count,
        header_set=header_set,
        table_id=table_id,
    )
    if placement_grid is not None:
        grid: list[list[TableCell]] = placement_grid
    else:
        col_starts = _cluster_positions(
            [
                rect[0]
                for row in raw_rows
                for cell in row
                if (rect := _rect_values(cell)) is not None
            ]
        )
        mapped_columns = len(col_starts) == column_count
        slot_starts = [
            (
                _cluster_positions(
                    [
                        rect[0]
                        for row in raw_rows
                        if col < len(row)
                        and (rect := _rect_values(row[col])) is not None
                    ]
                ) or [float(col)]
            )[0]
            for col in range(column_count)
        ]
        grid_slots: list[list[TableCell | None]] = [
            [None] * column_count for _ in range(row_count)
        ]
        for row_index, row in enumerate(raw_rows):
            for slot, raw_cell in enumerate(row):
                rect = _rect_values(raw_cell)
                if rect is None:
                    continue
                origin_col = slot
                if mapped_columns:
                    origin_col = min(
                        range(column_count),
                        key=lambda index: abs(col_starts[index] - rect[0]),
                    )
                    if abs(col_starts[origin_col] - rect[0]) > 2.0:
                        origin_col = slot
                origin_col = min(max(origin_col, 0), max(column_count - 1, 0))
                colspan = 1
                rowspan = 1
                if mapped_columns:
                    colspan = max(
                        1,
                        sum(
                            1
                            for start in col_starts
                            if rect[0] - 0.6 <= start < rect[2] - 0.6
                        ),
                    )
                    rowspan = max(
                        1,
                        sum(
                            1
                            for top in row_tops
                            if rect[1] - 0.6 <= top < rect[3] - 0.6
                        ),
                    )
                elif len(slot_starts) == column_count:
                    # 坐标聚类不可直接映射时，至少保留原始槽位；跨度降级为 1。
                    origin_col = slot
                text = ""
                row_values = extracted[row_index] if row_index < len(extracted) else []
                if slot < len(row_values) and row_values[slot] is not None:
                    text = _clean(str(row_values[slot]))
                origin = TableCell(
                    cell_id=f"{table_id}#r{row_index}c{origin_col}",
                    row=row_index,
                    col=origin_col,
                    rowspan=rowspan,
                    colspan=colspan,
                    origin_row=row_index,
                    origin_col=origin_col,
                    text=text,
                    native_text=text,
                    text_source="native",
                    is_header=row_index in header_set,
                    bbox=list(rect),
                )
                for row_offset in range(rowspan):
                    for col_offset in range(colspan):
                        target_row = row_index + row_offset
                        target_col = origin_col + col_offset
                        if target_row >= row_count or target_col >= column_count:
                            continue
                        if grid_slots[target_row][target_col] is not None:
                            if (row_offset or col_offset) == 0:
                                warnings.append(
                                    f"PDF 表格单元格重叠：r{target_row}c{target_col}，以先出现者为准"
                                )
                            continue
                        covered = bool(row_offset or col_offset)
                        grid_slots[target_row][target_col] = (
                            TableCell(
                                cell_id=f"{table_id}#r{target_row}c{target_col}",
                                row=target_row,
                                col=target_col,
                                rowspan=rowspan,
                                colspan=colspan,
                                origin_row=row_index,
                                origin_col=origin_col,
                                covered=True,
                                text="",
                                is_header=origin.is_header,
                                bbox=origin.bbox,
                            )
                            if covered
                            else origin
                        )
        for row_index in range(row_count):
            for col_index in range(column_count):
                if grid_slots[row_index][col_index] is not None:
                    continue
                raw_cell = (
                    raw_rows[row_index][col_index]
                    if col_index < len(raw_rows[row_index])
                    else None
                )
                rect = _rect_values(raw_cell)
                row_values = extracted[row_index] if row_index < len(extracted) else []
                text = ""
                if col_index < len(row_values) and row_values[col_index] is not None:
                    text = _clean(str(row_values[col_index]))
                grid_slots[row_index][col_index] = _empty_pdf_cell(
                    table_id,
                    row_index,
                    col_index,
                    header_set,
                    bbox=list(rect) if rect is not None else None,
                    text=text,
                )
        grid = [
            [cell for cell in row if cell is not None]
            for row in grid_slots
        ]

    if source_page is not None:
        for _row_index, row in enumerate(grid):
            for cell in row:
                if cell.covered or cell.bbox is None:
                    continue
                clip_text = _pdf_clip_text(source_page, cell.bbox)
                if clip_text is None:
                    continue
                native = cell.text
                native_compact = "".join(native.split())
                clip_compact = "".join(clip_text.split())
                if native_compact == clip_compact:
                    continue
                cell.native_text = native
                if native and sorted(native_compact) == sorted(clip_compact):
                    cell.text = clip_text
                    cell.text_source = "clip_text"
                    warnings.append(
                        f"单元格 {cell.cell_id} 原生文本顺序不稳定，已按原页坐标文本校正并保留原生值"
                    )
                else:
                    cell.needs_review = True
                    if _number_tokens(native) != _number_tokens(clip_text):
                        warnings.append(
                            f"单元格 {cell.cell_id} 原生文本与坐标裁剪数字不一致"
                            f"（{native!r} vs {clip_text!r}），已保留原生值并标记复核"
                        )
                    else:
                        warnings.append(
                            f"单元格 {cell.cell_id} 原生文本与坐标裁剪不一致，"
                            "已保留原生值并标记复核"
                        )

    if fragment_row is not None and 0 <= fragment_row < len(grid):
        # 碎片格保留原文，但不得让下游把它当成完整课程行。
        for cell in grid[fragment_row]:
            if not cell.covered and cell.text.strip():
                cell.needs_review = True

    rows: list[TableRow] = []
    for row_index, row_cells in enumerate(grid):
        slots = row_leaf_slots(
            row_cells,
            row_index=row_index,
            column_count=column_count,
            resolve_origin=lambda origin_row, origin_col: _grid_cell(
                grid, origin_row, origin_col
            ),
        )
        rows.append(
            TableRow(
                row_id=f"{table_id}#row{row_index}",
                row_index=row_index,
                is_header=row_index in header_set,
                cells=row_cells,
                text=join_row_texts(
                    [cell.text if cell is not None else "" for cell in slots]
                ),
                locator=SourceLocator(
                    format="pdf",
                    page=page,
                    bbox=_bbox_values(getattr(table, "bbox", None)),
                ),
            )
        )

    header_paths: list[list[str]] = []
    for col in range(column_count):
        path: list[str] = []
        for row_index in header_indices:
            if row_index >= len(rows):
                continue
            cell = rows[row_index].cells[col]
            if cell.covered:
                origin_row = rows[cell.origin_row] if cell.origin_row < len(rows) else None
                if origin_row is not None and cell.origin_col < len(origin_row.cells):
                    cell = origin_row.cells[cell.origin_col]
            text = cell.text.strip()
            if text and (not path or path[-1] != text):
                path.append(text)
        header_paths.append(path)
    column_headers = [" > ".join(path) for path in header_paths]
    if any(cell.rowspan > 1 or cell.colspan > 1 for row in rows for cell in row.cells):
        warnings.append("PDF 表格含合并单元格，已按原页坐标展开并保留跨度")
    empty_slots = sum(1 for row in rows for cell in row.cells if not cell.text and not cell.covered)
    if empty_slots:
        warnings.append(f"PDF 表格保留 {empty_slots} 个空单元格位")
    if any(len(row) < column_count for row in extracted):
        warnings.append("PDF 表格行宽不一致，已补齐空单元格位")
    review_cells = sum(1 for row in rows for cell in row.cells if cell.needs_review)
    if review_cells:
        warnings.append(f"PDF 表格有 {review_cells} 个单元格文本需复核，已保留原生值")

    bbox = _bbox_values(getattr(table, "bbox", None))
    rendered = "\n".join(row.text for row in rows if row.text.strip())
    structure = TableStructure(
        table_id=table_id,
        block_id=block_id,
        table_index=table_index,
        page=page,
        bbox=bbox,
        rows=rows,
        header_row_indices=header_indices,
        column_count=column_count,
        row_count=len(rows),
        column_headers=column_headers,
        header_paths=header_paths,
        # 支持矩阵与课程表都按完整行切块；classify_table_kind 再做语义标记。
        kind="data",
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=page, bbox=bbox),
        warnings=warnings,
    )
    structure.column_bindings = _local_header_bindings(structure)
    binding_note = _header_binding_warning(structure.column_bindings)
    if binding_note:
        structure.warnings.append(binding_note)
    return structure, rendered


_PDF_CONTINUATION_COLUMN_TOLERANCE = 2.0
_HEADER_INTERVAL_MATCH_TOLERANCE = 1.0


def _cell_interval(cell: TableCell) -> tuple[float, float] | None:
    if cell.covered or cell.colspan != 1:
        return None
    bbox = _bbox_values(cell.bbox)
    if bbox is None:
        return None
    left, right = bbox[0], bbox[2]
    if right <= left:
        return None
    return (left, right)


def _table_column_intervals(
    table: TableStructure,
) -> list[tuple[float, float] | None]:
    """每列的真实 x 区间；优先表头格，缺列时由数据行同列格兜底。"""
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
            bounds[cell.col] = _cell_interval(cell)
    return bounds


def _table_column_bboxes(
    table: TableStructure,
) -> list[list[float] | None]:
    """每列真实单元格四值 bbox；缺失保持 None，不从 x 区间补造矩形。"""
    bounds: list[list[float] | None] = [None] * max(0, table.column_count)
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
            if cell.covered or not 0 <= cell.col < len(bounds):
                continue
            if bounds[cell.col] is not None:
                continue
            bbox = _bbox_values(cell.bbox)
            if bbox is not None:
                bounds[cell.col] = bbox
    return bounds


def _bounds_match(
    left: tuple[float, float] | None,
    right: tuple[float, float] | None,
) -> bool:
    if left is None or right is None:
        return False
    return (
        abs(left[0] - right[0]) <= _PDF_CONTINUATION_COLUMN_TOLERANCE
        and abs(left[1] - right[1]) <= _PDF_CONTINUATION_COLUMN_TOLERANCE
    )


def _table_identity(table_id: str) -> str:
    return table_id.split("#", 1)[0].strip()


def _bbox_interval(bbox: list[float] | None) -> tuple[float, float] | None:
    rect = _rect_values(bbox)
    if rect is None:
        return None
    left, right = rect[0], rect[2]
    if right <= left:
        return None
    return (left, right)


def _header_leaf_cell(
    table: TableStructure, row_index: int, col: int
) -> TableCell | None:
    if not 0 <= row_index < len(table.rows):
        return None
    row = table.rows[row_index]
    if not 0 <= col < len(row.cells):
        return None
    return row.cells[col]


def _header_origin_cell(
    table: TableStructure, cell: TableCell | None
) -> TableCell | None:
    if cell is None:
        return None
    if not cell.covered:
        return cell
    origin = _header_leaf_cell(table, cell.origin_row, cell.origin_col)
    if origin is None or origin.covered:
        return None
    return origin


def _clear_reviewed_header_column(table: TableStructure, col: int) -> None:
    """只清除被 needs_review 来源影响的解释性列名；原始行单元格不动。"""
    if 0 <= col < len(table.column_headers):
        table.column_headers[col] = ""
    if 0 <= col < len(table.header_paths):
        table.header_paths[col] = []


def _append_warning_once(table: TableStructure, warning: str) -> None:
    if warning not in table.warnings:
        table.warnings.append(warning)


def _local_header_bindings(table: TableStructure) -> list[HeaderColumnBinding]:
    """本表表头各 leaf 列的真实 origin 单元格与覆盖缺口；不做跨列猜名。"""
    column_count = max(0, table.column_count)
    if not column_count:
        return []
    target_bboxes = _table_column_bboxes(table)
    header_indices = [
        index
        for index in table.header_row_indices
        if 0 <= index < len(table.rows)
    ]
    if not header_indices:
        header_indices = [row.row_index for row in table.rows if row.is_header]
    bindings: list[HeaderColumnBinding] = []
    for col in range(column_count):
        target_bbox = (
            list(target_bboxes[col])
            if col < len(target_bboxes) and target_bboxes[col] is not None
            else None
        )
        path = list(table.header_paths[col]) if col < len(table.header_paths) else []
        if not header_indices:
            bindings.append(
                HeaderColumnBinding(
                    target_col=col,
                    status="unresolved",
                    target_bbox=target_bbox,
                    reason="表格没有本地表头行",
                )
            )
            continue
        origin_cells: list[TableCell] = []
        last_leaf: TableCell | None = None
        last_origin: TableCell | None = None
        for row_index in header_indices:
            leaf = _header_leaf_cell(table, row_index, col)
            if leaf is None:
                continue
            last_leaf = leaf
            origin = _header_origin_cell(table, leaf)
            if origin is None:
                continue
            last_origin = origin
            if all(origin.cell_id != item.cell_id for item in origin_cells):
                origin_cells.append(origin)
        if last_leaf is None or last_origin is None:
            bindings.append(
                HeaderColumnBinding(
                    target_col=col,
                    status="unresolved",
                    target_bbox=target_bbox,
                    reason="表头行缺少该列单元格",
                )
            )
            continue
        # target_bbox 只取真实目标格矩形；缺失时保留 None，不从区间补造。
        target_bbox = _cell_bbox(last_leaf)
        if target_bbox is None and col < len(target_bboxes):
            target_bbox = (
                list(target_bboxes[col]) if target_bboxes[col] is not None else None
            )
        reviewed_cells = [item for item in origin_cells if item.needs_review]
        if reviewed_cells:
            # 任一贡献来源单元格被标记复核（含跨列分组祖先）都不能提供
            # 可靠列名；保留来源证据供本地 OCR/视觉区域复核。
            review_cell = reviewed_cells[0]
            review_text = " > ".join(
                item.text.strip() for item in origin_cells if item.text.strip()
            )
            _clear_reviewed_header_column(table, col)
            _append_warning_once(
                table,
                f"表头来源需复核：c{col} 的来源单元格 "
                f"{', '.join(item.cell_id for item in origin_cells)} 标记 needs_review"
                "（原生/裁剪文本冲突），已保留来源 cell/bbox/文本供本地 OCR/视觉复核，"
                "未生成列名",
            )
            bindings.append(
                HeaderColumnBinding(
                    target_col=col,
                    status="unresolved",
                    method="none",
                    target_bbox=target_bbox,
                    review_source_table_id=table.table_id,
                    review_source_page=table.page,
                    review_source_col=review_cell.col,
                    review_source_header_cell_ids=[
                        item.cell_id for item in origin_cells
                    ],
                    review_source_header_row_indices=list(header_indices),
                    review_source_bbox=_cell_bbox(review_cell),
                    review_source_text=review_text,
                    reason=(
                        "表头来源单元格标记 needs_review（原生/裁剪文本冲突），"
                        "未生成可靠列名"
                    ),
                )
            )
            continue
        label_cells = [item for item in origin_cells if item.text.strip()]
        label_origin = label_cells[-1] if label_cells else None
        # 纵向 rowspan 只是同一列向下覆盖，不构成跨列共享；只有横向
        # colspan 或被跨列覆盖的 leaf 才是 shared_scope。
        cross_column = last_leaf.covered and last_leaf.origin_col != col
        group_only_label = (
            label_origin is not None
            and label_origin is not last_origin
            and not last_origin.text.strip()
            and any(item.colspan > 1 for item in origin_cells)
        )
        label_shared = (
            cross_column or last_origin.colspan > 1 or group_only_label
        )
        label_bbox = label_origin or last_origin
        common = {
            "target_col": col,
            "source_table_id": table.table_id,
            "source_page": table.page,
            "source_col": label_bbox.col,
            "source_header_cell_ids": [item.cell_id for item in origin_cells],
            "source_header_row_indices": list(header_indices),
            "source_bbox": _cell_bbox(label_bbox),
            "target_bbox": target_bbox,
            "header_text": label_bbox.text,
            "header_path": path,
            "covered": last_leaf.covered,
            "shared_scope": label_shared,
        }
        if label_origin is None or not any(part.strip() for part in path):
            bindings.append(
                HeaderColumnBinding(
                    status="gap",
                    method="local_header",
                    reason="表头单元格为空，未生成列名（空白列缺口）",
                    **common,
                )
            )
        elif label_shared:
            bindings.append(
                HeaderColumnBinding(
                    status="shared",
                    method="local_header",
                    reason="跨列共享表头，子列作用域未拆分",
                    **common,
                )
            )
        else:
            bindings.append(
                HeaderColumnBinding(
                    status="bound",
                    method="local_header",
                    **common,
                )
            )
    return bindings


def _table_cell_by_id(table: TableStructure, cell_id: str) -> TableCell | None:
    for row in table.rows:
        for cell in row.cells:
            if cell.cell_id == cell_id:
                return cell
    return None


def _binding_source_unreliable(
    table: TableStructure, binding: HeaderColumnBinding
) -> bool:
    """旧快照里已 bound/shared 的绑定，来源不可靠时必须降级而不是沿用。"""
    if binding.status not in {"bound", "shared"}:
        return False
    if binding.review_source_header_cell_ids:
        return True
    col = binding.target_col
    # 只有显式存在但已清空的列名才算“旧字段复活”；缺失整个数组的旧快照
    # 仍按绑定自身路径处理，避免把 schema 缺省误判成复核清空。
    path = (
        table.header_paths[col]
        if 0 <= col < len(table.header_paths)
        else None
    )
    label = (
        table.column_headers[col]
        if 0 <= col < len(table.column_headers)
        else None
    )
    if path is not None and not any(part.strip() for part in path):
        return True
    if label is not None and not label.strip():
        return True
    return any(
        (cell := _table_cell_by_id(table, cell_id)) is not None
        and cell.needs_review
        for cell_id in binding.source_header_cell_ids
    )


def _reviewed_legacy_binding(
    table: TableStructure, binding: HeaderColumnBinding
) -> HeaderColumnBinding:
    flagged = next(
        (
            cell
            for cell_id in binding.source_header_cell_ids
            if (cell := _table_cell_by_id(table, cell_id)) is not None
            and cell.needs_review
        ),
        None,
    )
    return HeaderColumnBinding(
        target_col=binding.target_col,
        status="unresolved",
        method="none",
        target_bbox=(
            list(binding.target_bbox) if binding.target_bbox is not None else None
        ),
        review_source_table_id=(
            binding.review_source_table_id or binding.source_table_id
        ),
        review_source_page=(
            binding.review_source_page
            if binding.review_source_page is not None
            else binding.source_page
        ),
        review_source_col=(
            flagged.col
            if flagged is not None
            else (
                binding.review_source_col
                if binding.review_source_col is not None
                else binding.source_col
            )
        ),
        review_source_header_cell_ids=(
            list(binding.review_source_header_cell_ids)
            or list(binding.source_header_cell_ids)
        ),
        review_source_header_row_indices=(
            list(binding.review_source_header_row_indices)
            or list(binding.source_header_row_indices)
        ),
        review_source_bbox=(
            _cell_bbox(flagged)
            if flagged is not None
            else (
                _bbox_values(binding.review_source_bbox)
                if binding.review_source_bbox is not None
                else _bbox_values(binding.source_bbox)
            )
        ),
        review_source_text=(
            flagged.text.strip()
            if flagged is not None and flagged.text.strip()
            else (binding.review_source_text or binding.header_text)
        ),
        reason=(
            "旧表头绑定来源标记 needs_review 或列名已清空，已降级为未解析；"
            "保留来源证据等待本地 OCR/视觉复核"
        ),
    )


def _effective_column_bindings(
    table: TableStructure,
) -> list[HeaderColumnBinding]:
    stored = table.column_bindings
    if len(stored) == table.column_count and stored:
        reviewed_cols = {
            binding.target_col
            for binding in stored
            if _binding_source_unreliable(table, binding)
        }
        if not reviewed_cols:
            if any(
                binding.status in {"bound", "shared"} for binding in stored
            ) or any(
                binding.review_source_header_cell_ids for binding in stored
            ):
                return stored
            return _local_header_bindings(table)
        sanitized = [
            _reviewed_legacy_binding(table, binding)
            if binding.target_col in reviewed_cols
            and binding.status in {"bound", "shared"}
            else binding
            for binding in stored
        ]
        for col in reviewed_cols:
            _clear_reviewed_header_column(table, col)
            _append_warning_once(
                table,
                f"表头来源需复核：c{col} 的旧表头绑定来源已标记 needs_review "
                "或列名已清空；已降级为 unresolved，来源 cell/bbox/文本保留"
                "等待本地 OCR/视觉复核",
            )
        table.column_bindings = sanitized
        return sanitized
    return _local_header_bindings(table)


def _header_binding_warning(
    bindings: list[HeaderColumnBinding],
) -> str | None:
    if not bindings:
        return None
    counts = {
        status: sum(1 for binding in bindings if binding.status == status)
        for status in ("bound", "shared", "gap", "unresolved")
    }
    if not (counts["shared"] or counts["gap"] or counts["unresolved"]):
        return None
    return (
        f"表头列绑定：{counts['bound']} 列唯一，{counts['shared']} 列跨列共享，"
        f"{counts['gap']} 列空表头缺口，{counts['unresolved']} 列未解析"
    )


def _edge_delta(
    left: tuple[float, float], right: tuple[float, float]
) -> float:
    return max(abs(left[0] - right[0]), abs(left[1] - right[1]))


def _unbound_binding(
    col: int,
    target_bbox: list[float] | None,
    status: Literal["unresolved", "ambiguous", "offset"],
    reason: str,
) -> HeaderColumnBinding:
    return HeaderColumnBinding(
        target_col=col,
        status=status,
        target_bbox=target_bbox,
        reason=reason,
    )


def _identity_target_col(
    bindings: list[HeaderColumnBinding],
    source_binding: HeaderColumnBinding,
) -> int | None:
    if source_binding.source_col is None or not source_binding.source_header_cell_ids:
        return None
    matches = [
        binding.target_col
        for binding in bindings
        if binding.status == "bound"
        and binding.source_col == source_binding.source_col
        and binding.source_header_cell_ids == source_binding.source_header_cell_ids
    ]
    return matches[0] if len(matches) == 1 else None


def _continuation_column_bindings(
    previous: TableStructure,
    previous_bindings: list[HeaderColumnBinding],
    target_bounds: list[tuple[float, float] | None],
    target_bboxes: list[list[float] | None],
    *,
    origin_table_id: str,
    origin_page: int | None,
    recovered_col: int | None,
) -> list[HeaderColumnBinding]:
    """按真实 x 区间把每个目标列绑定到来源表头 origin；允许部分成功。"""
    sources: list[tuple[HeaderColumnBinding, tuple[float, float]]] = []
    for binding in previous_bindings:
        if binding.status not in {"bound", "shared"}:
            continue
        if binding.review_source_header_cell_ids:
            continue
        if not binding.source_header_cell_ids:
            continue
        if not any(part.strip() for part in binding.header_path):
            continue
        # 来源表自身的解释性列名已被清空时，绑定不能再提供可继承标签。
        source_col = binding.target_col
        if (
            0 <= source_col < len(previous.header_paths)
            and not any(part.strip() for part in previous.header_paths[source_col])
        ):
            continue
        if (
            0 <= source_col < len(previous.column_headers)
            and not previous.column_headers[source_col].strip()
        ):
            continue
        interval = _bbox_interval(binding.source_bbox)
        if interval is None:
            continue
        sources.append((binding, interval))

    exact_lists: dict[int, list[int]] = {}
    near_lists: dict[int, list[int]] = {}
    for col, target in enumerate(target_bounds):
        exact: list[int] = []
        near: list[int] = []
        if target is not None:
            for index, (_binding, interval) in enumerate(sources):
                delta = _edge_delta(target, interval)
                if delta <= _HEADER_INTERVAL_MATCH_TOLERANCE:
                    exact.append(index)
                elif delta <= _PDF_CONTINUATION_COLUMN_TOLERANCE:
                    near.append(index)
        exact_lists[col] = exact
        near_lists[col] = near

    sole = {
        col: exact[0]
        for col, exact in exact_lists.items()
        if len(exact) == 1
    }
    usage: dict[int, list[int]] = {}
    for col, source_index in sole.items():
        usage.setdefault(source_index, []).append(col)
    conflicted = {
        col for col, source_index in sole.items() if len(usage[source_index]) > 1
    }

    via_table_id = (
        previous.table_id if previous.table_id != origin_table_id else None
    )
    bindings: list[HeaderColumnBinding] = []
    for col, target in enumerate(target_bounds):
        target_bbox = (
            list(target_bboxes[col])
            if col < len(target_bboxes) and target_bboxes[col] is not None
            else None
        )
        exact = exact_lists[col]
        near = near_lists[col]
        if col in conflicted:
            bindings.append(
                _unbound_binding(
                    col,
                    target_bbox,
                    "ambiguous",
                    "多个目标列共享同一来源列物理区间，无法唯一确定",
                )
            )
            continue
        if len(exact) == 1:
            source_binding, _interval = sources[exact[0]]
            shared = (
                source_binding.status == "shared"
                or source_binding.shared_scope
            )
            bindings.append(
                HeaderColumnBinding(
                    target_col=col,
                    status="shared" if shared else "bound",
                    method=(
                        "trailing_empty_recovery"
                        if col == recovered_col
                        else "physical_interval"
                    ),
                    source_table_id=origin_table_id,
                    source_via_table_id=via_table_id,
                    source_page=origin_page,
                    source_col=source_binding.source_col,
                    source_header_cell_ids=list(
                        source_binding.source_header_cell_ids
                    ),
                    source_header_row_indices=list(
                        source_binding.source_header_row_indices
                    ),
                    source_bbox=_bbox_values(source_binding.source_bbox),
                    target_bbox=target_bbox,
                    header_text=(
                        source_binding.header_text
                        or source_binding.header_path[-1]
                    ),
                    header_path=list(source_binding.header_path),
                    covered=source_binding.covered,
                    shared_scope=shared,
                    reason=(
                        "物理列区间唯一匹配；跨列共享表头，子列作用域未拆分"
                        if shared
                        else "物理列区间唯一匹配"
                    ),
                )
            )
            continue
        if len(exact) > 1:
            bindings.append(
                _unbound_binding(
                    col,
                    target_bbox,
                    "ambiguous",
                    f"{len(exact)} 个来源表头列具有相同物理列区间，无法唯一确定",
                )
            )
            continue
        if len(near) == 1 and target is not None:
            _source_binding, interval = sources[near[0]]
            delta = _edge_delta(target, interval)
            bindings.append(
                _unbound_binding(
                    col,
                    target_bbox,
                    "offset",
                    f"物理列边界偏移 {delta:.2f}pt，未继承该列标签",
                )
            )
            continue
        if len(near) > 1:
            bindings.append(
                _unbound_binding(
                    col,
                    target_bbox,
                    "ambiguous",
                    f"{len(near)} 个来源列物理区间与目标列接近，无法唯一确定",
                )
            )
            continue
        bindings.append(
            _unbound_binding(
                col,
                target_bbox,
                "unresolved",
                (
                    "缺少目标列物理区间"
                    if target is None
                    else "没有匹配的来源表头物理区间"
                ),
            )
        )
    return bindings


def _table_course_rows(table: TableStructure) -> list[TableRow]:
    rows: list[TableRow] = []
    for row in table.rows:
        if row.is_header:
            continue
        values = [""] * table.column_count
        for cell in row.cells:
            if not cell.covered and 0 <= cell.col < len(values):
                values[cell.col] = cell.text
        if _course_like_values(values):
            rows.append(row)
    return rows


def _row_vertical_bounds(
    table: TableStructure, row: TableRow
) -> tuple[float, float] | None:
    rects = [
        bbox
        for cell in row.cells
        if not cell.covered
        and cell.rowspan == 1
        and (bbox := _bbox_values(cell.bbox)) is not None
    ]
    if not rects:
        rects = [
            bbox
            for cell in row.cells
            if not cell.covered
            and (bbox := _bbox_values(cell.bbox)) is not None
        ]
    if rects:
        return (
            min(rect[1] for rect in rects),
            max(rect[3] for rect in rects),
        )
    table_bbox = _bbox_values(table.bbox)
    if table_bbox is not None:
        return (table_bbox[1], table_bbox[3])
    return None


def _recover_trailing_empty_column(
    previous: TableStructure,
    current: TableStructure,
    *,
    previous_bounds: list[tuple[float, float] | None],
    current_bounds: list[tuple[float, float] | None],
) -> list[list[str]] | None:
    """仅凭原表头边界恢复被忽略的最右侧空白 leaf 列；证据不足返回 None。"""
    if current.column_count + 1 != previous.column_count:
        return None
    extra = previous.column_count - 1
    for col in range(current.column_count):
        if not _bounds_match(previous_bounds[col], current_bounds[col]):
            return None
    extra_bounds = previous_bounds[extra]
    if extra_bounds is None:
        return None
    path = previous.header_paths[extra]
    if not any(part.strip() for part in path):
        return None
    if not previous.column_headers[extra].strip():
        return None
    current_rect = _bbox_values(current.bbox)
    current_right = (
        current_rect[2]
        if current_rect is not None
        else max(
            (bounds[1] for bounds in current_bounds if bounds is not None),
            default=None,
        )
    )
    if current_right is None:
        return None
    if abs(extra_bounds[0] - current_right) > _PDF_CONTINUATION_COLUMN_TOLERANCE:
        return None
    if extra_bounds[1] - extra_bounds[0] <= _PDF_CONTINUATION_COLUMN_TOLERANCE:
        return None
    for row in current.rows:
        vertical = _row_vertical_bounds(current, row)
        row.cells.append(
            TableCell(
                cell_id=f"{current.table_id}#r{row.row_index}c{extra}",
                row=row.row_index,
                col=extra,
                origin_row=row.row_index,
                origin_col=extra,
                is_header=row.is_header,
                bbox=(
                    [extra_bounds[0], vertical[0], extra_bounds[1], vertical[1]]
                    if vertical is not None
                    else None
                ),
            )
        )
        if row.text.strip():
            row.text = f"{row.text} | "
    current.column_count = previous.column_count
    return [list(path) for path in previous.header_paths]


def restore_continuation_header(
    previous: TableStructure, current: TableStructure
) -> bool:
    """同文档相邻页课程续表按物理列区间继承表头；无法证明的列保持未解析。"""
    if previous.page is None or current.page is None:
        return False
    if current.page != previous.page + 1:
        return False
    if current.header_row_indices or any(row.is_header for row in current.rows):
        return False
    identity = _table_identity(current.table_id)
    if not identity or _table_identity(previous.table_id) != identity:
        return False
    origin_table_id = previous.header_source_table_id or previous.table_id
    if _table_identity(origin_table_id) != identity:
        return False
    origin_page = (
        previous.header_source_page
        if previous.header_source_page is not None
        else previous.page
    )
    previous_bindings = _effective_column_bindings(previous)
    # 身份列只从已验证生效的绑定路径识别，不读可能残留的旧 column_headers。
    previous_labels = [""] * previous.column_count
    for binding in previous_bindings:
        if (
            binding.status in {"bound", "shared"}
            and binding.header_path
            and 0 <= binding.target_col < len(previous_labels)
        ):
            previous_labels[binding.target_col] = " > ".join(binding.header_path)
    code_column, name_column = _course_identity_columns(previous_labels)
    if code_column is None or name_column is None:
        return False
    code_binding = next(
        (binding for binding in previous_bindings if binding.target_col == code_column),
        None,
    )
    name_binding = next(
        (binding for binding in previous_bindings if binding.target_col == name_column),
        None,
    )
    if (
        code_binding is None
        or name_binding is None
        or code_binding.status != "bound"
        or name_binding.status != "bound"
        or code_binding.source_col is None
        or name_binding.source_col is None
        or code_binding.source_col == name_binding.source_col
        or not any(part.strip() for part in code_binding.header_path)
        or not any(part.strip() for part in name_binding.header_path)
    ):
        return False
    if not _table_course_rows(previous):
        return False
    if not _table_course_rows(current):
        return False
    if not _course_identity_row_compatible(previous, code_column, name_column):
        return False
    previous_bounds = _table_column_intervals(previous)
    current_bounds = _table_column_intervals(current)
    current_bboxes = _table_column_bboxes(current)
    if not previous_bounds or not current_bounds:
        return False
    recovered_col: int | None = None
    if current.column_count != previous.column_count:
        recovered = _recover_trailing_empty_column(
            previous,
            current,
            previous_bounds=previous_bounds,
            current_bounds=current_bounds,
        )
        if recovered is not None:
            recovered_col = current.column_count - 1
            current_bounds = _table_column_intervals(current)
            current_bboxes = _table_column_bboxes(current)
    bindings = _continuation_column_bindings(
        previous,
        previous_bindings,
        current_bounds,
        current_bboxes,
        origin_table_id=origin_table_id,
        origin_page=origin_page,
        recovered_col=recovered_col,
    )
    inherited = [
        binding
        for binding in bindings
        if binding.status in {"bound", "shared"}
    ]
    if not inherited:
        return False
    # 旧行为：列数变少且恰好命中原表前列时，只有几何恢复尾部空列才允许继承；
    # 不能因为前 N 列都对上就盲目前缀继承（可能是另一张少列表）。
    if (
        current.column_count < previous.column_count
        and recovered_col is None
        and len(inherited) == current.column_count
    ):
        source_columns = {
            binding.source_col
            for binding in bindings
            if binding.source_col is not None
        }
        if source_columns == set(range(current.column_count)):
            return False
    if (
        current.column_count == previous.column_count
        and len(inherited) != current.column_count
    ):
        return False
    current_code_column = _identity_target_col(bindings, code_binding)
    current_name_column = _identity_target_col(bindings, name_binding)
    if (
        current_code_column is None
        or current_name_column is None
        or current_code_column == current_name_column
        or not _course_identity_row_compatible(
            current, current_code_column, current_name_column
        )
    ):
        return False
    current.header_row_indices = []
    current.column_headers = [
        (
            " > ".join(binding.header_path)
            if binding.status in {"bound", "shared"}
            else ""
        )
        for binding in bindings
    ]
    current.header_paths = [
        list(binding.header_path)
        if binding.status in {"bound", "shared"}
        else []
        for binding in bindings
    ]
    current.column_bindings = bindings
    current.continuation_of = previous.table_id
    current.header_source_table_id = origin_table_id
    current.header_source_page = origin_page
    current.warnings.append(
        f"跨页续表未重复表头，已按物理列区间继承原表头 {origin_table_id}"
        f"（第 {origin_page} 页）{len(inherited)}/{current.column_count} 列；"
        "本表没有本地表头行"
    )
    unresolved = [
        binding
        for binding in bindings
        if binding.status not in {"bound", "shared"}
    ]
    if unresolved:
        current.warnings.append(
            "跨页续表未解析列："
            + "、".join(
                f"c{binding.target_col}({binding.status}: {binding.reason})"
                for binding in unresolved
            )
        )
    shared = [binding for binding in bindings if binding.status == "shared"]
    if shared:
        current.warnings.append(
            "跨页续表共享表头列："
            + "、".join(f"c{binding.target_col}" for binding in shared)
            + "；子列作用域未拆分，未生成独立列名"
        )
    if recovered_col is not None:
        current.warnings.append(
            "按原表头边界恢复了最右侧被忽略的空白列，"
            f"列名 {current.column_headers[recovered_col]!r}"
        )
    return True


def classify_table_kind(table: TableStructure) -> Literal["data", "matrix"]:
    """识别“适用学院/专业×层次”支持矩阵，避免误当课程清单。"""
    if table.kind == "layout":
        return "layout"
    headers = " ".join(table.column_headers)
    matrix_markers = ("适用", "面向", "支持", "学院", "专业", "层次", "类别")
    if sum(1 for marker in matrix_markers if marker in headers) >= 2:
        values = [
            cell.text.strip()
            for row in table.data_rows()
            for cell in row.cells
            if cell.text.strip()
        ]
        short = sum(1 for value in values if len(value) <= 6)
        if values and short / len(values) >= 0.6:
            return "matrix"
    return table.kind


__all__ = [
    "AssetResource",
    "HeaderColumnBinding",
    "PageStructure",
    "ParsedStructure",
    "SourceLocator",
    "StructureBlock",
    "TableCell",
    "TableRow",
    "TableStructure",
    "build_html_structure",
    "classify_table_kind",
    "dom_path",
    "extract_html_assets",
    "extract_html_table",
    "extract_pdf_table",
    "iter_html_blocks",
    "restore_continuation_header",
]
