"""物理列表头绑定：跨页 8→6、部分解析、来源链与表头边界（合成数据）。"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

from bs4 import BeautifulSoup

import campusguide.ingest.structure as structure_module
from campusguide.ingest.structure import (
    HeaderColumnBinding,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
    _effective_column_bindings,
    _local_header_bindings,
    _pdf_header_indices,
    extract_html_table,
    extract_pdf_table,
    restore_continuation_header,
)

Interval = tuple[float, float]


def _cell(
    table_id: str,
    row: int,
    col: int,
    text: str,
    interval: Interval,
    y0: float,
    y1: float,
    *,
    is_header: bool = False,
    rowspan: int = 1,
    colspan: int = 1,
    covered: bool = False,
    origin_row: int | None = None,
    origin_col: int | None = None,
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
        native_text=text or None,
        text_source="native" if text else None,
        is_header=is_header,
        bbox=[interval[0], y0, interval[1], y1],
    )


def _row(
    table_id: str,
    index: int,
    values: list[str],
    intervals: list[Interval],
    *,
    page: int,
    y0: float,
    y1: float,
    is_header: bool = False,
) -> TableRow:
    cells = [
        _cell(
            table_id,
            index,
            col,
            value,
            intervals[col],
            y0,
            y1,
            is_header=is_header,
        )
        for col, value in enumerate(values)
    ]
    return TableRow(
        row_id=f"{table_id}#row{index}",
        row_index=index,
        is_header=is_header,
        cells=cells,
        text=" | ".join(values),
        locator=SourceLocator(format="pdf", page=page),
    )


def _table(
    table_id: str,
    page: int,
    *,
    headers: list[str] | None,
    intervals: list[Interval],
    data_rows: list[list[str]],
    header_paths: list[list[str]] | None = None,
    header_source: tuple[str, int] | None = None,
    bindings: list[HeaderColumnBinding] | None = None,
    bbox: list[float] | None = None,
) -> TableStructure:
    rows: list[TableRow] = []
    offset = 0
    if headers is not None:
        rows.append(
            _row(
                table_id,
                0,
                headers,
                intervals,
                page=page,
                y0=100.0,
                y1=120.0,
                is_header=True,
            )
        )
        offset = 1
    for position, values in enumerate(data_rows, start=offset):
        y0 = 100.0 + 20.0 * (position + 1)
        rows.append(
            _row(
                table_id,
                position,
                values,
                intervals,
                page=page,
                y0=y0,
                y1=y0 + 20.0,
            )
        )
    if bbox is None:
        bbox = [
            intervals[0][0],
            100.0,
            intervals[-1][1],
            100.0 + 20.0 * (len(rows) + 1),
        ]
    if header_paths is not None:
        paths = [list(path) for path in header_paths]
    elif headers is not None:
        paths = [[header] for header in headers]
    else:
        paths = []
    table = TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=1,
        page=page,
        bbox=bbox,
        rows=rows,
        header_row_indices=[0] if headers is not None else [],
        column_count=len(intervals),
        row_count=len(rows),
        column_headers=(
            list(headers) if headers is not None else [""] * len(intervals)
        ),
        header_paths=paths,
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=page, bbox=bbox),
        header_source_table_id=header_source[0] if header_source else None,
        header_source_page=header_source[1] if header_source else None,
    )
    if bindings is not None:
        table.column_bindings = bindings
    return table


_SOURCE8: list[Interval] = [
    (96.50, 151.58),
    (151.58, 155.48),
    (155.48, 232.88),
    (232.88, 326.20),
    (326.20, 371.32),
    (371.32, 412.74),
    (412.74, 455.94),
    (455.94, 497.34),
]
_TARGET6: list[Interval] = [
    (90.24, 155.48),
    (155.48, 232.88),
    (232.88, 326.20),
    (326.20, 371.32),
    (371.32, 412.74),
    (412.74, 454.08),
]
_SOURCE_HEADERS = [
    "类别",
    "",
    "课程编号",
    "课程名称",
    "学分",
    "开课学期",
    "课程性质",
    "备注",
]
_SOURCE_PATHS = [
    ["类别"],
    [],
    ["课程编号"],
    ["课程名称"],
    ["学分"],
    ["开课学期"],
    ["课程性质"],
    ["备注"],
]


def test_eight_to_six_physical_mapping_binds_only_exact_columns() -> None:
    previous = _table(
        "plan@v2025#table2",
        8,
        headers=_SOURCE_HEADERS,
        header_paths=_SOURCE_PATHS,
        intervals=_SOURCE8,
        data_rows=[
            [
                "学位课",
                "",
                "00000000000001",
                "测试课程甲",
                "3",
                "秋",
                "必修",
                "",
            ]
        ],
    )
    current = _table(
        "plan@v2025#table3",
        9,
        headers=None,
        intervals=_TARGET6,
        data_rows=[
            ["学位课", "00000000000001", "测试课程甲", "3", "秋", "必修"]
        ],
    )

    assert restore_continuation_header(previous, current)

    assert [binding.status for binding in current.column_bindings] == [
        "unresolved",
        "bound",
        "bound",
        "bound",
        "bound",
        "offset",
    ]
    assert current.column_headers == [
        "",
        "课程编号",
        "课程名称",
        "学分",
        "开课学期",
        "",
    ]
    assert current.header_paths == [
        [],
        ["课程编号"],
        ["课程名称"],
        ["学分"],
        ["开课学期"],
        [],
    ]
    assert current.continuation_of == "plan@v2025#table2"
    assert current.header_source_table_id == "plan@v2025#table2"
    assert current.header_source_page == 8

    code = current.column_bindings[1]
    assert code.method == "physical_interval"
    assert code.source_table_id == "plan@v2025#table2"
    assert code.source_via_table_id is None
    assert code.source_page == 8
    assert code.source_col == 2
    assert code.source_header_cell_ids == ["plan@v2025#table2#r0c2"]
    assert code.source_header_row_indices == [0]
    assert code.source_bbox == [155.48, 100.0, 232.88, 120.0]
    assert code.target_bbox == [155.48, 120.0, 232.88, 140.0]
    assert code.header_text == "课程编号"
    assert code.header_path == ["课程编号"]

    category = current.column_bindings[0]
    assert category.status == "unresolved"
    assert category.method == "none"
    assert category.header_text == ""
    assert category.source_header_cell_ids == []
    assert category.reason

    nature = current.column_bindings[5]
    assert nature.status == "offset"
    assert nature.method == "none"
    assert nature.header_text == ""
    assert "1.86pt" in nature.reason

    assert "类别" not in current.column_headers
    assert "备注" not in current.column_headers
    assert current.column_count == 6
    assert [cell.text for cell in current.rows[0].cells] == [
        "学位课",
        "00000000000001",
        "测试课程甲",
        "3",
        "秋",
        "必修",
    ]
    assert any("4/6 列" in warning for warning in current.warnings)
    assert any(
        "c0(unresolved" in warning and "c5(offset" in warning
        for warning in current.warnings
    )
    for binding in current.column_bindings:
        assert binding.target_bbox is None or (
            len(binding.target_bbox) == 4
            and all(math.isfinite(value) for value in binding.target_bbox)
        )
        assert binding.source_bbox is None or (
            len(binding.source_bbox) == 4
            and all(math.isfinite(value) for value in binding.source_bbox)
        )


def test_partial_mapping_flags_ambiguous_and_unresolved_columns() -> None:
    previous = _table(
        "plan@v2025#table5",
        8,
        headers=["课程编号", "课程名称", "学分", "开课学期", "课程性质"],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (340.0, 380.0),
        ],
        data_rows=[["00000000000002", "测试课程乙", "2", "秋", "必修"]],
    )
    current = _table(
        "plan@v2025#table6",
        9,
        headers=None,
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (340.0, 380.0),
            (500.0, 540.0),
        ],
        data_rows=[["00000000000003", "测试课程丙", "2", "春"]],
    )

    assert restore_continuation_header(previous, current)

    assert [binding.status for binding in current.column_bindings] == [
        "bound",
        "bound",
        "ambiguous",
        "unresolved",
    ]
    assert current.column_headers == ["课程编号", "课程名称", "", ""]
    assert current.header_paths == [["课程编号"], ["课程名称"], [], []]
    ambiguous = current.column_bindings[2]
    assert ambiguous.method == "none"
    assert ambiguous.header_text == ""
    assert "2 个来源表头列" in ambiguous.reason
    unresolved = current.column_bindings[3]
    assert unresolved.status == "unresolved"
    assert unresolved.header_text == ""
    assert unresolved.reason
    assert current.column_count == 4
    assert any(
        "c2(ambiguous" in warning for warning in current.warnings
    )
    assert any(
        "c3(unresolved" in warning for warning in current.warnings
    )


def test_partial_continuation_requires_mapped_course_identity_columns() -> None:
    previous = _table(
        "plan@v2025#table22",
        8,
        headers=["课程编号", "课程名称", "学分", "开课学期", "课程性质"],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
        ],
        data_rows=[["00000000000022", "测试课程子", "2", "秋", "必修"]],
    )
    current = _table(
        "plan@v2025#table23",
        9,
        headers=None,
        intervals=[
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
            (500.0, 540.0),
        ],
        # 只有学分/学期/性质列物理匹配；代码列落在未匹配的新列。
        data_rows=[["2", "秋", "必修", "00000000000023"]],
    )

    assert not restore_continuation_header(previous, current)
    assert current.column_headers == [""] * 4
    assert current.header_paths == []
    assert current.column_bindings == []
    assert current.continuation_of is None
    assert current.header_source_table_id is None


def test_partial_continuation_rejects_incompatible_identity_values() -> None:
    previous = _table(
        "plan@v2025#table24",
        8,
        headers=["课程编号", "课程名称", "学分", "开课学期", "课程性质"],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
        ],
        data_rows=[["00000000000024", "测试课程丑", "2", "秋", "必修"]],
    )
    current = _table(
        "plan@v2025#table25",
        9,
        headers=None,
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
        ],
        # 代码/名称列位只有数字，课程代码落在学分列，不能据此继承表头。
        data_rows=[["3", "2", "00000000000025", "秋", "必修"]],
    )

    assert not restore_continuation_header(previous, current)
    assert current.column_headers == [""] * 5
    assert current.header_paths == []
    assert current.column_bindings == []
    assert current.continuation_of is None


def test_same_count_shifted_geometry_is_rejected() -> None:
    previous = _table(
        "plan@v2025#table7",
        8,
        headers=["课程编号", "课程名称", "学分", "开课学期", "课程性质"],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
        ],
        data_rows=[["00000000000004", "测试课程丁", "2", "秋", "必修"]],
    )
    current = _table(
        "plan@v2025#table8",
        9,
        headers=None,
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 418.20),
        ],
        data_rows=[["00000000000005", "测试课程戊", "2", "春", "必修"]],
    )

    assert not restore_continuation_header(previous, current)
    assert current.column_headers == [""] * 5
    assert current.header_paths == []
    assert current.column_bindings == []
    assert current.continuation_of is None
    assert current.header_source_table_id is None
    assert [cell.text for cell in current.rows[0].cells] == [
        "00000000000005",
        "测试课程戊",
        "2",
        "春",
        "必修",
    ]


def test_shorter_prefix_without_geometric_recovery_is_rejected() -> None:
    previous = _table(
        "plan@v2025#table20",
        8,
        headers=[
            "课程编号",
            "课程名称",
            "学分",
            "开课学期",
            "课程性质",
            "备注",
        ],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
            (420.0, 460.0),
        ],
        data_rows=[
            ["00000000000020", "测试课程甲", "2", "秋", "必修", ""]
        ],
    )
    current = _table(
        "plan@v2025#table21",
        9,
        headers=None,
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
            (380.0, 420.0),
        ],
        data_rows=[["00000000000021", "测试课程乙", "2", "春", "必修"]],
        # 右边界与来源表第 6 列左边界相差 5pt，不足以证明尾部空列被忽略。
        bbox=[100.0, 100.0, 415.0, 240.0],
    )

    assert not restore_continuation_header(previous, current)
    assert current.column_count == 5
    assert current.column_headers == [""] * 5
    assert current.header_paths == []
    assert current.column_bindings == []
    assert current.continuation_of is None
    assert all(len(row.cells) == 5 for row in current.rows)
    assert [cell.text for cell in current.rows[0].cells] == [
        "00000000000021",
        "测试课程乙",
        "2",
        "春",
        "必修",
    ]


def test_header_labels_follow_physical_intervals_not_index_order() -> None:
    previous = _table(
        "plan@v2025#table9",
        8,
        headers=["课程名称", "课程编号", "学分", "开课学期"],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
        ],
        data_rows=[["测试课程甲", "00000000000006", "2", "秋"]],
    )
    current = _table(
        "plan@v2025#table10",
        9,
        headers=None,
        intervals=[
            (200.0, 300.0),
            (100.0, 200.0),
            (300.0, 340.0),
            (340.0, 380.0),
        ],
        data_rows=[["00000000000007", "测试课程乙", "2", "春"]],
    )

    assert restore_continuation_header(previous, current)

    assert current.column_headers == [
        "课程编号",
        "课程名称",
        "学分",
        "开课学期",
    ]
    assert current.column_bindings[0].source_header_cell_ids == [
        "plan@v2025#table9#r0c1"
    ]
    assert current.column_bindings[0].source_col == 1
    assert current.column_bindings[1].source_header_cell_ids == [
        "plan@v2025#table9#r0c0"
    ]
    assert current.column_bindings[1].source_col == 0


def test_numeric_cell_values_do_not_override_physical_labels() -> None:
    previous = _table(
        "plan@v2025#table11",
        8,
        headers=["课程编号", "课程名称", "学分", "开课学期"],
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (300.0, 340.0),
            (340.0, 380.0),
        ],
        data_rows=[["00000000000008", "测试课程丙", "2", "秋"]],
    )
    current = _table(
        "plan@v2025#table12",
        9,
        headers=None,
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (340.0, 380.0),
            (300.0, 340.0),
        ],
        data_rows=[["00000000000009", "测试课程丁", "2", "秋"]],
    )

    assert restore_continuation_header(previous, current)

    # 数值“2”落在开课学期区间，不能被当成学分列；标签只看物理区间。
    assert current.column_headers == [
        "课程编号",
        "课程名称",
        "开课学期",
        "学分",
    ]
    assert current.rows[0].cells[2].text == "2"
    assert current.rows[0].cells[3].text == "秋"


def test_document_version_and_origin_identity_mismatch_blocks() -> None:
    headers = ["课程编号", "课程名称", "学分", "开课学期"]
    intervals = [
        (100.0, 200.0),
        (200.0, 300.0),
        (300.0, 340.0),
        (340.0, 380.0),
    ]
    data_row = ["00000000000010", "测试课程戊", "2", "秋"]
    previous = _table(
        "plan@v2025#table13",
        8,
        headers=headers,
        intervals=intervals,
        data_rows=[data_row],
    )

    other_version = _table(
        "plan@v2026#table14",
        9,
        headers=None,
        intervals=intervals,
        data_rows=[["00000000000011", "测试课程己", "2", "春"]],
    )
    assert not restore_continuation_header(previous, other_version)
    assert other_version.column_headers == [""] * 4
    assert other_version.column_bindings == []

    other_doc = _table(
        "other@v2025#table15",
        9,
        headers=None,
        intervals=intervals,
        data_rows=[["00000000000012", "测试课程庚", "2", "春"]],
    )
    assert not restore_continuation_header(previous, other_doc)
    assert other_doc.column_headers == [""] * 4

    previous.header_source_table_id = "other@v2025#table1"
    previous.header_source_page = 7
    same_doc = _table(
        "plan@v2025#table16",
        9,
        headers=None,
        intervals=intervals,
        data_rows=[["00000000000013", "测试课程辛", "2", "春"]],
    )
    assert not restore_continuation_header(previous, same_doc)
    assert same_doc.column_headers == [""] * 4
    assert same_doc.continuation_of is None


def test_chained_provenance_keeps_trusted_origin_and_via() -> None:
    labels = ["课程编号", "课程名称", "学分", "开课学期"]
    intervals = [
        (100.0, 200.0),
        (200.0, 300.0),
        (300.0, 340.0),
        (340.0, 380.0),
    ]
    bindings = [
        HeaderColumnBinding(
            target_col=col,
            status="bound",
            method="physical_interval",
            source_table_id="plan@v2025#table1",
            source_page=7,
            source_col=col,
            source_header_cell_ids=[f"plan@v2025#table1#r0c{col}"],
            source_header_row_indices=[0],
            source_bbox=[interval[0], 50.0, interval[1], 70.0],
            target_bbox=[interval[0], 100.0, interval[1], 120.0],
            header_text=labels[col],
            header_path=[labels[col]],
        )
        for col, interval in enumerate(intervals)
    ]
    previous = _table(
        "plan@v2025#table2",
        8,
        headers=None,
        intervals=intervals,
        data_rows=[["00000000000014", "测试课程壬", "2", "秋"]],
        header_source=("plan@v2025#table1", 7),
        bindings=bindings,
    )
    previous.column_headers = list(labels)
    previous.header_paths = [[label] for label in labels]
    current = _table(
        "plan@v2025#table3",
        9,
        headers=None,
        intervals=intervals,
        data_rows=[["00000000000015", "测试课程癸", "2", "春"]],
    )

    assert restore_continuation_header(previous, current)

    assert current.continuation_of == "plan@v2025#table2"
    assert current.header_source_table_id == "plan@v2025#table1"
    assert current.header_source_page == 7
    assert current.column_headers == labels
    for binding in current.column_bindings:
        assert binding.status == "bound"
        assert binding.source_table_id == "plan@v2025#table1"
        assert binding.source_via_table_id == "plan@v2025#table2"
        assert binding.source_page == 7
        assert binding.source_col == binding.target_col
        assert binding.source_header_cell_ids == [
            f"plan@v2025#table1#r0c{binding.target_col}"
        ]


def _raw_table(rows: list[list[Any]], names: list[str]) -> Any:
    return SimpleNamespace(
        header_rows=0,
        header=SimpleNamespace(names=names, bbox=None, external=False),
        rows=[
            SimpleNamespace(
                cells=row,
                bbox=[0.0, index * 25.0, 500.0, index * 25.0 + 25.0],
            )
            for index, row in enumerate(rows)
        ],
    )


def test_first_numbered_plan_row_is_not_promoted_to_header() -> None:
    extracted = [
        ["培养环节", "时间安排", None, "学\n分", "备注"],
        ["1.制定培养计划", "入学 2 周内", None, "-", ""],
        ["2.开题报告", "第 3 学期", None, "2", ""],
    ]
    raw_rows: list[list[Any]] = [
        [
            [100.0, 0.0, 200.0, 25.0],
            [200.0, 0.0, 400.0, 25.0],
            None,
            [400.0, 0.0, 430.0, 25.0],
            [430.0, 0.0, 500.0, 25.0],
        ],
        [
            [100.0, 25.0, 200.0, 50.0],
            [200.0, 25.0, 400.0, 50.0],
            None,
            [400.0, 25.0, 430.0, 50.0],
            [430.0, 25.0, 500.0, 50.0],
        ],
        [
            [100.0, 50.0, 200.0, 75.0],
            [200.0, 50.0, 300.0, 75.0],
            [300.0, 50.0, 400.0, 75.0],
            [400.0, 50.0, 430.0, 75.0],
            [430.0, 50.0, 500.0, 75.0],
        ],
    ]
    table = _raw_table(
        raw_rows,
        ["培养环节", "时间安排", "", "学\n分", "备注"],
    )

    indices, notes, fragment = _pdf_header_indices(
        table, raw_rows, extracted, [0.0, 25.0, 50.0]
    )

    assert indices == [0]
    assert fragment is None
    assert any("次行保留为数据" in note for note in notes)


def test_numbered_stage_and_relative_times_are_not_subheader_evidence() -> None:
    extracted = [
        ["培养环节", "时间安排", "", "学分"],
        ["1.制定培养计划", "第三学期", "第四学期", "2"],
        ["2.开题报告", "第四学期", "第五学期", "2"],
    ]
    raw_rows: list[list[Any]] = [
        [
            [100.0, 0.0, 200.0, 25.0],
            [200.0, 0.0, 400.0, 25.0],
            None,
            [400.0, 0.0, 500.0, 25.0],
        ],
        [
            [100.0, 25.0, 200.0, 50.0],
            [200.0, 25.0, 300.0, 50.0],
            [300.0, 25.0, 400.0, 50.0],
            [400.0, 25.0, 500.0, 50.0],
        ],
        [
            [100.0, 50.0, 200.0, 75.0],
            [200.0, 50.0, 300.0, 75.0],
            [300.0, 50.0, 400.0, 75.0],
            [400.0, 50.0, 500.0, 75.0],
        ],
    ]
    table = _raw_table(
        raw_rows,
        ["培养环节", "时间安排", "", "学分"],
    )

    indices, notes, fragment = _pdf_header_indices(
        table, raw_rows, extracted, [0.0, 25.0, 50.0]
    )

    assert indices == [0]
    assert fragment is None
    assert any("次行保留为数据" in note for note in notes)
    assert not any("已把次行纳入表头" in note for note in notes)


def test_theory_practice_subheaders_remain_header_evidence() -> None:
    extracted = [
        ["课程编号", "课程名称", "总学时", "", "学分"],
        ["", "", "理论", "实践", ""],
        ["00000000000027", "测试课程寅", "48", "16", "3"],
    ]
    raw_rows: list[list[Any]] = [
        [
            [100.0, 0.0, 200.0, 25.0],
            [200.0, 0.0, 300.0, 25.0],
            [300.0, 0.0, 450.0, 25.0],
            None,
            [450.0, 0.0, 500.0, 25.0],
        ],
        [
            [100.0, 25.0, 200.0, 50.0],
            [200.0, 25.0, 300.0, 50.0],
            [300.0, 25.0, 375.0, 50.0],
            [375.0, 25.0, 450.0, 50.0],
            [450.0, 25.0, 500.0, 50.0],
        ],
        [
            [100.0, 50.0, 200.0, 75.0],
            [200.0, 50.0, 300.0, 75.0],
            [300.0, 50.0, 375.0, 75.0],
            [375.0, 50.0, 450.0, 75.0],
            [450.0, 50.0, 500.0, 75.0],
        ],
    ]
    table = _raw_table(
        raw_rows,
        ["课程编号", "课程名称", "总学时", "", "学分"],
    )

    indices, notes, fragment = _pdf_header_indices(
        table, raw_rows, extracted, [0.0, 25.0, 50.0]
    )

    assert indices == [0, 1]
    assert fragment is None
    assert any("独立子表头" in note for note in notes)


def test_independent_subheader_cells_are_kept_as_multirow_header() -> None:
    extracted = [
        ["课程编号", "课程名称", "考核方式", "", "学分"],
        ["", "", "过程考核", "结课考核", ""],
        ["00000000000016", "测试课程子", "80", "20", "2"],
        ["00000000000017", "测试课程丑", "90", "10", "3"],
    ]
    raw_rows: list[list[Any]] = [
        [
            [100.0, 0.0, 200.0, 25.0],
            [200.0, 0.0, 250.0, 25.0],
            [250.0, 0.0, 400.0, 25.0],
            None,
            [400.0, 0.0, 500.0, 25.0],
        ],
        [
            [100.0, 25.0, 200.0, 50.0],
            [200.0, 25.0, 250.0, 50.0],
            [250.0, 25.0, 325.0, 50.0],
            [325.0, 25.0, 400.0, 50.0],
            [400.0, 25.0, 500.0, 50.0],
        ],
        [
            [100.0, 50.0, 200.0, 75.0],
            [200.0, 50.0, 250.0, 75.0],
            [250.0, 50.0, 325.0, 75.0],
            [325.0, 50.0, 400.0, 75.0],
            [400.0, 50.0, 500.0, 75.0],
        ],
        [
            [100.0, 75.0, 200.0, 100.0],
            [200.0, 75.0, 250.0, 100.0],
            [250.0, 75.0, 325.0, 100.0],
            [325.0, 75.0, 400.0, 100.0],
            [400.0, 75.0, 500.0, 100.0],
        ],
    ]
    table = _raw_table(
        raw_rows,
        ["课程编号", "课程名称", "考核方式", "", "学分"],
    )

    indices, notes, fragment = _pdf_header_indices(
        table, raw_rows, extracted, [0.0, 25.0, 50.0, 75.0]
    )

    assert indices == [0, 1]
    assert fragment is None
    assert any("独立子表头" in note for note in notes)


class _NativeTable:
    """只实现 extract_pdf_table 读取的原生 Table 表面。"""

    def __init__(
        self, raw_rows: list[list[Any]], extracted: list[list[str]]
    ) -> None:
        self._extracted = extracted
        self.header_rows = 0
        self.header = SimpleNamespace(
            names=list(extracted[0]), bbox=None, external=False
        )
        self.col_count = len(raw_rows[0])
        self.bbox = [100.0, 0.0, 500.0, 50.0]
        self.rows = [
            SimpleNamespace(
                cells=row,
                bbox=[100.0, index * 25.0, 500.0, index * 25.0 + 25.0],
            )
            for index, row in enumerate(raw_rows)
        ]
        self.placements = None

    def extract(self) -> list[list[str]]:
        return [list(row) for row in self._extracted]


def test_pdf_extraction_populates_bindings_from_origin_cells() -> None:
    raw_rows: list[list[Any]] = [
        [
            [100.0, 0.0, 200.0, 25.0],
            [200.0, 0.0, 400.0, 25.0],
            None,
            [400.0, 0.0, 500.0, 25.0],
        ],
        [
            [100.0, 25.0, 200.0, 50.0],
            [200.0, 25.0, 300.0, 50.0],
            [300.0, 25.0, 400.0, 50.0],
            [400.0, 25.0, 500.0, 50.0],
        ],
    ]
    extracted = [
        ["类别", "时间安排", "", "备注"],
        ["学位课", "00000000000019", "测试课程卯", ""],
    ]
    table = _NativeTable(raw_rows, extracted)

    structure, _rendered = extract_pdf_table(
        table,
        doc_id="plan",
        version_id="v2025",
        table_index=19,
        page=9,
        block_id="plan@v2025#block19",
    )

    assert structure.header_row_indices == [0]
    assert not structure.rows[1].is_header
    assert [binding.status for binding in structure.column_bindings] == [
        "bound",
        "shared",
        "shared",
        "bound",
    ]
    covered = structure.column_bindings[2]
    assert covered.covered is True
    assert covered.source_col == 1
    assert covered.source_header_cell_ids == [
        "plan@v2025#table19#r0c1"
    ]
    assert covered.header_text == "时间安排"
    assert any(
        "2 列跨列共享" in warning for warning in structure.warnings
    )


def test_group_header_covered_leaf_is_shared_without_fake_names() -> None:
    table_id = "plan@v2025#table18"
    header_cells = [
        _cell(
            table_id, 0, 0, "类别", (100.0, 200.0), 0.0, 20.0, is_header=True
        ),
        _cell(
            table_id,
            0,
            1,
            "时间安排",
            (200.0, 400.0),
            0.0,
            20.0,
            is_header=True,
            colspan=2,
        ),
        _cell(
            table_id,
            0,
            2,
            "",
            (200.0, 400.0),
            0.0,
            20.0,
            is_header=True,
            colspan=2,
            covered=True,
            origin_col=1,
        ),
        _cell(
            table_id, 0, 3, "备注", (400.0, 500.0), 0.0, 20.0, is_header=True
        ),
    ]
    table = TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=1,
        page=9,
        bbox=[100.0, 0.0, 500.0, 20.0],
        rows=[
            TableRow(
                row_id=f"{table_id}#row0",
                row_index=0,
                is_header=True,
                cells=header_cells,
                text="类别 | 时间安排 | 时间安排 | 备注",
                locator=SourceLocator(format="pdf", page=9),
            )
        ],
        header_row_indices=[0],
        column_count=4,
        row_count=1,
        column_headers=["类别", "时间安排", "时间安排", "备注"],
        header_paths=[
            ["类别"],
            ["时间安排"],
            ["时间安排"],
            ["备注"],
        ],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=9),
    )

    bindings = _local_header_bindings(table)

    assert [binding.status for binding in bindings] == [
        "bound",
        "shared",
        "shared",
        "bound",
    ]
    group = bindings[1]
    assert group.header_text == "时间安排"
    assert group.shared_scope is True
    assert group.covered is False
    assert group.source_header_cell_ids == [f"{table_id}#r0c1"]

    covered = bindings[2]
    assert covered.header_text == "时间安排"
    assert covered.header_path == ["时间安排"]
    assert covered.covered is True
    assert covered.shared_scope is True
    assert covered.source_col == 1
    assert covered.source_header_cell_ids == [f"{table_id}#r0c1"]
    assert covered.source_bbox == [200.0, 0.0, 400.0, 20.0]
    assert all("全日制" not in binding.header_text for binding in bindings)
    assert all(
        "非全日制" not in " ".join(binding.header_path)
        for binding in bindings
    )


def test_vertical_rowspan_header_leaf_stays_bound_with_origin_provenance() -> None:
    table_id = "plan@v2025#table26"
    row0 = TableRow(
        row_id=f"{table_id}#row0",
        row_index=0,
        is_header=True,
        cells=[
            _cell(
                table_id,
                0,
                0,
                "类别",
                (100.0, 200.0),
                0.0,
                40.0,
                is_header=True,
                rowspan=2,
            ),
            _cell(
                table_id,
                0,
                1,
                "课程编号",
                (200.0, 300.0),
                0.0,
                40.0,
                is_header=True,
                rowspan=2,
            ),
        ],
        text="类别 | 课程编号",
        locator=SourceLocator(format="pdf", page=9),
    )
    row1 = TableRow(
        row_id=f"{table_id}#row1",
        row_index=1,
        is_header=True,
        cells=[
            _cell(
                table_id,
                1,
                0,
                "",
                (100.0, 200.0),
                0.0,
                40.0,
                is_header=True,
                rowspan=2,
                covered=True,
                origin_row=0,
                origin_col=0,
            ),
            _cell(
                table_id,
                1,
                1,
                "",
                (200.0, 300.0),
                0.0,
                40.0,
                is_header=True,
                rowspan=2,
                covered=True,
                origin_row=0,
                origin_col=1,
            ),
        ],
        text="类别 | 课程编号",
        locator=SourceLocator(format="pdf", page=9),
    )
    table = TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=1,
        page=9,
        bbox=[100.0, 0.0, 300.0, 40.0],
        rows=[row0, row1],
        header_row_indices=[0, 1],
        column_count=2,
        row_count=2,
        column_headers=["类别", "课程编号"],
        header_paths=[["类别"], ["课程编号"]],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=9),
    )

    bindings = _local_header_bindings(table)

    assert [binding.status for binding in bindings] == ["bound", "bound"]
    for col, binding in enumerate(bindings):
        assert binding.shared_scope is False
        assert binding.covered is True
        assert binding.source_col == col
        assert binding.source_header_cell_ids == [f"{table_id}#r0c{col}"]
        assert binding.header_text == table.column_headers[col]
        assert binding.source_bbox == [
            float(100 + col * 100),
            0.0,
            float(200 + col * 100),
            40.0,
        ]
        assert binding.target_bbox == binding.source_bbox
        assert len(binding.target_bbox) == 4


def test_nonfinite_bounds_are_not_emitted() -> None:
    table_id = "plan@v2025#table27"
    table = TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=1,
        page=9,
        rows=[
            TableRow(
                row_id=f"{table_id}#row0",
                row_index=0,
                is_header=True,
                cells=[
                    _cell(
                        table_id,
                        0,
                        0,
                        "课程编号",
                        (100.0, float("inf")),
                        0.0,
                        20.0,
                        is_header=True,
                    )
                ],
                text="课程编号",
                locator=SourceLocator(format="pdf", page=9),
            )
        ],
        header_row_indices=[0],
        column_count=1,
        row_count=1,
        column_headers=["课程编号"],
        header_paths=[["课程编号"]],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=9),
    )

    bindings = _local_header_bindings(table)

    assert [binding.status for binding in bindings] == ["bound"]
    assert bindings[0].source_bbox is None
    assert bindings[0].target_bbox is None
    assert bindings[0].source_col == 0


def test_old_snapshot_without_bindings_loads_with_empty_default() -> None:
    table = TableStructure.model_validate(
        {
            "table_id": "plan@v2025#table1",
            "block_id": "plan@v2025#block1",
            "table_index": 1,
            "page": 8,
            "column_count": 2,
            "column_headers": ["课程编号", "学分"],
            "header_paths": [["课程编号"], ["学分"]],
            "rows": [],
            "locator": {"format": "pdf", "page": 8},
        }
    )

    assert table.column_bindings == []
    assert table.continuation_of is None
    assert table.header_source_table_id is None
    assert table.header_source_page is None


def test_html_tables_keep_legacy_headers_and_no_bindings() -> None:
    soup = BeautifulSoup(
        "<table>"
        "<tr><th>课程编号</th><th>课程名称</th></tr>"
        "<tr><td>00000000000018</td><td>测试课程寅</td></tr>"
        "</table>",
        "lxml",
    )
    table_tag = soup.find("table")
    assert table_tag is not None

    table, _rendered = extract_html_table(
        table_tag,
        doc_id="plan",
        version_id="v2025",
        table_index=1,
        block_id="plan@v2025#block1",
    )

    assert table.column_headers == ["课程编号", "课程名称"]
    assert table.header_paths == [["课程编号"], ["课程名称"]]
    assert table.column_bindings == []
    assert table.continuation_of is None


def test_reviewed_group_ancestor_unresolves_only_affected_columns() -> None:
    table_id = "plan@v2025#table31"
    header_row0 = TableRow(
        row_id=f"{table_id}#row0",
        row_index=0,
        is_header=True,
        cells=[
            _cell(
                table_id,
                0,
                0,
                "课程编号",
                (100.0, 200.0),
                100.0,
                140.0,
                is_header=True,
                rowspan=2,
            ),
            _cell(
                table_id,
                0,
                1,
                "课程名称",
                (200.0, 300.0),
                100.0,
                140.0,
                is_header=True,
                rowspan=2,
            ),
            _cell(
                table_id,
                0,
                2,
                "总学时",
                (300.0, 450.0),
                100.0,
                120.0,
                is_header=True,
                colspan=2,
            ),
            _cell(
                table_id,
                0,
                3,
                "",
                (300.0, 450.0),
                100.0,
                120.0,
                is_header=True,
                colspan=2,
                covered=True,
                origin_col=2,
            ),
            _cell(
                table_id,
                0,
                4,
                "学分",
                (450.0, 500.0),
                100.0,
                140.0,
                is_header=True,
                rowspan=2,
            ),
        ],
        text="课程编号 | 课程名称 | 总学时 | 总学时 | 学分",
        locator=SourceLocator(format="pdf", page=9),
    )
    header_row0.cells[2].needs_review = True
    header_row1 = TableRow(
        row_id=f"{table_id}#row1",
        row_index=1,
        is_header=True,
        cells=[
            _cell(
                table_id,
                1,
                0,
                "",
                (100.0, 200.0),
                100.0,
                140.0,
                is_header=True,
                rowspan=2,
                covered=True,
                origin_row=0,
                origin_col=0,
            ),
            _cell(
                table_id,
                1,
                1,
                "",
                (200.0, 300.0),
                100.0,
                140.0,
                is_header=True,
                rowspan=2,
                covered=True,
                origin_row=0,
                origin_col=1,
            ),
            _cell(
                table_id,
                1,
                2,
                "理论",
                (300.0, 375.0),
                120.0,
                140.0,
                is_header=True,
            ),
            _cell(
                table_id,
                1,
                3,
                "实践",
                (375.0, 450.0),
                120.0,
                140.0,
                is_header=True,
            ),
            _cell(
                table_id,
                1,
                4,
                "",
                (450.0, 500.0),
                100.0,
                140.0,
                is_header=True,
                rowspan=2,
                covered=True,
                origin_row=0,
                origin_col=4,
            ),
        ],
        text="课程编号 | 课程名称 | 理论 | 实践 | 学分",
        locator=SourceLocator(format="pdf", page=9),
    )
    data_row = TableRow(
        row_id=f"{table_id}#row2",
        row_index=2,
        cells=[
            _cell(table_id, 2, 0, "00000000000031", (100.0, 200.0), 140.0, 160.0),
            _cell(table_id, 2, 1, "测试课程申", (200.0, 300.0), 140.0, 160.0),
            _cell(table_id, 2, 2, "48", (300.0, 375.0), 140.0, 160.0),
            _cell(table_id, 2, 3, "16", (375.0, 450.0), 140.0, 160.0),
            _cell(table_id, 2, 4, "3", (450.0, 500.0), 140.0, 160.0),
        ],
        text="00000000000031 | 测试课程申 | 48 | 16 | 3",
        locator=SourceLocator(format="pdf", page=9),
    )
    table = TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=1,
        page=9,
        bbox=[100.0, 100.0, 500.0, 160.0],
        rows=[header_row0, header_row1, data_row],
        header_row_indices=[0, 1],
        column_count=5,
        row_count=3,
        column_headers=[
            "课程编号",
            "课程名称",
            "总学时 > 理论",
            "总学时 > 实践",
            "学分",
        ],
        header_paths=[
            ["课程编号"],
            ["课程名称"],
            ["总学时", "理论"],
            ["总学时", "实践"],
            ["学分"],
        ],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=9),
    )

    bindings = _local_header_bindings(table)

    assert [binding.status for binding in bindings] == [
        "bound",
        "bound",
        "unresolved",
        "unresolved",
        "bound",
    ]
    for col in (2, 3):
        reviewed = bindings[col]
        assert reviewed.method == "none"
        assert reviewed.header_text == ""
        assert reviewed.header_path == []
        assert reviewed.source_header_cell_ids == []
        assert reviewed.review_source_table_id == table_id
        assert reviewed.review_source_page == 9
        assert reviewed.review_source_col == 2
        assert reviewed.review_source_bbox == [300.0, 100.0, 450.0, 120.0]
        assert reviewed.target_bbox is not None
    assert bindings[2].review_source_header_cell_ids == [
        f"{table_id}#r0c2",
        f"{table_id}#r1c2",
    ]
    assert bindings[2].review_source_text == "总学时 > 理论"
    assert bindings[3].review_source_header_cell_ids == [
        f"{table_id}#r0c2",
        f"{table_id}#r1c3",
    ]
    assert bindings[3].review_source_text == "总学时 > 实践"

    credit = bindings[4]
    assert credit.status == "bound"
    assert credit.method == "local_header"
    assert credit.header_text == "学分"
    assert credit.header_path == ["学分"]
    assert credit.source_header_cell_ids == [f"{table_id}#r0c4"]

    # 只清被复核来源影响的解释性列名，安全列仍保留。
    assert table.column_headers == ["课程编号", "课程名称", "", "", "学分"]
    assert table.header_paths == [
        ["课程编号"],
        ["课程名称"],
        [],
        [],
        ["学分"],
    ]
    # 原始 TableRow 表头单元格与原生文本不变，供后续区域复核。
    assert table.rows[0].cells[2].text == "总学时"
    assert table.rows[0].cells[2].native_text == "总学时"
    assert table.rows[0].cells[2].needs_review is True
    assert table.rows[1].cells[2].text == "理论"
    assert table.rows[1].cells[3].text == "实践"
    assert any(
        "表头来源需复核：c2" in warning for warning in table.warnings
    )
    assert any(
        "表头来源需复核：c3" in warning for warning in table.warnings
    )

    effective = _effective_column_bindings(table)
    assert [binding.status for binding in effective] == [
        "bound",
        "bound",
        "unresolved",
        "unresolved",
        "bound",
    ]
    assert effective[2].header_text == ""
    assert effective[3].header_text == ""
    assert effective[4].header_text == "学分"


def test_pdf_extraction_uses_reviewed_headers_not_stale_labels(
    monkeypatch: Any,
) -> None:
    raw_rows: list[list[Any]] = [
        [
            [100.0, 0.0, 200.0, 25.0],
            [200.0, 0.0, 300.0, 25.0],
            [300.0, 0.0, 450.0, 25.0],
            None,
            [450.0, 0.0, 500.0, 25.0],
        ],
        [
            [100.0, 25.0, 200.0, 50.0],
            [200.0, 25.0, 300.0, 50.0],
            [300.0, 25.0, 375.0, 50.0],
            [375.0, 25.0, 450.0, 50.0],
            [450.0, 25.0, 500.0, 50.0],
        ],
        [
            [100.0, 50.0, 200.0, 75.0],
            [200.0, 50.0, 300.0, 75.0],
            [300.0, 50.0, 375.0, 75.0],
            [375.0, 50.0, 450.0, 75.0],
            [450.0, 50.0, 500.0, 75.0],
        ],
    ]
    extracted = [
        ["课程编号", "课程名称", "总学时", "", "学分"],
        ["", "", "理论", "实践", ""],
        ["00000000000032", "测试课程酉", "48", "16", "3"],
    ]
    table = _NativeTable(raw_rows, extracted)
    group_bbox = [300.0, 0.0, 450.0, 25.0]

    def _clip_text(_source_page: Any, bbox: list[float]) -> str | None:
        if [float(value) for value in bbox] == group_bbox:
            return "总学时X"
        return None

    monkeypatch.setattr(structure_module, "_pdf_clip_text", _clip_text)

    structure, _rendered = extract_pdf_table(
        table,
        doc_id="plan",
        version_id="v2025",
        table_index=32,
        page=9,
        block_id="plan@v2025#block32",
        source_page=object(),
    )

    assert structure.header_row_indices == [0, 1]
    # 抽取结果必须使用复核后的解释表头，而不是抽取前算好的旧标签。
    assert structure.column_headers == [
        "课程编号",
        "课程名称",
        "",
        "",
        "学分",
    ]
    assert structure.header_paths == [
        ["课程编号"],
        ["课程名称"],
        [],
        [],
        ["学分"],
    ]
    assert [binding.status for binding in structure.column_bindings] == [
        "bound",
        "bound",
        "unresolved",
        "unresolved",
        "bound",
    ]
    assert structure.column_bindings[4].header_text == "学分"
    assert structure.rows[0].cells[2].text == "总学时"
    assert structure.rows[0].cells[2].native_text == "总学时"
    assert structure.rows[0].cells[2].needs_review is True
    assert structure.rows[1].cells[2].text == "理论"
    assert "总学时" not in structure.column_headers
    assert all("总学时" not in " ".join(path) for path in structure.header_paths)
    assert any(
        "表头来源需复核：c2" in warning for warning in structure.warnings
    )
    assert any(
        "表头来源需复核：c3" in warning for warning in structure.warnings
    )
    assert any("2 列未解析" in warning for warning in structure.warnings)


def test_continuation_does_not_resurrect_reviewed_legacy_labels() -> None:
    headers = ["课程编号", "课程名称", "学时", "学分", "开课学期"]
    intervals: list[Interval] = [
        (100.0, 200.0),
        (200.0, 300.0),
        (300.0, 340.0),
        (340.0, 380.0),
        (380.0, 420.0),
    ]
    previous = _table(
        "plan@v2025#table33",
        8,
        headers=headers,
        intervals=intervals,
        data_rows=[["00000000000033", "测试课程戌", "48", "3", "秋"]],
    )
    previous.rows[0].cells[2].needs_review = True
    previous.column_bindings = [
        HeaderColumnBinding(
            target_col=col,
            status="bound",
            method="local_header",
            source_table_id=previous.table_id,
            source_page=8,
            source_col=col,
            source_header_cell_ids=[previous.rows[0].cells[col].cell_id],
            source_header_row_indices=[0],
            source_bbox=list(previous.rows[0].cells[col].bbox or []),
            target_bbox=list(previous.rows[0].cells[col].bbox or []),
            header_text=label,
            header_path=[label],
        )
        for col, label in enumerate(headers)
    ]

    effective = _effective_column_bindings(previous)

    assert [binding.status for binding in effective] == [
        "bound",
        "bound",
        "unresolved",
        "bound",
        "bound",
    ]
    stale = effective[2]
    assert stale.method == "none"
    assert stale.header_text == ""
    assert stale.header_path == []
    assert stale.review_source_header_cell_ids == [
        f"{previous.table_id}#r0c2"
    ]
    assert stale.review_source_text == "学时"
    assert "旧表头绑定来源" in stale.reason
    # 旧绑定不再能沿用；只清该列，安全相邻列保持可用。
    assert previous.column_headers == [
        "课程编号",
        "课程名称",
        "",
        "学分",
        "开课学期",
    ]
    assert previous.header_paths == [
        ["课程编号"],
        ["课程名称"],
        [],
        ["学分"],
        ["开课学期"],
    ]
    assert effective[3].status == "bound"
    assert effective[3].header_text == "学分"
    assert any(
        "表头来源需复核：c2" in warning for warning in previous.warnings
    )

    current = _table(
        "plan@v2025#table34",
        9,
        headers=None,
        intervals=[
            (100.0, 200.0),
            (200.0, 300.0),
            (340.0, 380.0),
            (380.0, 420.0),
        ],
        data_rows=[["00000000000034", "测试课程亥", "3", "春"]],
    )

    assert restore_continuation_header(previous, current)
    assert current.column_headers == [
        "课程编号",
        "课程名称",
        "学分",
        "开课学期",
    ]
    assert current.header_paths == [
        ["课程编号"],
        ["课程名称"],
        ["学分"],
        ["开课学期"],
    ]
    assert [binding.status for binding in current.column_bindings] == [
        "bound",
        "bound",
        "bound",
        "bound",
    ]
    assert current.column_bindings[2].header_text == "学分"
    assert current.column_bindings[2].source_col == 3
    assert "学时" not in " ".join(current.column_headers)
    assert all("学时" not in " ".join(path) for path in current.header_paths)

    # 同列数续表只要含未复核列就无法全部继承，整体拒绝而不复活旧标签。
    same_shape = _table(
        "plan@v2025#table35",
        9,
        headers=None,
        intervals=intervals,
        data_rows=[["00000000000035", "测试课程子", "48", "3", "春"]],
    )
    assert not restore_continuation_header(previous, same_shape)
    assert same_shape.column_headers == [""] * 5
    assert same_shape.header_paths == []
    assert same_shape.column_bindings == []
    assert same_shape.continuation_of is None
