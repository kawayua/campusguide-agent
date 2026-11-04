"""Header-column provenance: ingestion validation, payload roundtrip, runtime gating.

The tables are synthetic shapes matching the structure extractor's output.
They prove lossless transport and fail-closed semantics only; they do not claim
real-PDF extraction accuracy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from campusguide.domain.enums import ArticleStatus, Audience
from campusguide.domain.models import (
    Article,
    ArticlePublic,
    DocumentVersion,
    HeaderColumnProvenance,
)
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.segment import HeaderBindingError, segment_document
from campusguide.ingest.structure import (
    HeaderColumnBinding,
    ParsedStructure,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)
from campusguide.retrieval.indexer import (
    VectorIndexError,
    _validate_parent_links,
    article_payload,
)
from campusguide.retrieval.service import article_from_payload
from campusguide.retrieval.source_spans import structured_table_row

DOC_ID = "header-doc"
VERSION_ID = "v1"
SCOPE = f"{DOC_ID}@{VERSION_ID}"
TABLE_ONE = f"{SCOPE}#table1"
TABLE_TWO = f"{SCOPE}#table2"
TABLE_THREE = f"{SCOPE}#table3"
SOURCE_URL = "https://example.edu/header-table"

THREE_COLUMNS = ((50.0, 150.0), (150.0, 300.0), (300.0, 360.0))
FIVE_COLUMNS = (
    (50.0, 150.0),
    (150.0, 300.0),
    (300.0, 360.0),
    (360.0, 420.0),
    (420.0, 500.0),
)
SIX_COLUMNS = (
    (50.0, 150.0),
    (150.0, 300.0),
    (300.0, 360.0),
    (360.0, 420.0),
    (420.0, 480.0),
    (480.0, 540.0),
)
ROW0_Y = (100.0, 120.0)
ROW1_Y = (120.0, 140.0)
ROW2_Y = (140.0, 160.0)
HEADERS_THREE = ("课程代码", "课程名称", "学分")
HEADERS_FIVE = ("课程代码", "课程名称", "学分", "", "开课学期")
VALUES_THREE = ("CS101", "高等数学", "3")
VALUES_FIVE = ("CS101", "高等数学", "3", "4", "第1学期")


def _rect(x_span: tuple[float, float], y_span: tuple[float, float]) -> list[float]:
    return [x_span[0], y_span[0], x_span[1], y_span[1]]


def _cell(
    table_id: str,
    row: int,
    col: int,
    text: str,
    *,
    x_span: tuple[float, float],
    y_span: tuple[float, float],
    is_header: bool = False,
    covered: bool = False,
    origin_row: int | None = None,
    origin_col: int | None = None,
    rowspan: int = 1,
    colspan: int = 1,
    needs_review: bool = False,
) -> TableCell:
    return TableCell(
        cell_id=f"{table_id}#r{row}c{col}",
        row=row,
        col=col,
        rowspan=rowspan,
        colspan=colspan,
        origin_row=row if origin_row is None else origin_row,
        origin_col=col if origin_col is None else origin_col,
        covered=covered,
        text=text,
        text_source="native",
        is_header=is_header,
        bbox=_rect(x_span, y_span),
        needs_review=needs_review,
    )


def _row(
    table_id: str,
    row_index: int,
    cells: list[TableCell],
    *,
    page: int,
    is_header: bool,
) -> TableRow:
    return TableRow(
        row_id=f"{table_id}#row{row_index}",
        row_index=row_index,
        is_header=is_header,
        cells=cells,
        text=" | ".join(cell.text for cell in cells),
        locator=SourceLocator(format="pdf", page=page),
    )


def _table(
    table_id: str,
    *,
    table_index: int,
    page: int,
    rows: list[TableRow],
    header_row_indices: list[int],
    column_count: int,
    column_headers: tuple[str, ...],
    header_paths: list[list[str]],
    column_bindings: list[HeaderColumnBinding] | None = None,
    continuation_of: str | None = None,
    header_source_table_id: str | None = None,
    header_source_page: int | None = None,
) -> TableStructure:
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=table_index,
        page=page,
        rows=rows,
        header_row_indices=header_row_indices,
        column_count=column_count,
        row_count=len(rows),
        column_headers=list(column_headers),
        header_paths=header_paths,
        column_bindings=column_bindings or [],
        kind="data",
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=page),
        continuation_of=continuation_of,
        header_source_table_id=header_source_table_id,
        header_source_page=header_source_page,
    )


def _version() -> DocumentVersion:
    return DocumentVersion(
        doc_id=DOC_ID,
        version_id=VERSION_ID,
        title="课程表",
        doc_type="regulation",
        issuing_department="教务处",
        source_url=SOURCE_URL,
        published_at=date(2026, 10, 1),
        effective_from=date(2026, 9, 1),
        status=ArticleStatus.ACTIVE,
        source_sha256="a" * 64,
        fetched_at=datetime(2026, 10, 19, tzinfo=UTC),
        parser_version="test",
        audience=[Audience.UNDERGRADUATE],
    )


def _parsed(tables: list[TableStructure]) -> ParsedDocument:
    text = "第一条 课程安排见下表。\n"
    return ParsedDocument(
        doc_id=DOC_ID,
        version_id=VERSION_ID,
        title="课程表",
        text=text,
        format="pdf",
        source_sha256="a" * 64,
        fetched_at="2026-10-19T08:00:00+00:00",
        parser_version="test",
        cjk_count=len(text),
        char_count=len(text),
        structure=ParsedStructure(format="pdf", tables=tables),
    )


def _bound_binding(
    table_id: str,
    page: int,
    col: int,
    cell: TableCell,
    *,
    rows: tuple[int, ...] = (0,),
    header_text: str,
    header_path: list[str],
) -> HeaderColumnBinding:
    return HeaderColumnBinding(
        target_col=col,
        status="bound",
        method="local_header",
        source_table_id=table_id,
        source_page=page,
        source_col=cell.col,
        source_header_cell_ids=[cell.cell_id],
        source_header_row_indices=list(rows),
        source_bbox=list(cell.bbox or []),
        target_bbox=list(cell.bbox or []),
        header_text=header_text,
        header_path=list(header_path),
        reason="本地表头原格",
    )


def _local_three_table(*, bindings: bool = True) -> TableStructure:
    header_cells = [
        _cell(
            TABLE_ONE,
            0,
            col,
            HEADERS_THREE[col],
            x_span=THREE_COLUMNS[col],
            y_span=ROW0_Y,
            is_header=True,
        )
        for col in range(3)
    ]
    data_cells = [
        _cell(
            TABLE_ONE,
            1,
            col,
            VALUES_THREE[col],
            x_span=THREE_COLUMNS[col],
            y_span=ROW1_Y,
        )
        for col in range(3)
    ]
    column_bindings = (
        [
            _bound_binding(
                TABLE_ONE,
                1,
                col,
                header_cells[col],
                header_text=HEADERS_THREE[col],
                header_path=[HEADERS_THREE[col]],
            )
            for col in range(3)
        ]
        if bindings
        else []
    )
    return _table(
        TABLE_ONE,
        table_index=1,
        page=1,
        rows=[
            _row(TABLE_ONE, 0, header_cells, page=1, is_header=True),
            _row(TABLE_ONE, 1, data_cells, page=1, is_header=False),
        ],
        header_row_indices=[0],
        column_count=3,
        column_headers=HEADERS_THREE,
        header_paths=[[header] for header in HEADERS_THREE],
        column_bindings=column_bindings,
    )


def _continuation_table(
    source: TableStructure,
    *,
    table_id: str,
    table_index: int,
    page: int,
    values: tuple[str, str, str],
    via: str | None = None,
    target_shift: float = 0.0,
) -> TableStructure:
    x_spans = tuple(
        (left + target_shift, right + target_shift) for left, right in THREE_COLUMNS
    )
    data_cells = [
        _cell(
            table_id,
            0,
            col,
            values[col],
            x_span=x_spans[col],
            y_span=ROW1_Y,
        )
        for col in range(3)
    ]
    origin_table_id = source.header_source_table_id or source.table_id
    origin_page = source.header_source_page or source.page
    bindings: list[HeaderColumnBinding] = []
    for col, origin_binding in enumerate(source.column_bindings):
        bindings.append(
            HeaderColumnBinding(
                target_col=col,
                status="bound",
                method="physical_interval",
                source_table_id=origin_table_id,
                source_via_table_id=via,
                source_page=origin_page,
                source_col=origin_binding.source_col,
                source_header_cell_ids=list(origin_binding.source_header_cell_ids),
                source_header_row_indices=list(
                    origin_binding.source_header_row_indices
                ),
                source_bbox=(
                    list(origin_binding.source_bbox)
                    if origin_binding.source_bbox is not None
                    else None
                ),
                target_bbox=list(data_cells[col].bbox or []),
                header_text=origin_binding.header_text,
                header_path=list(origin_binding.header_path),
                covered=origin_binding.covered,
                shared_scope=origin_binding.shared_scope,
                reason="物理列区间唯一匹配",
            )
        )
    return _table(
        table_id,
        table_index=table_index,
        page=page,
        rows=[_row(table_id, 0, data_cells, page=page, is_header=False)],
        header_row_indices=[],
        column_count=3,
        column_headers=source.column_headers,
        header_paths=[list(path) for path in source.header_paths],
        column_bindings=bindings,
        continuation_of=via or source.table_id,
        header_source_table_id=origin_table_id,
        header_source_page=origin_page,
    )


def _chained_parsed(*, final_shift: float = 0.0) -> ParsedDocument:
    origin = _local_three_table()
    middle = _continuation_table(
        origin,
        table_id=TABLE_TWO,
        table_index=2,
        page=2,
        values=("MA101", "线性代数", "4"),
    )
    final = _continuation_table(
        origin,
        table_id=TABLE_THREE,
        table_index=3,
        page=3,
        values=("PH201", "大学物理", "3"),
        via=TABLE_TWO,
        target_shift=final_shift,
    )
    return _parsed([origin, middle, final])


def _shared_five_table() -> TableStructure:
    row0 = [
        _cell(
            TABLE_ONE,
            0,
            0,
            "课程代码",
            x_span=FIVE_COLUMNS[0],
            y_span=ROW0_Y,
            is_header=True,
        ),
        _cell(
            TABLE_ONE,
            0,
            1,
            "课程名称",
            x_span=FIVE_COLUMNS[1],
            y_span=ROW0_Y,
            is_header=True,
        ),
        _cell(
            TABLE_ONE,
            0,
            2,
            "学分",
            x_span=(FIVE_COLUMNS[2][0], FIVE_COLUMNS[3][1]),
            y_span=ROW0_Y,
            is_header=True,
            colspan=2,
        ),
        _cell(
            TABLE_ONE,
            0,
            3,
            "",
            x_span=FIVE_COLUMNS[3],
            y_span=ROW0_Y,
            is_header=True,
            covered=True,
            origin_row=0,
            origin_col=2,
        ),
        _cell(
            TABLE_ONE,
            0,
            4,
            "开课学期",
            x_span=FIVE_COLUMNS[4],
            y_span=ROW0_Y,
            is_header=True,
        ),
    ]
    group_cell = row0[2]
    subheader_cells = [
        _cell(
            TABLE_ONE,
            1,
            col,
            "",
            x_span=FIVE_COLUMNS[col],
            y_span=ROW1_Y,
            is_header=True,
        )
        for col in (2, 3)
    ]
    data_cells = [
        _cell(
            TABLE_ONE,
            2,
            col,
            VALUES_FIVE[col],
            x_span=FIVE_COLUMNS[col],
            y_span=ROW2_Y,
        )
        for col in range(5)
    ]
    bindings = [
        _bound_binding(
            TABLE_ONE,
            1,
            col,
            row0[col],
            rows=(0, 1),
            header_text=HEADERS_FIVE[col],
            header_path=[HEADERS_FIVE[col]],
        )
        for col in (0, 1)
    ]
    for col, subheader in ((2, subheader_cells[0]), (3, subheader_cells[1])):
        bindings.append(
            HeaderColumnBinding(
                target_col=col,
                status="shared",
                method="local_header",
                source_table_id=TABLE_ONE,
                source_page=1,
                source_col=group_cell.col,
                source_header_cell_ids=[
                    group_cell.cell_id,
                    subheader.cell_id,
                ],
                source_header_row_indices=[0, 1],
                source_bbox=list(group_cell.bbox or []),
                target_bbox=list(subheader.bbox or []),
                header_text="学分",
                header_path=["学分"],
                shared_scope=True,
                reason="跨列共享表头，子列作用域未拆分",
            )
        )
    bindings.append(
        _bound_binding(
            TABLE_ONE,
            1,
            4,
            row0[4],
            rows=(0, 1),
            header_text="开课学期",
            header_path=["开课学期"],
        )
    )
    bindings.sort(key=lambda binding: binding.target_col)
    return _table(
        TABLE_ONE,
        table_index=1,
        page=1,
        rows=[
            _row(TABLE_ONE, 0, row0, page=1, is_header=True),
            _row(TABLE_ONE, 1, subheader_cells, page=1, is_header=True),
            _row(TABLE_ONE, 2, data_cells, page=1, is_header=False),
        ],
        header_row_indices=[0, 1],
        column_count=5,
        column_headers=(
            "课程代码",
            "课程名称",
            "学分",
            "学分",
            "开课学期",
        ),
        header_paths=[
            ["课程代码"],
            ["课程名称"],
            ["学分"],
            ["学分"],
            ["开课学期"],
        ],
        column_bindings=bindings,
    )


def _partial_five_table(status: str) -> TableStructure:
    row0 = [
        _cell(
            TABLE_ONE,
            0,
            col,
            HEADERS_FIVE[col],
            x_span=FIVE_COLUMNS[col],
            y_span=ROW0_Y,
            is_header=True,
        )
        for col in range(5)
    ]
    data_cells = [
        _cell(
            TABLE_ONE,
            1,
            col,
            VALUES_FIVE[col],
            x_span=FIVE_COLUMNS[col],
            y_span=ROW1_Y,
        )
        for col in range(5)
    ]
    bindings = [
        _bound_binding(
            TABLE_ONE,
            1,
            col,
            row0[col],
            header_text=HEADERS_FIVE[col],
            header_path=[HEADERS_FIVE[col]],
        )
        for col in (0, 1, 2, 4)
    ]
    bindings.append(
        HeaderColumnBinding(
            target_col=3,
            status=status,  # type: ignore[arg-type]
            method="none",
            target_bbox=list(row0[3].bbox or []),
            reason="表头无法唯一展开",
        )
    )
    bindings.sort(key=lambda binding: binding.target_col)
    return _table(
        TABLE_ONE,
        table_index=1,
        page=1,
        rows=[
            _row(TABLE_ONE, 0, row0, page=1, is_header=True),
            _row(TABLE_ONE, 1, data_cells, page=1, is_header=False),
        ],
        header_row_indices=[0],
        column_count=5,
        column_headers=HEADERS_FIVE,
        header_paths=[
            ["课程代码"],
            ["课程名称"],
            ["学分"],
            [],
            ["开课学期"],
        ],
        column_bindings=bindings,
    )


def _gap_five_table() -> TableStructure:
    table = _partial_five_table("ambiguous")
    header_cells = table.rows[0].cells
    bindings = [
        binding for binding in table.column_bindings if binding.target_col != 3
    ]
    empty_header = header_cells[3]
    bindings.append(
        HeaderColumnBinding(
            target_col=3,
            status="gap",
            method="local_header",
            source_table_id=TABLE_ONE,
            source_page=1,
            source_col=3,
            source_header_cell_ids=[empty_header.cell_id],
            source_header_row_indices=[0],
            source_bbox=list(empty_header.bbox or []),
            target_bbox=list(empty_header.bbox or []),
            header_text="",
            header_path=[],
            covered=True,
            reason="表头单元格为空，未生成列名（空白列缺口）",
        )
    )
    bindings.sort(key=lambda binding: binding.target_col)
    return table.model_copy(update={"column_bindings": bindings})


def _reviewed_six_table() -> TableStructure:
    """Reviewed group header: c2/c3 keep audit evidence, safe columns stay bound."""
    header0 = [
        _cell(
            TABLE_ONE,
            0,
            0,
            "课程代码",
            x_span=SIX_COLUMNS[0],
            y_span=ROW0_Y,
            is_header=True,
        ),
        _cell(
            TABLE_ONE,
            0,
            1,
            "课程名称",
            x_span=SIX_COLUMNS[1],
            y_span=ROW0_Y,
            is_header=True,
        ),
        _cell(
            TABLE_ONE,
            0,
            2,
            "总学时",
            x_span=(SIX_COLUMNS[2][0], SIX_COLUMNS[3][1]),
            y_span=ROW0_Y,
            is_header=True,
            colspan=2,
            needs_review=True,
        ),
        _cell(
            TABLE_ONE,
            0,
            3,
            "",
            x_span=(SIX_COLUMNS[2][0], SIX_COLUMNS[3][1]),
            y_span=ROW0_Y,
            is_header=True,
            covered=True,
            origin_row=0,
            origin_col=2,
            colspan=2,
        ),
        _cell(
            TABLE_ONE,
            0,
            4,
            "学分",
            x_span=SIX_COLUMNS[4],
            y_span=(ROW0_Y[0], ROW1_Y[1]),
            is_header=True,
            rowspan=2,
        ),
        _cell(
            TABLE_ONE,
            0,
            5,
            "开课学期",
            x_span=SIX_COLUMNS[5],
            y_span=(ROW0_Y[0], ROW1_Y[1]),
            is_header=True,
            rowspan=2,
        ),
    ]
    header1 = [
        _cell(
            TABLE_ONE,
            1,
            0,
            "",
            x_span=SIX_COLUMNS[0],
            y_span=(ROW0_Y[0], ROW1_Y[1]),
            is_header=True,
            covered=True,
            origin_row=0,
            origin_col=0,
            rowspan=2,
        ),
        _cell(
            TABLE_ONE,
            1,
            1,
            "",
            x_span=SIX_COLUMNS[1],
            y_span=(ROW0_Y[0], ROW1_Y[1]),
            is_header=True,
            covered=True,
            origin_row=0,
            origin_col=1,
            rowspan=2,
        ),
        _cell(
            TABLE_ONE,
            1,
            2,
            "理论",
            x_span=SIX_COLUMNS[2],
            y_span=ROW1_Y,
            is_header=True,
        ),
        _cell(
            TABLE_ONE,
            1,
            3,
            "实践",
            x_span=SIX_COLUMNS[3],
            y_span=ROW1_Y,
            is_header=True,
        ),
        _cell(
            TABLE_ONE,
            1,
            4,
            "",
            x_span=SIX_COLUMNS[4],
            y_span=(ROW0_Y[0], ROW1_Y[1]),
            is_header=True,
            covered=True,
            origin_row=0,
            origin_col=4,
            rowspan=2,
        ),
        _cell(
            TABLE_ONE,
            1,
            5,
            "",
            x_span=SIX_COLUMNS[5],
            y_span=(ROW0_Y[0], ROW1_Y[1]),
            is_header=True,
            covered=True,
            origin_row=0,
            origin_col=5,
            rowspan=2,
        ),
    ]
    data_values = ("CS101", "高等数学", "48", "16", "3", "第1学期")
    data_cells = [
        _cell(
            TABLE_ONE,
            2,
            col,
            data_values[col],
            x_span=SIX_COLUMNS[col],
            y_span=ROW2_Y,
        )
        for col in range(6)
    ]
    group_bbox = list(header0[2].bbox or [])
    bindings = [
        _bound_binding(
            TABLE_ONE,
            1,
            col,
            header0[col],
            rows=(0, 1),
            header_text=header0[col].text,
            header_path=[header0[col].text],
        )
        for col in (0, 1)
    ]
    for col, subheader, text_value in (
        (2, header1[2], "总学时 > 理论"),
        (3, header1[3], "总学时 > 实践"),
    ):
        bindings.append(
            HeaderColumnBinding(
                target_col=col,
                status="unresolved",
                method="none",
                target_bbox=list(subheader.bbox or []),
                review_source_table_id=TABLE_ONE,
                review_source_page=1,
                review_source_col=header0[2].col,
                review_source_header_cell_ids=[
                    header0[2].cell_id,
                    subheader.cell_id,
                ],
                review_source_header_row_indices=[0, 1],
                review_source_bbox=group_bbox,
                review_source_text=text_value,
                reason="表头来源标记 needs_review，等待本地 OCR/视觉复核",
            )
        )
    for col in (4, 5):
        bindings.append(
            _bound_binding(
                TABLE_ONE,
                1,
                col,
                header0[col],
                rows=(0, 1),
                header_text=header0[col].text,
                header_path=[header0[col].text],
            )
        )
    bindings.sort(key=lambda binding: binding.target_col)
    return _table(
        TABLE_ONE,
        table_index=1,
        page=1,
        rows=[
            _row(TABLE_ONE, 0, header0, page=1, is_header=True),
            _row(TABLE_ONE, 1, header1, page=1, is_header=True),
            _row(TABLE_ONE, 2, data_cells, page=1, is_header=False),
        ],
        header_row_indices=[0, 1],
        column_count=6,
        column_headers=("课程代码", "课程名称", "", "", "学分", "开课学期"),
        header_paths=[
            ["课程代码"],
            ["课程名称"],
            [],
            [],
            ["学分"],
            ["开课学期"],
        ],
        column_bindings=bindings,
    )


def _table_articles(result, table_id: str):
    parent = next(
        article
        for article in result.articles
        if article.block_kind == "table_parent" and article.table_id == table_id
    )
    row = next(
        article
        for article in result.articles
        if article.block_kind == "table_row" and article.table_id == table_id
    )
    return parent, row


def _local_articles() -> tuple[Article, Article]:
    result = segment_document(_parsed([_local_three_table()]), _version())
    return _table_articles(result, TABLE_ONE)


def _reviewed_articles() -> tuple[Article, Article]:
    result = segment_document(_parsed([_reviewed_six_table()]), _version())
    return _table_articles(result, TABLE_ONE)


def _partial_row(status: str) -> Article:
    result = segment_document(_parsed([_partial_five_table(status)]), _version())
    _, row = _table_articles(result, TABLE_ONE)
    return row


def _chained_final_articles() -> tuple[Article, Article]:
    result = segment_document(_chained_parsed(), _version())
    return _table_articles(result, TABLE_THREE)


def test_local_bindings_roundtrip_through_public_and_payload() -> None:
    parent, row = _local_articles()

    assert parent.table_column_bindings == row.table_column_bindings
    assert len(row.table_column_bindings) == 3
    assert [binding.target_col for binding in row.table_column_bindings] == [0, 1, 2]
    assert all(
        binding.status == "bound" and binding.method == "local_header"
        for binding in row.table_column_bindings
    )

    first = row.table_column_bindings[0]
    assert first.source_table_id == TABLE_ONE
    assert first.source_page == 1
    assert first.source_col == 0
    assert first.target_bbox_kind == "rect"
    assert first.source_header_cell_ids == [f"{TABLE_ONE}#r0c0"]
    assert [cell.cell_id for cell in first.source_header_cells] == [
        f"{TABLE_ONE}#r0c0"
    ]
    assert first.source_header_cells[0].text == "课程代码"
    assert first.source_header_cells[0].bbox == [50.0, 100.0, 150.0, 120.0]
    assert first.source_header_cells[0].is_header is True

    public = ArticlePublic.from_article(row)
    assert public.table_column_bindings == row.table_column_bindings
    assert public.table_column_bindings[0].source_header_cells[0].text == "课程代码"

    payload = article_payload(row)
    payload_binding = payload["table_column_bindings"][0]
    assert payload_binding["target_bbox_kind"] == "rect"
    assert payload_binding["source_header_cells"][0]["cell_id"] == (
        f"{TABLE_ONE}#r0c0"
    )
    restored = article_from_payload(payload)
    assert restored.table_column_bindings == row.table_column_bindings
    assert Article.model_validate_json(row.model_dump_json()).table_column_bindings == (
        row.table_column_bindings
    )

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.course_code == "CS101"
    assert structured.course_name == "高等数学"
    assert structured.value("credits") == "3"
    assert structured.quote == "行：CS101 | 高等数学 | 3"
    assert structured.header_quote == "表头：课程代码 | 课程名称 | 学分"
    assert "source_header_cells" not in row.text


def test_reviewed_source_audit_roundtrips_and_keeps_safe_numeric_fields() -> None:
    parent, row = _reviewed_articles()

    assert parent.table_column_bindings == row.table_column_bindings
    reviewed = row.table_column_bindings[2]
    assert reviewed.status == "unresolved"
    assert reviewed.method == "none"
    assert reviewed.source_table_id is None
    assert reviewed.source_via_table_id is None
    assert reviewed.source_page is None
    assert reviewed.source_col is None
    assert reviewed.source_header_cell_ids == []
    assert reviewed.source_header_row_indices == []
    assert reviewed.source_bbox is None
    assert reviewed.source_header_cells == []
    assert reviewed.header_text == ""
    assert reviewed.header_path == []
    assert reviewed.review_source_table_id == TABLE_ONE
    assert reviewed.review_source_page == 1
    assert reviewed.review_source_col == 2
    assert reviewed.review_source_header_cell_ids == [
        f"{TABLE_ONE}#r0c2",
        f"{TABLE_ONE}#r1c2",
    ]
    assert reviewed.review_source_header_row_indices == [0, 1]
    assert reviewed.review_source_bbox == [300.0, 100.0, 420.0, 120.0]
    assert reviewed.review_source_text == "总学时 > 理论"
    assert [cell.cell_id for cell in reviewed.review_source_header_cells] == [
        f"{TABLE_ONE}#r0c2",
        f"{TABLE_ONE}#r1c2",
    ]
    assert [cell.text for cell in reviewed.review_source_header_cells] == [
        "总学时",
        "理论",
    ]
    assert [
        cell.needs_review for cell in reviewed.review_source_header_cells
    ] == [True, False]
    assert reviewed.review_source_header_cells[0].bbox == [
        300.0,
        100.0,
        420.0,
        120.0,
    ]

    practice = row.table_column_bindings[3]
    assert practice.review_source_text == "总学时 > 实践"
    assert practice.review_source_header_cell_ids == [
        f"{TABLE_ONE}#r0c2",
        f"{TABLE_ONE}#r1c3",
    ]

    public = ArticlePublic.from_article(row)
    assert public.table_column_bindings == row.table_column_bindings
    payload = article_payload(row)
    payload_review = payload["table_column_bindings"][2]
    assert payload_review["review_source_text"] == "总学时 > 理论"
    assert payload_review["review_source_header_cells"][0]["cell_id"] == (
        f"{TABLE_ONE}#r0c2"
    )
    restored = article_from_payload(payload)
    assert restored.table_column_bindings == row.table_column_bindings
    assert Article.model_validate_json(row.model_dump_json()).table_column_bindings == (
        row.table_column_bindings
    )

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.course_code == "CS101"
    assert structured.course_name == "高等数学"
    assert structured.value("credits") == "3"
    assert structured.value("semester") == "第1学期"
    assert structured.columns[2].field == "other"
    assert structured.columns[3].field == "other"
    assert structured.values[2] == "48"
    assert structured.values[3] == "16"
    assert not any(problem.startswith("BINDING_") for problem in structured.problems)


def test_ingestion_rejects_bound_source_cell_needing_review() -> None:
    table = _local_three_table()
    table.rows[0].cells[0].needs_review = True

    with pytest.raises(HeaderBindingError) as error:
        segment_document(_parsed([table]), _version())
    assert "needs_review" in str(error.value)


def test_runtime_rejects_bound_source_header_cell_needing_review() -> None:
    _, row = _local_articles()
    binding = row.table_column_bindings[0]
    flagged = binding.source_header_cells[0].model_copy(
        update={"needs_review": True}
    )
    forged = _replace_binding(
        row,
        0,
        binding.model_copy(update={"source_header_cells": [flagged]}),
    )

    structured = structured_table_row(forged)
    assert structured is not None and not structured.reliable
    assert not structured.curriculum_row
    assert "BINDING_SOURCE_NEEDS_REVIEW:0" in structured.problems


@pytest.mark.parametrize(
    "field",
    (
        "target_col",
        "source_page",
        "source_col",
        "review_source_page",
        "review_source_col",
    ),
)
def test_integer_fields_reject_bool_before_coercion(field: str) -> None:
    payload: dict[str, object] = {"target_col": 0, field: True}
    with pytest.raises(ValidationError) as error:
        HeaderColumnProvenance(**payload)
    assert "布尔" in str(error.value)


def test_review_row_indices_reject_bool_before_coercion() -> None:
    with pytest.raises(ValidationError):
        HeaderColumnProvenance(
            target_col=0,
            review_source_header_row_indices=[True],
        )


def test_chained_bindings_hydrate_original_header_cells() -> None:
    parent, row = _chained_final_articles()

    assert parent.table_id == TABLE_THREE
    assert parent.table_continuation_of == TABLE_TWO
    assert parent.table_header_source_id == TABLE_ONE
    assert parent.table_header_source_page == 1
    assert parent.table_column_bindings == row.table_column_bindings

    first = row.table_column_bindings[0]
    assert first.source_table_id == TABLE_ONE
    assert first.source_via_table_id == TABLE_TWO
    assert first.source_page == 1
    assert first.method == "physical_interval"
    assert first.source_header_cell_ids == [f"{TABLE_ONE}#r0c0"]
    assert [cell.cell_id for cell in first.source_header_cells] == [
        f"{TABLE_ONE}#r0c0"
    ]
    assert first.source_header_cells[0].text == "课程代码"
    assert first.source_header_cells[0].bbox == [50.0, 100.0, 150.0, 120.0]

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.course_code == "PH201"
    assert structured.value("credits") == "3"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"source_table_id": "foreign-doc@v1#table1"},
            "本地表头来源必须是当前表",
        ),
        ({"source_col": 99}, "超出来源表列数"),
        ({"source_page": 0}, "来源页号 0 非正数"),
        ({"header_text": "伪造标签"}, "来源标签与 header_text 不一致"),
        ({"header_path": ["伪造路径"]}, "表头路径与表格列头不一致"),
        (
            {"source_bbox": [55.0, 100.0, 155.0, 120.0]},
            "source_bbox 与来源标签格实际坐标不一致",
        ),
        ({"status": "shared"}, "shared 列必须标记 shared_scope"),
        (
            {"status": "bound", "shared_scope": True},
            "bound 列不能标记 shared_scope",
        ),
        (
            {"target_bbox": [float("nan"), 100.0, 150.0, 120.0]},
            "target_bbox 非有限或退化",
        ),
        (
            {"source_header_cell_ids": [f"{TABLE_ONE}#r1c0"]},
            "不是表头格",
        ),
        ({"source_header_cell_ids": []}, "缺少来源表头单元格"),
    ],
)
def test_ingestion_rejects_forged_local_binding(
    updates: dict[str, object],
    expected: str,
) -> None:
    table = _local_three_table()
    bindings = list(table.column_bindings)
    bindings[0] = bindings[0].model_copy(update=updates)
    forged = table.model_copy(update={"column_bindings": bindings})

    with pytest.raises(HeaderBindingError) as error:
        segment_document(_parsed([forged]), _version())
    assert expected in str(error.value)


def test_ingestion_rejects_missing_or_duplicate_binding_slots() -> None:
    table = _local_three_table()

    missing = table.model_copy(
        update={"column_bindings": list(table.column_bindings[:2])}
    )
    with pytest.raises(HeaderBindingError) as error:
        segment_document(_parsed([missing]), _version())
    assert "表头绑定数 2 与列数 3 不一致" in str(error.value)

    duplicate = list(table.column_bindings)
    duplicate[1] = duplicate[1].model_copy(update={"target_col": 0})
    duplicated = table.model_copy(update={"column_bindings": duplicate})
    with pytest.raises(HeaderBindingError) as error:
        segment_document(_parsed([duplicated]), _version())
    assert "表头绑定列不唯一或未覆盖全部 leaf 列" in str(error.value)


def test_ingestion_rejects_unbound_binding_with_source_evidence() -> None:
    table = _local_three_table()
    bindings = list(table.column_bindings)
    bindings[0] = bindings[0].model_copy(
        update={"status": "ambiguous", "method": "none"}
    )
    forged = table.model_copy(update={"column_bindings": bindings})

    with pytest.raises(HeaderBindingError) as error:
        segment_document(_parsed([forged]), _version())
    assert "未绑定列不能携带来源证据" in str(error.value)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"source_via_table_id": TABLE_ONE},
            "链上来源表指向自身或 origin",
        ),
        (
            {"source_via_table_id": None},
            "直接继承来源与前表不一致",
        ),
        (
            {"source_page": 2},
            "继承来源页与可信续表头页不一致",
        ),
        (
            {"source_table_id": TABLE_TWO},
            "继承来源与可信续表头来源不一致",
        ),
        (
            {"source_header_cell_ids": [f"{TABLE_TWO}#r0c0"]},
            "来源单元格",
        ),
    ],
)
def test_ingestion_rejects_forged_inherited_binding(
    updates: dict[str, object],
    expected: str,
) -> None:
    parsed = _chained_parsed()
    final = parsed.structure.tables[2]
    bindings = list(final.column_bindings)
    bindings[0] = bindings[0].model_copy(update=updates)
    forged = final.model_copy(update={"column_bindings": bindings})
    parsed = parsed.model_copy(
        update={
            "structure": parsed.structure.model_copy(
                update={"tables": [*parsed.structure.tables[:2], forged]}
            )
        }
    )

    with pytest.raises(HeaderBindingError) as error:
        segment_document(parsed, _version())
    assert expected in str(error.value)


def test_ingestion_rejects_physical_interval_outside_strict_tolerance() -> None:
    with pytest.raises(HeaderBindingError) as error:
        segment_document(_chained_parsed(final_shift=5.0), _version())
    assert "物理列区间超出严格匹配容差" in str(error.value)


def test_legacy_two_value_target_bbox_stays_an_explicit_interval() -> None:
    table = _local_three_table()
    bindings = [
        binding.model_copy(
            update={
                "target_bbox": [
                    float(binding.target_bbox[0]),
                    float(binding.target_bbox[2]),
                ]
            }
        )
        for binding in table.column_bindings
    ]
    forged = table.model_copy(update={"column_bindings": bindings})

    result = segment_document(_parsed([forged]), _version())
    _, row = _table_articles(result, TABLE_ONE)
    assert all(
        binding.target_bbox_kind == "x_interval"
        for binding in row.table_column_bindings
    )
    assert row.table_column_bindings[0].target_bbox == [50.0, 150.0]

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.value("credits") == "3"


@pytest.mark.parametrize("status", ["ambiguous", "unresolved", "offset"])
def test_unbound_numeric_column_does_not_invalidate_proven_fields(
    status: str,
) -> None:
    row = _partial_row(status)

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.course_code == "CS101"
    assert structured.course_name == "高等数学"
    assert structured.value("credits") == "3"
    assert structured.value("semester") == "第1学期"
    assert structured.columns[3].field == "other"
    assert structured.values[3] == "4"
    assert not any(problem.startswith("BINDING_") for problem in structured.problems)


def test_gap_column_keeps_blank_scope_without_invalidating_other_fields() -> None:
    result = segment_document(_parsed([_gap_five_table()]), _version())
    _, row = _table_articles(result, TABLE_ONE)

    gap = row.table_column_bindings[3]
    assert gap.status == "gap"
    assert gap.method == "local_header"
    assert gap.header_path == []
    assert gap.header_text == ""
    assert [cell.text for cell in gap.source_header_cells] == [""]

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.value("credits") == "3"
    assert structured.value("semester") == "第1学期"
    assert structured.columns[3].field == "other"
    assert structured.values[3] == "4"


def test_shared_scope_columns_cannot_supply_unique_numeric_fields() -> None:
    result = segment_document(_parsed([_shared_five_table()]), _version())
    _, row = _table_articles(result, TABLE_ONE)

    shared = row.table_column_bindings[2:4]
    assert [binding.status for binding in shared] == ["shared", "shared"]
    assert all(binding.shared_scope for binding in shared)
    assert all(binding.source_col == 2 for binding in shared)
    assert [cell.text for cell in shared[1].source_header_cells] == ["学分", ""]

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.course_code == "CS101"
    assert structured.value("credits") == ""
    assert structured.columns[2].field == "other"
    assert structured.columns[3].field == "other"
    assert structured.value("semester") == "第1学期"


def test_empty_bindings_keep_legacy_field_derivation() -> None:
    result = segment_document(
        _parsed([_local_three_table(bindings=False)]), _version()
    )
    _, row = _table_articles(result, TABLE_ONE)
    assert row.table_column_bindings == []

    structured = structured_table_row(row)
    assert structured is not None and structured.reliable
    assert structured.course_code == "CS101"
    assert structured.value("credits") == "3"


def test_target_bbox_kind_is_typed_and_rejects_rectangle_impersonation() -> None:
    interval = HeaderColumnProvenance(target_col=0, target_bbox=[10.0, 20.0])
    assert interval.target_bbox_kind == "x_interval"

    rectangle = HeaderColumnProvenance(
        target_col=0, target_bbox=[10.0, 5.0, 20.0, 15.0]
    )
    assert rectangle.target_bbox_kind == "rect"

    with pytest.raises(ValidationError):
        HeaderColumnProvenance(
            target_col=0,
            target_bbox=[10.0, 20.0],
            target_bbox_kind="rect",
        )
    with pytest.raises(ValidationError):
        HeaderColumnProvenance(
            target_col=0,
            target_bbox=[10.0, 5.0, float("nan"), 15.0],
        )
    with pytest.raises(ValidationError):
        HeaderColumnProvenance(target_col=0, source_bbox=[10.0, 20.0])


def _replace_binding(
    row: Article,
    index: int,
    binding: HeaderColumnProvenance,
) -> Article:
    bindings = list(row.table_column_bindings)
    bindings[index] = binding
    return row.model_copy(update={"table_column_bindings": bindings})


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"source_table_id": "foreign-doc@v1#table1"},
            "BINDING_SOURCE_SCOPE:0",
        ),
        ({"source_col": 2}, "BINDING_SOURCE_COL:0"),
        (
            {"source_bbox": [55.0, 100.0, 155.0, 120.0]},
            "BINDING_SOURCE_BBOX:0",
        ),
        ({"header_text": "伪造标签"}, "BINDING_SOURCE_HEADER_TEXT:0"),
        (
            {"status": "shared", "shared_scope": False},
            "BINDING_SHARED_FLAG_MISMATCH:0",
        ),
        ({"method": "none"}, "BINDING_METHOD_INVALID:0"),
    ],
)
def test_runtime_rejects_forged_binding_fields(
    updates: dict[str, object],
    expected: str,
) -> None:
    _, row = _local_articles()
    forged = _replace_binding(
        row,
        0,
        row.table_column_bindings[0].model_copy(update=updates),
    )

    structured = structured_table_row(forged)
    assert structured is not None and not structured.reliable
    assert expected in structured.problems


@pytest.mark.parametrize(
    ("cell_updates", "expected"),
    [
        ({"text": "伪造标签"}, "BINDING_SOURCE_LABELS:0"),
        (
            {"bbox": [55.0, 100.0, 155.0, 120.0]},
            "BINDING_SOURCE_BBOX:0",
        ),
        (
            {"cell_id": f"{SCOPE}#table9#r0c0"},
            "BINDING_SOURCE_CELLS:0",
        ),
        ({"is_header": False}, "BINDING_SOURCE_CELL_ORIGIN:0"),
    ],
)
def test_runtime_rejects_forged_source_header_cells(
    cell_updates: dict[str, object],
    expected: str,
) -> None:
    _, row = _local_articles()
    binding = row.table_column_bindings[0]
    forged_cell = binding.source_header_cells[0].model_copy(update=cell_updates)
    forged_binding = binding.model_copy(
        update={"source_header_cells": [forged_cell]}
    )
    forged = _replace_binding(row, 0, forged_binding)

    structured = structured_table_row(forged)
    assert structured is not None and not structured.reliable
    assert expected in structured.problems


def test_runtime_rejects_non_finite_target_bbox() -> None:
    _, row = _local_articles()
    binding = row.table_column_bindings[0]
    forged_binding = binding.model_copy(
        update={"target_bbox": [float("nan"), 100.0, 150.0, 120.0]}
    )
    forged = _replace_binding(row, 0, forged_binding)

    structured = structured_table_row(forged)
    assert structured is not None and not structured.reliable
    assert "BINDING_TARGET_BBOX_INVALID:0" in structured.problems


def test_runtime_rejects_two_value_bbox_claimed_as_rectangle() -> None:
    _, row = _local_articles()
    binding = row.table_column_bindings[0].model_copy(
        update={"target_bbox": [50.0, 150.0], "target_bbox_kind": "rect"}
    )
    forged = _replace_binding(row, 0, binding)

    structured = structured_table_row(forged)
    assert structured is not None and not structured.reliable
    assert "BINDING_TARGET_BBOX_KIND:0" in structured.problems


def test_runtime_rejects_binding_count_and_slot_mismatch() -> None:
    _, row = _local_articles()

    missing = row.model_copy(
        update={"table_column_bindings": list(row.table_column_bindings[:2])}
    )
    structured = structured_table_row(missing)
    assert structured is not None and not structured.reliable
    assert "BINDING_COUNT_MISMATCH" in structured.problems

    duplicate = list(row.table_column_bindings)
    duplicate[2] = duplicate[2].model_copy(update={"target_col": 0})
    duplicated = row.model_copy(update={"table_column_bindings": duplicate})
    structured = structured_table_row(duplicated)
    assert structured is not None and not structured.reliable
    assert "BINDING_TARGET_SLOTS_INVALID" in structured.problems


def test_runtime_rejects_unbound_binding_that_keeps_source_evidence() -> None:
    _, row = _local_articles()
    binding = row.table_column_bindings[0].model_copy(
        update={"status": "ambiguous", "method": "none"}
    )
    forged = _replace_binding(row, 0, binding)

    structured = structured_table_row(forged)
    assert structured is not None and not structured.reliable
    assert "BINDING_UNBOUND_SOURCE_PRESENT:0" in structured.problems
    assert structured.columns[0].field == "other"


def test_parent_child_binding_mismatch_fails_preflight() -> None:
    parent, row = _local_articles()

    cleared = row.model_copy(update={"table_column_bindings": []})
    with pytest.raises(VectorIndexError) as error:
        _validate_parent_links([cleared], [parent])
    assert "table_column_bindings" in str(error.value)

    bindings = list(row.table_column_bindings)
    bindings[0] = bindings[0].model_copy(update={"header_text": "伪造标签"})
    altered = row.model_copy(update={"table_column_bindings": bindings})
    with pytest.raises(VectorIndexError) as error:
        _validate_parent_links([altered], [parent])
    assert "table_column_bindings" in str(error.value)
