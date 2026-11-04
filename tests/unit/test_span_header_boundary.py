"""Covered span origins and PDF curriculum header boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from campusguide.domain.enums import BlockType
from campusguide.domain.models import Article, CellProvenance
from campusguide.ingest.structure import (
    _course_code_column,
    extract_pdf_table,
)
from campusguide.retrieval.source_spans import structured_table_row
from tests.w2_helpers import make_article

_HEADERS = ("课程代码", "课程名称", "修读说明", "学分", "课程类别")


def _cell(
    row_index: int,
    col: int,
    text: str,
    *,
    covered: bool = False,
    origin_row: int | None = None,
    origin_col: int | None = None,
    rowspan: int = 1,
    colspan: int = 1,
) -> CellProvenance:
    return CellProvenance(
        cell_id=f"t1-{row_index}-{col}",
        row=row_index,
        col=col,
        text=text,
        rowspan=rowspan,
        colspan=colspan,
        origin_row=row_index if origin_row is None else origin_row,
        origin_col=col if origin_col is None else origin_col,
        covered=covered,
        text_source="native",
    )


def _article(
    cells: list[CellProvenance],
    headers: tuple[str, ...] = _HEADERS,
    *,
    row_index: int = 1,
) -> Article:
    article = make_article(
        f"plan@v2024#t1.row{row_index}",
        doc_id="plan",
        version_id="v2024",
        doc_title="甲专业培养方案",
        article_no=f"表格1第{row_index}行",
        block_type=BlockType.SECTION,
        text="表头："
        + " | ".join(headers)
        + "\n行："
        + " | ".join(cell.text for cell in cells),
    )
    return article.model_copy(
        update={
            "block_kind": "table_row",
            "parent_article_id": "plan@v2024#table1",
            "parent_table_id": "t1",
            "table_id": "t1",
            "row_id": f"t1-r{row_index}",
            "row_index": row_index,
            "table_headers": list(headers),
            "header_paths": [[header] for header in headers],
            "cell_provenance": cells,
        }
    )


def test_horizontal_colspan_covered_keeps_following_credits_reliable() -> None:
    cells = [
        _cell(1, 0, "617017"),
        _cell(1, 1, "高等数学", colspan=2),
        _cell(1, 2, "", covered=True, origin_col=1, colspan=2),
        _cell(1, 3, "5"),
        _cell(1, 4, "必修"),
    ]
    row = structured_table_row(_article(cells))
    assert row is not None
    assert row.reliable
    assert "COVERED_ORIGIN_MISMATCH:2" not in row.problems
    assert row.values[2] == ""
    assert row.course_code == "617017"
    assert row.value("credits") == "5"
    assert row.category == "必修"


def test_cross_row_covered_is_context_without_numeric_support() -> None:
    cells = [
        _cell(2, 0, "", covered=True, origin_row=1, origin_col=0),
        _cell(2, 1, "大学物理"),
        _cell(2, 2, "说明"),
        _cell(2, 3, "4"),
        _cell(2, 4, "必修"),
    ]
    row = structured_table_row(_article(cells, row_index=2))
    assert row is not None
    assert row.reliable
    assert row.values[0] == ""
    assert row.course_code == ""


def test_covered_origin_colspan_must_reach_current_column() -> None:
    cells = [
        _cell(1, 0, "617017"),
        _cell(1, 1, "高等数学"),
        _cell(1, 2, "", covered=True, origin_col=0),
        _cell(1, 3, "5"),
        _cell(1, 4, "必修"),
    ]
    row = structured_table_row(_article(cells))
    assert row is not None
    assert not row.reliable
    assert "COVERED_ORIGIN_MISMATCH:2" in row.problems


def test_covered_origin_on_right_or_future_row_is_rejected() -> None:
    right = [
        _cell(1, 0, "617017"),
        _cell(1, 1, "", covered=True, origin_col=2),
        _cell(1, 2, "说明"),
        _cell(1, 3, "5"),
        _cell(1, 4, "必修"),
    ]
    row = structured_table_row(_article(right))
    assert row is not None and not row.reliable
    assert "COVERED_ORIGIN_MISMATCH:1" in row.problems

    future = [
        _cell(1, 0, "617017"),
        _cell(1, 1, "", covered=True, origin_row=2, origin_col=1),
        _cell(1, 2, "说明"),
        _cell(1, 3, "5"),
        _cell(1, 4, "必修"),
    ]
    row = structured_table_row(_article(future))
    assert row is not None and not row.reliable
    assert "COVERED_ORIGIN_MISMATCH:1" in row.problems

    negative = [
        _cell(1, 0, "617017"),
        _cell(1, 1, "", covered=True, origin_col=-1),
        _cell(1, 2, "说明"),
        _cell(1, 3, "5"),
        _cell(1, 4, "必修"),
    ]
    row = structured_table_row(_article(negative))
    assert row is not None and not row.reliable
    assert "COVERED_ORIGIN_MISMATCH:1" in row.problems


def test_covered_origin_cannot_be_another_covered_cell() -> None:
    cells = [
        _cell(1, 0, "617017", colspan=2),
        _cell(1, 1, "", covered=True, origin_col=0),
        _cell(1, 2, "", covered=True, origin_col=1),
        _cell(1, 3, "5"),
        _cell(1, 4, "必修"),
    ]
    row = structured_table_row(_article(cells))
    assert row is not None
    assert not row.reliable
    assert "COVERED_ORIGIN_MISMATCH:2" in row.problems


def test_covered_numeric_text_never_becomes_credits() -> None:
    headers = ("课程代码", "课程名称", "学分", "备注")
    cells = [
        _cell(1, 0, "617017"),
        _cell(1, 1, "高等数学", colspan=2),
        _cell(1, 2, "5", covered=True, origin_col=1, colspan=2),
        _cell(1, 3, "通识"),
    ]
    row = structured_table_row(_article(cells, headers))
    assert row is not None
    assert not row.reliable
    assert "COVERED_CELL_HAS_TEXT:2" in row.problems
    assert row.values[2] == ""
    assert row.value("credits") == ""


class _PdfTable:
    """Minimal PyMuPDF Table surface used by extract_pdf_table."""

    def __init__(
        self,
        rows: list[list[str]],
        *,
        header_rows: int = 0,
        names: list[str] | None = None,
        header_bbox: list[float] | None = None,
        external: bool = False,
    ) -> None:
        self._rows = [list(row) for row in rows]
        self.header_rows = header_rows
        self.col_count = max((len(row) for row in rows), default=0)
        self.row_count = len(rows)
        self.bbox = [0.0, 0.0, 500.0, 800.0]
        self.rows: list[Any] = []
        self.placements = None
        self.header = SimpleNamespace(
            names=list(names or []), bbox=header_bbox, external=external
        )

    def extract(self) -> list[list[str]]:
        return [list(row) for row in self._rows]


def _extract(table: _PdfTable) -> Any:
    structure, _ = extract_pdf_table(
        table,
        doc_id="plan",
        version_id="v2024",
        table_index=0,
        page=1,
        block_id="b0",
    )
    return structure


_COURSE_ROWS = [
    ["课程代码", "课程名称", "学分"],
    ["617017", "高等数学", "5"],
    ["617018", "大学物理", "4"],
    ["617019", "程序设计", "3"],
    ["617020", "线性代数", "3"],
]


def test_native_header_bbox_stops_at_first_real_course_code() -> None:
    table = _PdfTable(
        _COURSE_ROWS,
        header_rows=0,
        names=["课程代码", "课程名称", "学分"],
        header_bbox=[0.0, 0.0, 500.0, 700.0],
    )
    structure = _extract(table)
    assert structure.header_row_indices == [0]
    assert structure.rows[0].is_header
    assert not structure.rows[1].is_header
    assert structure.rows[1].text.startswith("617017")
    assert structure.header_paths[0] == ["课程代码"]
    assert all(
        "617017" not in path and "617018" not in path
        for path in structure.header_paths
    )


def test_declared_header_rows_stop_before_first_course() -> None:
    table = _PdfTable(
        _COURSE_ROWS,
        header_rows=4,
        names=["课程代码", "课程名称", "学分"],
    )
    structure = _extract(table)
    assert structure.header_row_indices == [0]
    assert structure.header_paths[0] == ["课程代码"]
    assert structure.rows[1].text.startswith("617017")


def test_header_boundary_is_not_a_fixed_three_rows() -> None:
    table = _PdfTable(
        [
            ["课程代码", "课程名称", "学分"],
            ["", "", ""],
            ["", "", ""],
            ["", "", ""],
            ["617017", "高等数学", "5"],
            ["617018", "大学物理", "4"],
        ],
        header_rows=6,
        names=["课程代码", "课程名称", "学分"],
    )
    structure = _extract(table)
    assert structure.header_row_indices == [0, 1, 2, 3]
    assert not structure.rows[4].is_header
    assert structure.rows[4].text.startswith("617017")


def test_two_level_hours_header_is_not_lost() -> None:
    table = _PdfTable(
        [
            ["课程代码", "课程名称", "总学时", ""],
            ["", "", "理论", "实验"],
            ["617017", "高等数学", "48", "16"],
            ["617018", "大学物理", "40", "8"],
        ],
        header_rows=2,
        names=["课程代码", "课程名称", "总学时", "总学时"],
    )
    structure = _extract(table)
    assert structure.header_row_indices == [0, 1]
    assert structure.header_paths[0] == ["课程代码"]
    assert structure.header_paths[2][-1] == "理论"
    assert structure.rows[2].text.startswith("617017")


def test_external_header_geometry_never_swallows_first_course() -> None:
    # 非 6/14 位代码无法靠数据边界兜底，外置 bbox 一旦参与就会吞掉首行数据。
    table = _PdfTable(
        [
            ["KC001", "高等数学", "5"],
            ["KC002", "大学物理", "4"],
        ],
        header_rows=0,
        names=["课程代码", "课程名称", "学分"],
        header_bbox=[0.0, 0.0, 500.0, 700.0],
        external=True,
    )
    structure = _extract(table)
    assert structure.header_row_indices == []
    assert not structure.rows[0].is_header
    assert not structure.rows[1].is_header
    assert structure.rows[0].text.startswith("KC001")
    assert structure.header_paths == [[], [], []]


def test_student_ids_and_amounts_are_not_course_code_columns() -> None:
    assert _course_code_column(["学号", "姓名", "金额"]) is None
    assert _course_code_column(["编号", "姓名", "学分"]) is None
    assert _course_code_column(["课程编号", "课程名称", "学分"]) == 0
    assert _course_code_column(["课程代码", "课程名称", "总学时"]) == 0
