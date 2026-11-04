"""跨页课程续表：表头继承、数据保留与边界阻断（合成数据）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from campusguide.ingest.parse import _pdf_continuation_blocked
from campusguide.ingest.structure import (
    SourceLocator,
    StructureBlock,
    TableCell,
    TableRow,
    TableStructure,
    _course_like_values,
    _pdf_header_indices,
    extract_pdf_table,
    restore_continuation_header,
)

_SOFTWARE_COLUMNS = [
    (66.94, 124.08),
    (124.08, 197.76),
    (197.76, 291.84),
    (291.84, 317.76),
    (317.76, 354.00),
    (354.00, 394.80),
    (394.80, 510.50),
]
_HEADERS = ["课程类别", "课程编号", "课程中文名称", "学分", "开课学期", "课程性质", "备注"]
_PATHS = [[header] for header in _HEADERS]


def _cell(
    row: int,
    col: int,
    text: str,
    *,
    columns: list[tuple[float, float]],
    y0: float,
    y1: float,
    is_header: bool = False,
) -> TableCell:
    x0, x1 = columns[col]
    return TableCell(
        cell_id=f"table#r{row}c{col}",
        row=row,
        col=col,
        origin_row=row,
        origin_col=col,
        text=text,
        native_text=text or None,
        text_source="native" if text else None,
        is_header=is_header,
        bbox=[x0, y0, x1, y1],
    )


def _row(
    table_id: str,
    index: int,
    values: list[str],
    *,
    columns: list[tuple[float, float]],
    page: int,
    y0: float,
    y1: float,
    is_header: bool = False,
) -> TableRow:
    cells = [
        _cell(index, col, value, columns=columns, y0=y0, y1=y1, is_header=is_header)
        for col, value in enumerate(values)
    ]
    return TableRow(
        row_id=f"{table_id}#row{index}",
        row_index=index,
        is_header=is_header,
        cells=cells,
        text=" | ".join(values) if any(values) else "",
        locator=SourceLocator(format="pdf", page=page),
    )


def _course_values(code: str) -> list[str]:
    return ["学位课", code, "测试课程", "2", "秋", "必修", ""]


def _table(
    table_id: str,
    page: int,
    *,
    with_header: bool,
    codes: tuple[str, ...],
    columns: list[tuple[float, float]],
    bbox: list[float],
    header_source: tuple[str, int] | None = None,
) -> TableStructure:
    rows: list[TableRow] = []
    offset = 0
    if with_header:
        rows.append(
            _row(
                table_id,
                0,
                _HEADERS,
                columns=columns,
                page=page,
                y0=bbox[1],
                y1=bbox[1] + 25.0,
                is_header=True,
            )
        )
        offset = 1
    for position, code in enumerate(codes, start=offset):
        y0 = bbox[1] + 25.0 * (position + 1)
        rows.append(
            _row(
                table_id,
                position,
                _course_values(code)[: len(columns)],
                columns=columns,
                page=page,
                y0=y0,
                y1=y0 + 25.0,
            )
        )
    column_count = len(columns)
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=1,
        page=page,
        bbox=bbox,
        rows=rows,
        header_row_indices=[0] if with_header else [],
        column_count=column_count,
        row_count=len(rows),
        column_headers=list(_HEADERS[:column_count]) if with_header else [],
        header_paths=(
            [list(path) for path in _PATHS[:column_count]] if with_header else []
        ),
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=page, bbox=bbox),
        header_source_table_id=header_source[0] if header_source else None,
        header_source_page=header_source[1] if header_source else None,
    )


def _block(
    text: str, *, page: int, y0: float, y1: float
) -> StructureBlock:
    return StructureBlock(
        block_id=f"block-{page}-{y0}",
        order=1,
        kind="paragraph",
        text=text,
        locator=SourceLocator(
            format="pdf", page=page, bbox=[66.94, y0, 500.0, y1]
        ),
    )


def test_adjacent_page_inherits_real_header_and_keeps_rows_data() -> None:
    previous = _table(
        "doc#table2",
        8,
        with_header=True,
        codes=("06031095131102", "12031095131110"),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 600.0, 510.50, 790.0],
        header_source=("doc#table1", 7),
    )
    current = _table(
        "doc#table3",
        9,
        with_header=False,
        codes=("03031095131092", "06031095131103"),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 72.0, 510.50, 300.0],
    )

    assert restore_continuation_header(previous, current)

    assert current.header_row_indices == []
    assert not any(row.is_header for row in current.rows)
    assert current.column_headers == _HEADERS
    assert current.header_paths == _PATHS
    assert current.continuation_of == "doc#table2"
    assert current.header_source_table_id == "doc#table1"
    assert current.header_source_page == 7
    assert all(row.text.split(" | ")[0] == "学位课" for row in current.rows)


def test_no_reliable_previous_header_means_no_guessing() -> None:
    previous = _table(
        "doc#table2",
        8,
        with_header=False,
        codes=("06031095131102",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 600.0, 510.50, 790.0],
    )
    current = _table(
        "doc#table3",
        9,
        with_header=False,
        codes=("03031095131092",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 72.0, 510.50, 300.0],
    )

    assert not restore_continuation_header(previous, current)
    assert current.column_headers == []
    assert current.header_paths == []
    assert current.header_row_indices == []
    assert current.continuation_of is None
    assert current.header_source_table_id is None


def test_column_shift_or_page_gap_blocks_inheritance() -> None:
    previous = _table(
        "doc#table2",
        8,
        with_header=True,
        codes=("06031095131102",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 600.0, 510.50, 790.0],
    )
    shifted = [
        (x0 + 6.0, x1 + 6.0) for x0, x1 in _SOFTWARE_COLUMNS
    ]
    current = _table(
        "doc#table3",
        9,
        with_header=False,
        codes=("03031095131092",),
        columns=shifted,
        bbox=[72.94, 72.0, 516.50, 300.0],
    )
    assert not restore_continuation_header(previous, current)
    assert current.column_headers == []

    later_page = _table(
        "doc#table4",
        10,
        with_header=False,
        codes=("03031095131092",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 72.0, 510.50, 300.0],
    )
    assert not restore_continuation_header(previous, later_page)
    assert later_page.column_headers == []


def test_notes_heading_or_class_boundary_blocks_continuation() -> None:
    previous = _table(
        "doc#table2",
        8,
        with_header=True,
        codes=("06031095131102",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 600.0, 510.50, 790.0],
    )
    current = _table(
        "doc#table3",
        9,
        with_header=False,
        codes=("03031095131092",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 72.0, 510.50, 300.0],
    )
    note = _block("注：本表课程为选修课。", page=8, y0=800.0, y1=812.0)
    heading = _block("三、培养环节及时间安排", page=9, y0=40.0, y1=55.0)
    class_boundary = _block(
        "2025级软件工程专业型硕士培养方案", page=9, y0=40.0, y1=55.0
    )
    neutral = _block("课程信息见下表", page=9, y0=40.0, y1=55.0)
    far_away = _block("注：这是表格上方的注记。", page=8, y0=100.0, y1=115.0)

    assert _pdf_continuation_blocked(previous, current, [note], [])
    assert _pdf_continuation_blocked(previous, current, [], [heading])
    assert _pdf_continuation_blocked(previous, current, [], [class_boundary])
    assert not _pdf_continuation_blocked(previous, current, [], [neutral])
    assert not _pdf_continuation_blocked(previous, current, [far_away], [])


def test_student_ids_amounts_and_cultivation_rows_are_not_courses() -> None:
    assert not _course_like_values(
        ["20231234567890", "张三", "3000元", "秋", "必修", "", ""]
    )
    assert not _course_like_values(
        ["1 制定培养计划", "入学 2 周内", "-", "", "", "", ""]
    )

    previous = _table(
        "doc#table2",
        7,
        with_header=True,
        codes=("14021081200001",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 600.0, 510.50, 790.0],
    )
    current = TableStructure(
        table_id="doc#table3",
        block_id="doc#table3#block",
        table_index=2,
        page=8,
        bbox=[66.94, 72.0, 510.50, 300.0],
        rows=[
            _row(
                "doc#table3",
                0,
                ["20231234567890", "张三", "3000元", "秋", "必修", "", ""],
                columns=_SOFTWARE_COLUMNS,
                page=8,
                y0=72.0,
                y1=97.0,
            )
        ],
        header_row_indices=[],
        column_count=7,
        row_count=1,
        source="pymupdf",
        locator=SourceLocator(
            format="pdf", page=8, bbox=[66.94, 72.0, 510.50, 300.0]
        ),
    )
    assert not restore_continuation_header(previous, current)
    assert current.column_headers == []


def test_cultivation_table_below_course_table_does_not_inherit() -> None:
    previous = _table(
        "doc#table3",
        7,
        with_header=True,
        codes=("14021081200001",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 72.0, 394.80, 447.57],
    )
    current = TableStructure(
        table_id="doc#table4",
        block_id="doc#table4#block",
        table_index=4,
        page=7,
        bbox=[66.94, 582.44, 481.22, 738.93],
        rows=[
            _row(
                "doc#table4",
                0,
                ["培养环节", "时间安排", "学分", "备注", "", "", ""],
                columns=_SOFTWARE_COLUMNS,
                page=7,
                y0=582.44,
                y1=600.0,
            ),
            _row(
                "doc#table4",
                1,
                ["1 制定培养计划", "入学 2 周内", "-", "", "", "", ""],
                columns=_SOFTWARE_COLUMNS,
                page=7,
                y0=600.0,
                y1=620.0,
            ),
        ],
        header_row_indices=[0],
        column_count=7,
        row_count=2,
        column_headers=["培养环节", "时间安排", "学分", "备注", "", "", ""],
        header_paths=[
            ["培养环节"],
            ["时间安排"],
            ["学分"],
            ["备注"],
            [],
            [],
            [],
        ],
        source="pymupdf",
        locator=SourceLocator(
            format="pdf", page=7, bbox=[66.94, 582.44, 481.22, 738.93]
        ),
    )
    assert not restore_continuation_header(previous, current)
    assert current.column_headers == ["培养环节", "时间安排", "学分", "备注", "", "", ""]


def test_next_page_headerless_cultivation_rows_do_not_inherit() -> None:
    previous = _table(
        "doc#table2",
        7,
        with_header=True,
        codes=("14021081200001",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 600.0, 510.50, 790.0],
    )
    current = TableStructure(
        table_id="doc#table4",
        block_id="doc#table4#block",
        table_index=4,
        page=8,
        bbox=[66.94, 72.0, 510.50, 228.44],
        rows=[
            _row(
                "doc#table4",
                0,
                ["1 制定培养计划", "入学 2 周内", "-", "", "", "", ""],
                columns=_SOFTWARE_COLUMNS,
                page=8,
                y0=72.0,
                y1=97.0,
            ),
            _row(
                "doc#table4",
                1,
                ["2 开题报告", "第 3 学期", "2", "", "", "", ""],
                columns=_SOFTWARE_COLUMNS,
                page=8,
                y0=97.0,
                y1=122.0,
            ),
        ],
        header_row_indices=[],
        column_count=7,
        row_count=2,
        source="pymupdf",
        locator=SourceLocator(
            format="pdf", page=8, bbox=[66.94, 72.0, 510.50, 228.44]
        ),
    )

    assert not restore_continuation_header(previous, current)
    assert current.column_headers == []
    assert current.header_paths == []
    assert current.continuation_of is None


def test_trailing_empty_remark_column_is_recovered_only_by_geometry() -> None:
    previous = _table(
        "doc#table2",
        6,
        with_header=True,
        codes=("14021081200001",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 435.84, 510.50, 748.35],
    )
    current = _table(
        "doc#table3",
        7,
        with_header=False,
        codes=("14022081200005", "14022081200007"),
        columns=_SOFTWARE_COLUMNS[:6],
        bbox=[66.945, 72.21, 394.80, 456.96],
    )
    # 真实软件表上一页的备注列有内容，恢复只能凭几何边界而不是“原列为空”。
    previous.rows[1].cells[-1].text = "研究生院开课"

    assert restore_continuation_header(previous, current)
    assert current.column_count == 7
    assert current.column_headers[-1] == "备注"
    assert current.header_paths[-1] == ["备注"]
    assert all(len(row.cells) == 7 for row in current.rows)
    assert all(row.cells[-1].text == "" for row in current.rows)
    assert not any(
        cell.is_header for row in current.rows for cell in row.cells
    )

    shifted_edge = _table(
        "doc#table4",
        7,
        with_header=False,
        codes=("14022081200005",),
        columns=_SOFTWARE_COLUMNS[:6],
        bbox=[66.945, 72.21, 390.0, 456.96],
    )
    assert not restore_continuation_header(previous, shifted_edge)
    assert shifted_edge.column_count == 6
    assert shifted_edge.column_headers == []


def test_existing_header_is_never_overwritten() -> None:
    previous = _table(
        "doc#table2",
        6,
        with_header=True,
        codes=("14021081200001",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.94, 435.84, 510.50, 748.35],
    )
    current = _table(
        "doc#table3",
        7,
        with_header=True,
        codes=("14022081200005",),
        columns=_SOFTWARE_COLUMNS,
        bbox=[66.945, 72.21, 510.50, 456.96],
    )
    original_headers = list(current.column_headers)
    original_paths = [list(path) for path in current.header_paths]

    assert not restore_continuation_header(previous, current)
    assert current.column_headers == original_headers
    assert current.header_paths == original_paths
    assert current.header_row_indices == [0]
    assert current.continuation_of is None


class _Header:
    def __init__(
        self,
        names: list[str],
        bbox: list[float] | None,
        external: bool = False,
    ) -> None:
        self.names = names
        self.bbox = bbox
        self.external = external


class _RawTable:
    def __init__(
        self,
        rows: list[list[str]],
        *,
        names: list[str],
        header_bbox: list[float] | None,
        header_rows: int = 0,
    ) -> None:
        self.header_rows = header_rows
        self.header = _Header(names, header_bbox)
        self.rows = [
            SimpleNamespace(
                cells=[None for _ in row],
                bbox=[0.0, float(index * 25), 500.0, float(index * 25 + 25)],
            )
            for index, row in enumerate(rows)
        ]


def _header_indices(
    rows: list[list[str]],
    *,
    names: list[str],
    header_bbox: list[float] | None = None,
    header_rows: int = 0,
) -> tuple[list[int], list[str], int | None]:
    table = _RawTable(
        rows, names=names, header_bbox=header_bbox, header_rows=header_rows
    )
    raw_rows: list[list[Any]] = [[None for _ in row] for row in rows]
    row_tops = [float(index * 25) for index in range(len(rows))]
    return _pdf_header_indices(table, raw_rows, rows, row_tops)


def test_native_header_names_that_are_data_do_not_swallow_rows() -> None:
    rows = [
        _course_values("06031095131102"),
        _course_values("12031095131110"),
    ]
    indices, notes, fragment = _header_indices(
        rows, names=rows[0], header_bbox=[0.0, 0.0, 500.0, 300.0]
    )
    assert indices == []
    assert fragment is None
    assert any("数据" in note for note in notes)


def test_fragment_row_is_neither_header_nor_fake_course() -> None:
    rows = [
        ["", "", "例（作物与种业）", "", "", "三选\n一", ""],
        ["", "06031095131102", "测试课程", "2", "秋", "必修", ""],
    ]
    indices, notes, fragment = _header_indices(
        rows, names=rows[0], header_bbox=[0.0, 0.0, 500.0, 300.0]
    )
    assert indices == []
    assert fragment == 0
    assert any("碎片" in note for note in notes)
    assert not _course_like_values(rows[0])
    assert rows[0][2] == "例（作物与种业）"


class _PdfTable:
    """只实现 extract_pdf_table 读取的公开字段。"""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self.header_rows = 0
        self.header = _Header(rows[0], [0.0, 0.0, 510.5, 300.0])
        self.col_count = len(rows[0])
        self.bbox = [66.94, 0.0, 510.5, 300.0]
        self.rows = []
        for index, values in enumerate(rows):
            y0 = float(index * 25)
            self.rows.append(
                SimpleNamespace(
                    cells=[
                        (
                            SimpleNamespace(
                                bbox=[
                                    _SOFTWARE_COLUMNS[col][0],
                                    y0,
                                    _SOFTWARE_COLUMNS[col][1],
                                    y0 + 25.0,
                                ]
                            )
                            if value
                            else None
                        )
                        for col, value in enumerate(values)
                    ],
                    bbox=[66.94, y0, 510.5, y0 + 25.0],
                )
            )

    def extract(self) -> list[list[str]]:
        return [list(row) for row in self._rows]


def test_fragment_row_is_kept_as_reviewable_data_at_extraction() -> None:
    table = _PdfTable(
        [
            ["", "", "例（作物与种业）", "", "", "三选\n一", ""],
            ["", "06031095131102", "测试课程", "2", "秋", "必修", ""],
        ]
    )
    structure, _ = extract_pdf_table(
        table,
        doc_id="doc",
        version_id="v",
        table_index=3,
        page=9,
        block_id="doc#block",
    )

    assert structure.header_row_indices == []
    assert not any(row.is_header for row in structure.rows)
    assert structure.column_headers == [""] * 7
    assert structure.header_paths == [[] for _ in range(7)]
    assert any(cell.needs_review for cell in structure.rows[0].cells)
    assert not _course_like_values(
        [cell.text for cell in structure.rows[0].cells]
    )
    assert structure.rows[1].cells[1].text == "06031095131102"


def test_explicit_course_header_still_stops_before_first_data_row() -> None:
    rows = [
        ["课程编号", "课程中文名称", "学分", "开课学期", "课程性质"],
        ["06031095131102", "测试课程", "2", "秋", "必修"],
        ["12031095131110", "另一门课程", "3", "春", "选修"],
    ]
    indices, notes, fragment = _header_indices(
        rows, names=rows[0], header_bbox=[0.0, 0.0, 500.0, 300.0]
    )
    assert indices == [0]
    assert notes == []
    assert fragment is None
