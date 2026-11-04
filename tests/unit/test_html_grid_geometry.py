"""Synthetic tests for mapping HTML DOM tables onto rendered-PDF geometry.

These tests prove the strict mapping protocol only; they do not measure real
HTML or PDF extraction accuracy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from campusguide.ingest.html_grid import build_html_grid_geometry
from campusguide.ingest.structure import (
    PageStructure,
    ParsedStructure,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)
from campusguide.ingest.visual_verify import build_native_grid_context


def _render_bbox(row: int, col: int, page: int = 1) -> list[float]:
    x0 = 10.0 + 30.0 * col
    y0 = 10.0 + 15.0 * row + 20.0 * (page - 1)
    return [x0, y0, x0 + 28.0, y0 + 13.0]


def _row(
    prefix: str,
    row_index: int,
    texts: Sequence[str],
    *,
    page: int | None,
    bbox: bool = False,
    is_header: bool = False,
    spans: Mapping[int, tuple[int, int]] | None = None,
    covered: Mapping[int, tuple[int, int]] | None = None,
) -> TableRow:
    span_map = dict(spans or {})
    covered_map = dict(covered or {})
    cells: list[TableCell] = []
    for col, text in enumerate(texts):
        rowspan, colspan = span_map.get(col, (1, 1))
        origin = covered_map.get(col)
        cells.append(
            TableCell(
                cell_id=f"{prefix}#r{row_index}c{col}",
                row=row_index,
                col=col,
                rowspan=rowspan,
                colspan=colspan,
                origin_row=row_index if origin is None else origin[0],
                origin_col=col if origin is None else origin[1],
                covered=origin is not None,
                text="" if origin is not None else text,
                bbox=_render_bbox(row_index, col, page)
                if bbox and page is not None
                else None,
                is_header=is_header,
            )
        )
    return TableRow(
        row_id=f"{prefix}#row{row_index}",
        row_index=row_index,
        is_header=is_header,
        cells=cells,
        text=" | ".join(texts),
        locator=SourceLocator(format="pdf" if page is not None else "html", page=page),
    )


def _table(
    table_id: str,
    rows: Sequence[TableRow],
    *,
    page: int | None = None,
    column_count: int = 3,
    kind: str = "data",
    header_indices: Sequence[int] | None = None,
    table_index: int = 1,
    column_headers: Sequence[str] | None = None,
    header_paths: Sequence[Sequence[str]] | None = None,
) -> TableStructure:
    headers = [0] if header_indices is None else list(header_indices)
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=table_index,
        page=page,
        rows=list(rows),
        header_row_indices=headers,
        column_count=column_count,
        row_count=len(rows),
        column_headers=list(column_headers or []),
        header_paths=[list(path) for path in (header_paths or [])],
        kind=kind,
        source="html_dom" if page is None else "pymupdf",
        locator=SourceLocator(format="html" if page is None else "pdf", page=page),
    )


def _structure(
    fmt: str,
    tables: Sequence[TableStructure],
    *,
    pages: Sequence[PageStructure] = (),
) -> ParsedStructure:
    return ParsedStructure(format=fmt, tables=list(tables), pages=list(pages))


def _page(
    page: int = 1, *, width: float | None = 300.0, height: float | None = 200.0
) -> PageStructure:
    return PageStructure(page=page, width=width, height=height)


def test_single_page_full_match_preserves_native_identity() -> None:
    native = _structure(
        "html",
        [
            _table(
                "org#table1",
                [
                    _row("n", 0, ["分 工", "姓\u3000名", "联系支部"], page=None, is_header=True),
                    _row("n", 1, ["党委书记", "谢 珊", "甲支部"], page=None),
                    _row("n", 2, ["组织委员", "赫圣博", "乙支部"], page=None),
                ],
                column_headers=["分 工", "姓\u3000名", "联系支部"],
                header_paths=[["分 工"], ["姓\u3000名"], ["联系支部"]],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#table1",
                [
                    _row("r", 0, ["分 工", "姓 名", "联系支部"], page=1, bbox=True, is_header=True),
                    _row("r", 1, ["党委书记", "谢 珊", "甲支部"], page=1, bbox=True),
                    _row("r", 2, ["组织委员", "赫圣博", "乙支部"], page=1, bbox=True),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )
    before = native.model_dump()

    result = build_html_grid_geometry(native, rendered)

    assert native.model_dump() == before
    assert list(result.page_tables) == [1]
    table = result.page_tables[1][0]
    assert table.table_id == "org#table1"
    assert table.page == 1
    assert table.row_count == 3
    assert [row.row_index for row in table.rows] == [0, 1, 2]
    assert table.column_headers == ["分 工", "姓\u3000名", "联系支部"]
    assert table.rows[1].cells[1].text == "谢 珊"
    assert table.rows[1].cells[1].bbox == _render_bbox(1, 1, 1)
    assert table.rows[2].cells[2].bbox == _render_bbox(2, 2, 1)
    assert result.expected_rows == {"org#table1": {0, 1, 2}}
    assert result.mapped_rows == {"org#table1": {0, 1, 2}}
    assert result.unresolved == []

    context = build_native_grid_context(
        result.page_tables[1],
        page_no=1,
        source_format="html",
        page_size=(300.0, 200.0),
    )
    assert context["native_page_size"] == [300.0, 200.0]
    context_row = next(row for row in context["tables"][0]["rows"] if row["row"] == 1)
    context_cell = next(cell for cell in context_row["cells"] if cell["col"] == 1)
    assert context_cell["bbox"] == _render_bbox(1, 1, 1)
    assert context_cell["bbox_reliable"] is True


def _two_page_case():
    native = _structure(
        "html",
        [
            _table(
                "org#table1",
                [
                    _row("n", 0, ["项目", "负责人", "支部"], page=None, is_header=True),
                    _row("n", 1, ["甲", "张三", "一支部"], page=None),
                    _row("n", 2, ["乙", "李四", "二支部"], page=None),
                    _row("n", 3, ["丙", "王五", "三支部"], page=None),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#p1",
                [
                    _row("r1", 0, ["项目", "负责人", "支部"], page=1, bbox=True, is_header=True),
                    _row("r1", 1, ["甲", "张三", "一支部"], page=1, bbox=True),
                    _row("r1", 2, ["乙", "李四", "二支部"], page=1, bbox=True),
                ],
                page=1,
                table_index=1,
            ),
            _table(
                "render#p2",
                [
                    _row("r2", 0, ["项目", "负责人", "支部"], page=2, bbox=True, is_header=True),
                    _row("r2", 1, ["丙", "王五", "三支部"], page=2, bbox=True),
                ],
                page=2,
                table_index=2,
            ),
        ],
        pages=[_page(1), _page(2)],
    )
    return native, rendered, build_html_grid_geometry(native, rendered)


def test_two_page_fragments_keep_dom_row_indices_and_union() -> None:
    _, _, result = _two_page_case()

    assert list(result.page_tables) == [1, 2]
    page_one, page_two = result.page_tables[1][0], result.page_tables[2][0]
    assert [row.row_index for row in page_one.rows] == [0, 1, 2]
    assert page_one.row_count == 3
    assert [row.row_index for row in page_two.rows] == [0, 3]
    assert page_two.row_count == 2
    assert page_two.rows[1].row_id == "n#row3"
    assert page_two.page == 2
    assert result.expected_rows == {"org#table1": {0, 1, 2, 3}}
    assert result.mapped_rows == {"org#table1": {0, 1, 2, 3}}
    assert result.unresolved == []


def test_repeated_header_uses_current_page_geometry() -> None:
    _, _, result = _two_page_case()

    header_maps = [item for item in result.row_mapping if item["row_index"] == 0]
    assert [item["page"] for item in header_maps] == [1, 2]
    assert [item["repeated_header"] for item in header_maps] == [False, True]
    page_two_header = result.page_tables[2][0].rows[0]
    assert page_two_header.cells[0].bbox == _render_bbox(0, 0, 2)
    assert page_two_header.cells[0].bbox != _render_bbox(0, 0, 1)


def test_same_column_count_tables_do_not_borrow_rows() -> None:
    native = _structure(
        "html",
        [
            _table(
                "a#table1",
                [
                    _row("na", 0, ["项目", "金额"], page=None, is_header=True),
                    _row("na", 1, ["甲", "10"], page=None),
                ],
                column_count=2,
                table_index=1,
            ),
            _table(
                "b#table1",
                [
                    _row("nb", 0, ["项目", "数量"], page=None, is_header=True),
                    _row("nb", 1, ["乙", "20"], page=None),
                ],
                column_count=2,
                table_index=2,
            ),
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#decoy",
                [
                    _row("rd", 0, ["其他", "列"], page=1, bbox=True, is_header=True),
                    _row("rd", 1, ["丙", "30"], page=1, bbox=True),
                ],
                page=1,
                column_count=2,
                table_index=1,
            ),
            _table(
                "render#a",
                [
                    _row("ra", 0, ["项目", "金额"], page=1, bbox=True, is_header=True),
                    _row("ra", 1, ["甲", "10"], page=1, bbox=True),
                ],
                page=1,
                column_count=2,
                table_index=2,
            ),
            _table(
                "render#b",
                [
                    _row("rb", 0, ["项目", "数量"], page=2, bbox=True, is_header=True),
                    _row("rb", 1, ["乙", "20"], page=2, bbox=True),
                ],
                page=2,
                column_count=2,
                table_index=3,
            ),
        ],
        pages=[_page(1), _page(2)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert [table.table_id for table in result.page_tables[1]] == ["a#table1"]
    assert [table.table_id for table in result.page_tables[2]] == ["b#table1"]
    table_a = result.page_tables[1][0]
    table_b = result.page_tables[2][0]
    assert table_a.rows[1].cells[1].bbox == _render_bbox(1, 1, 1)
    assert table_b.rows[1].cells[1].bbox == _render_bbox(1, 1, 2)
    assert result.expected_rows == {"a#table1": {0, 1}, "b#table1": {0, 1}}
    assert result.mapped_rows == {"a#table1": {0, 1}, "b#table1": {0, 1}}
    assert result.unresolved == []


def test_duplicate_same_value_rows_fail_closed() -> None:
    native = _structure(
        "html",
        [
            _table(
                "dup#table1",
                [
                    _row("nd", 0, ["项目", "金额"], page=None, is_header=True),
                    _row("nd", 1, ["甲", "10"], page=None),
                    _row("nd", 2, ["甲", "10"], page=None),
                ],
                column_count=2,
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#dup",
                [
                    _row("rd", 0, ["项目", "金额"], page=1, bbox=True, is_header=True),
                    _row("rd", 1, ["甲", "10"], page=1, bbox=True),
                    _row("rd", 2, ["甲", "10"], page=1, bbox=True),
                ],
                page=1,
                column_count=2,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    duplicate_rows = {
        (item["row"], item["reason"])
        for item in result.unresolved
        if item["reason"] == "duplicate_row_signature"
    }
    assert duplicate_rows == {(1, "duplicate_row_signature"), (2, "duplicate_row_signature")}
    assert result.page_tables == {}
    assert result.mapped_rows == {"dup#table1": set()}
    assert native.tables[0].rows[1].cells[0].text == "甲"
    assert native.tables[0].rows[2].cells[0].text == "甲"


def test_column_swap_is_not_matched() -> None:
    native = _structure(
        "html",
        [
            _table(
                "swap#table1",
                [
                    _row("ns", 0, ["A", "B", "C"], page=None, is_header=True),
                    _row("ns", 1, ["1", "2", "3"], page=None),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#swap",
                [
                    _row("rs", 0, ["A", "B", "C"], page=1, bbox=True, is_header=True),
                    _row("rs", 1, ["1", "3", "2"], page=1, bbox=True),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.page_tables == {}
    assert result.mapped_rows == {"swap#table1": set()}
    assert any(
        item["row"] == 1 and item["col"] == 1 and item["reason"] == "cell_value_mismatch"
        for item in result.unresolved
    )
    assert native.tables[0].rows[1].cells[1].text == "2"


def test_column_count_mismatch_is_not_matched() -> None:
    native = _structure(
        "html",
        [
            _table(
                "wide#table1",
                [
                    _row("nw", 0, ["A", "B", "C"], page=None, is_header=True),
                    _row("nw", 1, ["1", "2", "3"], page=None),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#narrow",
                [
                    _row("rn", 0, ["A", "B"], page=1, bbox=True, is_header=True),
                    _row("rn", 1, ["1", "2"], page=1, bbox=True),
                ],
                page=1,
                column_count=2,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.page_tables == {}
    assert result.mapped_rows == {"wide#table1": set()}
    assert any(
        item["row"] is None and item["reason"] == "no_render_table"
        for item in result.unresolved
    )


def test_split_half_row_is_not_matched_as_complete() -> None:
    native = _structure(
        "html",
        [
            _table(
                "split#table1",
                [
                    _row("ns", 0, ["项目", "金额", "备注"], page=None, is_header=True),
                    _row("ns", 1, ["甲", "10", "首行"], page=None),
                    _row("ns", 2, ["乙", "20", "次行"], page=None),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#split",
                [
                    _row("rs", 0, ["项目", "金额", "备注"], page=1, bbox=True, is_header=True),
                    _row("rs", 1, ["甲", "10", "首行"], page=1, bbox=True),
                    _row("rs", 2, ["乙", "", "次行"], page=1, bbox=True),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    page_table = result.page_tables[1][0]
    assert [row.row_index for row in page_table.rows] == [0, 1]
    assert page_table.row_count == 2
    assert result.mapped_rows == {"split#table1": {0, 1}}
    assert any(
        item["row"] == 2 and item["col"] == 1 and item["reason"] == "cell_value_mismatch"
        for item in result.unresolved
    )


def test_origin_bbox_and_covered_cells_do_not_mutate_native() -> None:
    native = _structure(
        "html",
        [
            _table(
                "span#table1",
                [
                    _row("ns", 0, ["职务", "姓名", "支部"], page=None, is_header=True),
                    _row(
                        "ns",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=None,
                        spans={0: (2, 1)},
                    ),
                    _row(
                        "ns",
                        2,
                        ["", "赫圣博", "二支部"],
                        page=None,
                        spans={0: (2, 1)},
                        covered={0: (1, 0)},
                    ),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#span",
                [
                    _row("rs", 0, ["职务", "姓名", "支部"], page=1, bbox=True, is_header=True),
                    _row(
                        "rs",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=1,
                        bbox=True,
                        spans={0: (2, 1)},
                    ),
                    _row(
                        "rs",
                        2,
                        ["", "赫圣博", "二支部"],
                        page=1,
                        bbox=True,
                        spans={0: (2, 1)},
                        covered={0: (1, 0)},
                    ),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )
    before = native.model_dump()

    result = build_html_grid_geometry(native, rendered)

    table = result.page_tables[1][0]
    assert table is not native.tables[0]
    assert table.rows[1].cells[0].bbox == _render_bbox(1, 0, 1)
    assert table.rows[2].cells[0].covered is True
    assert table.rows[2].cells[0].bbox is None
    assert (table.rows[2].cells[0].origin_row, table.rows[2].cells[0].origin_col) == (1, 0)
    assert table.rows[2].cells[1].bbox == _render_bbox(2, 1, 1)
    assert native.model_dump() == before
    assert native.tables[0].rows[2].cells[0].bbox is None
    assert result.unresolved == []
    covered_mapping = next(
        item
        for item in result.cell_mapping
        if item["row_index"] == 2 and item["col"] == 0
    )
    assert covered_mapping["covered"] is True
    assert covered_mapping["bbox"] is None


def test_layout_table_does_not_occupy_expected_rows() -> None:
    native = _structure(
        "html",
        [
            _table(
                "layout#table1",
                [
                    _row("nl", 0, ["A", "B", "C"], page=None, is_header=True),
                    _row("nl", 1, ["甲", "乙", "丙"], page=None),
                ],
                kind="layout",
            ),
            _table(
                "data#table1",
                [
                    _row("nd", 0, ["项目", "金额"], page=None, is_header=True),
                    _row("nd", 1, ["甲", "10"], page=None),
                ],
                column_count=2,
                table_index=2,
            ),
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#layout",
                [
                    _row("rl", 0, ["A", "B", "C"], page=1, bbox=True, is_header=True),
                    _row("rl", 1, ["甲", "乙", "丙"], page=1, bbox=True),
                ],
                page=1,
                kind="layout",
            ),
            _table(
                "render#data",
                [
                    _row("rd", 0, ["项目", "金额"], page=1, bbox=True, is_header=True),
                    _row("rd", 1, ["甲", "10"], page=1, bbox=True),
                ],
                page=1,
                column_count=2,
                table_index=2,
            ),
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.expected_rows == {"data#table1": {0, 1}}
    assert result.mapped_rows == {"data#table1": {0, 1}}
    assert [table.table_id for table in result.page_tables[1]] == ["data#table1"]
    assert result.unresolved == []


def test_native_blank_render_text_conflict_is_not_overwritten() -> None:
    native = _structure(
        "html",
        [
            _table(
                "blank#table1",
                [
                    _row("nb", 0, ["项目", "金额", "备注"], page=None, is_header=True),
                    _row("nb", 1, ["甲", "", "首行"], page=None),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#blank",
                [
                    _row("rb", 0, ["项目", "金额", "备注"], page=1, bbox=True, is_header=True),
                    _row("rb", 1, ["甲", "20", "首行"], page=1, bbox=True),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.page_tables == {}
    assert result.mapped_rows == {"blank#table1": set()}
    assert any(
        item["row"] == 1 and item["col"] == 1 and item["reason"] == "native_blank_render_conflict"
        for item in result.unresolved
    )
    assert native.tables[0].rows[1].cells[1].text == ""


def test_blank_cell_copies_only_its_own_render_bbox() -> None:
    native = _structure(
        "html",
        [
            _table(
                "blank#table1",
                [
                    _row("nb", 0, ["项目", "金额", "备注"], page=None, is_header=True),
                    _row("nb", 1, ["甲", "", "首行"], page=None),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#blank",
                [
                    _row("rb", 0, ["项目", "金额", "备注"], page=1, bbox=True, is_header=True),
                    _row("rb", 1, ["甲", "", "首行"], page=1, bbox=True),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    table = result.page_tables[1][0]
    assert table.rows[1].cells[1].text == ""
    assert table.rows[1].cells[1].bbox == _render_bbox(1, 1, 1)
    assert table.rows[1].cells[1].bbox != _render_bbox(1, 0, 1)
    assert table.rows[1].cells[1].bbox != _render_bbox(1, 2, 1)
    assert result.unresolved == []


def test_blank_cell_without_render_bbox_is_unresolved() -> None:
    native = _structure(
        "html",
        [
            _table(
                "blank#table1",
                [
                    _row("nb", 0, ["项目", "金额", "备注"], page=None, is_header=True),
                    _row("nb", 1, ["甲", "", "首行"], page=None),
                ],
            )
        ],
    )
    render_row = _row("rb", 1, ["甲", "", "首行"], page=1, bbox=True)
    render_row.cells[1].bbox = None
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#blank",
                [
                    _row("rb", 0, ["项目", "金额", "备注"], page=1, bbox=True, is_header=True),
                    render_row,
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    table = result.page_tables[1][0]
    assert table.rows[1].cells[1].bbox is None
    assert result.mapped_rows == {"blank#table1": {0, 1}}
    assert any(
        item["row"] == 1 and item["col"] == 1 and item["reason"] == "render_bbox_missing"
        for item in result.unresolved
    )


def test_missing_render_page_size_blocks_mapping() -> None:
    native = _structure(
        "html",
        [
            _table(
                "page#table1",
                [
                    _row("np", 0, ["项目", "金额"], page=None, is_header=True),
                    _row("np", 1, ["甲", "10"], page=None),
                ],
                column_count=2,
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#page",
                [
                    _row("rp", 0, ["项目", "金额"], page=1, bbox=True, is_header=True),
                    _row("rp", 1, ["甲", "10"], page=1, bbox=True),
                ],
                page=1,
                column_count=2,
            )
        ],
        pages=[],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.page_tables == {}
    assert result.mapped_rows == {"page#table1": set()}
    assert any(
        item["row"] is None and item["reason"] == "render_page_size_missing"
        for item in result.unresolved
    )


def test_cross_page_rowspan_keeps_covered_origin_context() -> None:
    native = _structure(
        "html",
        [
            _table(
                "span#table1",
                [
                    _row("ns", 0, ["职务", "姓名", "支部"], page=None, is_header=True),
                    _row(
                        "ns",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=None,
                        spans={0: (2, 1)},
                    ),
                    _row(
                        "ns",
                        2,
                        ["", "赫圣博", "二支部"],
                        page=None,
                        spans={0: (2, 1)},
                        covered={0: (1, 0)},
                    ),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#p1",
                [
                    _row("r1", 0, ["职务", "姓名", "支部"], page=1, bbox=True, is_header=True),
                    _row(
                        "r1",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=1,
                        bbox=True,
                        spans={0: (2, 1)},
                    ),
                ],
                page=1,
                table_index=1,
            ),
            _table(
                "render#p2",
                [
                    _row("r2", 0, ["职务", "姓名", "支部"], page=2, bbox=True, is_header=True),
                    _row("r2", 1, ["组织委员", "赫圣博", "二支部"], page=2, bbox=True),
                ],
                page=2,
                table_index=2,
            ),
        ],
        pages=[_page(1), _page(2)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert list(result.page_tables) == [1, 2]
    page_two = result.page_tables[2][0]
    assert [row.row_index for row in page_two.rows] == [0, 2]
    covered = page_two.rows[1].cells[0]
    assert covered.covered is True
    assert covered.bbox is None
    assert (covered.origin_row, covered.origin_col) == (1, 0)
    assert page_two.rows[1].cells[1].bbox == _render_bbox(1, 1, 2)
    assert result.expected_rows == {"span#table1": {0, 1, 2}}
    assert result.mapped_rows == {"span#table1": {0, 1, 2}}
    assert result.unresolved == []


def test_rowspan_mismatch_is_unresolved() -> None:
    native = _structure(
        "html",
        [
            _table(
                "span#table1",
                [
                    _row("ns", 0, ["职务", "姓名", "支部"], page=None, is_header=True),
                    _row(
                        "ns",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=None,
                        spans={0: (2, 1)},
                    ),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#span",
                [
                    _row("rs", 0, ["职务", "姓名", "支部"], page=1, bbox=True, is_header=True),
                    _row("rs", 1, ["组织委员", "梁志辉", "一支部"], page=1, bbox=True),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.page_tables == {}
    assert result.mapped_rows == {"span#table1": set()}
    assert any(
        item["row"] == 1 and item["col"] == 0 and item["reason"] == "span_mismatch"
        for item in result.unresolved
    )


def test_same_page_covered_origin_mismatch_is_unresolved() -> None:
    native = _structure(
        "html",
        [
            _table(
                "span#table1",
                [
                    _row("ns", 0, ["职务", "姓名", "支部"], page=None, is_header=True),
                    _row(
                        "ns",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=None,
                        spans={0: (2, 1)},
                    ),
                    _row(
                        "ns",
                        2,
                        ["", "赫圣博", "二支部"],
                        page=None,
                        spans={0: (2, 1)},
                        covered={0: (1, 0)},
                    ),
                ],
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#span",
                [
                    _row("rs", 0, ["职务", "姓名", "支部"], page=1, bbox=True, is_header=True),
                    _row(
                        "rs",
                        1,
                        ["组织委员", "梁志辉", "一支部"],
                        page=1,
                        bbox=True,
                        spans={0: (2, 1)},
                    ),
                    _row(
                        "rs",
                        2,
                        ["", "赫圣博", "二支部"],
                        page=1,
                        bbox=True,
                        spans={0: (2, 1)},
                        covered={0: (0, 0)},
                    ),
                ],
                page=1,
            )
        ],
        pages=[_page(1)],
    )

    result = build_html_grid_geometry(native, rendered)

    assert result.mapped_rows == {"span#table1": {0, 1, 2}}
    assert result.page_tables[1][0].rows[2].cells[0].origin_row == 1
    assert any(
        item["row"] == 2 and item["col"] == 0 and item["reason"] == "covered_origin_mismatch"
        for item in result.unresolved
    )


def test_merged_origin_covered_cells_keep_five_columns() -> None:
    def merged_row(
        prefix: str,
        row_index: int,
        label: str,
        value: str,
        *,
        page: int | None = None,
        is_header: bool = False,
    ) -> TableRow:
        return _row(
            prefix,
            row_index,
            [label, value, "", "", ""],
            page=page,
            bbox=page is not None,
            is_header=is_header,
            spans={1: (1, 4), 2: (1, 4), 3: (1, 4), 4: (1, 4)},
            covered={
                2: (row_index, 1),
                3: (row_index, 1),
                4: (row_index, 1),
            },
        )

    native = _structure(
        "html",
        [
            _table(
                "merged#table1",
                [
                    merged_row("n", 0, "字段", "姓名：示例", is_header=True),
                    merged_row("n", 1, "职称", "技术职称：教授"),
                ],
                column_count=5,
            )
        ],
    )
    rendered = _structure(
        "pdf",
        [
            _table(
                "render#merged",
                [
                    merged_row("r", 0, "字段", "姓名：示例", page=1, is_header=True),
                    merged_row("r", 1, "职称", "技术职称：教授", page=1),
                ],
                page=1,
                column_count=5,
            )
        ],
        pages=[_page(1)],
    )
    before = native.model_dump()

    result = build_html_grid_geometry(native, rendered)

    assert native.model_dump() == before
    assert result.unresolved == []
    assert result.mapped_rows == {"merged#table1": {0, 1}}
    table = result.page_tables[1][0]
    assert table.column_count == 5
    assert [cell.col for cell in table.rows[0].cells] == [0, 1, 2, 3, 4]
    assert table.rows[0].cells[1].bbox == _render_bbox(0, 1, 1)
    assert table.rows[0].cells[4].covered is True
    assert table.rows[0].cells[4].colspan == 4
    assert table.rows[1].cells[1].bbox == _render_bbox(1, 1, 1)


def test_out_of_range_origin_span_is_rejected() -> None:
    native = _structure(
        "html",
        [
            _table(
                "wide#table1",
                [
                    _row(
                        "nw",
                        0,
                        ["A", "B", "C", "D", "E"],
                        page=None,
                        is_header=True,
                        spans={1: (1, 5)},
                    )
                ],
                column_count=5,
            )
        ],
    )

    with pytest.raises(ValueError, match="column_count"):
        build_html_grid_geometry(native, _structure("pdf", []))


def test_orphan_covered_cell_is_rejected() -> None:
    native = _structure(
        "html",
        [
            _table(
                "orphan#table1",
                [
                    _row("no", 0, ["A", "B", "C", "D", "E"], page=None, is_header=True),
                    _row(
                        "no",
                        1,
                        ["甲", "乙", "丙", "", ""],
                        page=None,
                        spans={3: (1, 2), 4: (1, 2)},
                        covered={3: (9, 1), 4: (9, 1)},
                    ),
                ],
                column_count=5,
            )
        ],
    )

    with pytest.raises(ValueError, match="origin"):
        build_html_grid_geometry(native, _structure("pdf", []))


def test_rejects_wrong_structure_formats() -> None:
    with pytest.raises(ValueError, match="html"):
        build_html_grid_geometry(_structure("pdf", []), _structure("pdf", []))
