"""Real-PDF tests for the independent native source audit.

Every fixture PDF is generated with PyMuPDF and text is re-extracted from the
saved file; no clip return value is stubbed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pymupdf
import pytest

from campusguide.ingest.native_source import (
    EXTRACTOR_METHOD,
    NativeSourceAudit,
    audit_pdf_grid_cells,
)
from campusguide.ingest.structure import (
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)

PAGE_WIDTH = 300.0
PAGE_HEIGHT = 300.0
TABLE_ID = "pdf#table1"
VALUE_BBOX = [40.0, 40.0, 120.0, 85.0]
EMPTY_BBOX = [140.0, 40.0, 220.0, 85.0]


def _make_pdf(path: Path, draw: Callable[[pymupdf.Page], None]) -> str:
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    draw(page)
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _draw_48(page: pymupdf.Page) -> None:
    page.insert_text((50, 66), "48", fontname="helv", fontsize=12)


def _cell(
    row: int,
    col: int,
    text: str,
    *,
    bbox: list[float] | None = None,
    covered: bool = False,
    origin_row: int | None = None,
    origin_col: int | None = None,
    rowspan: int = 1,
    colspan: int = 1,
    needs_review: bool = False,
    table_id: str = TABLE_ID,
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
        needs_review=needs_review,
        bbox=bbox,
    )


def _row(
    row_index: int,
    cells: list[TableCell],
    *,
    table_id: str = TABLE_ID,
    is_header: bool = False,
) -> TableRow:
    return TableRow(
        row_id=f"{table_id}#row{row_index}",
        row_index=row_index,
        is_header=is_header,
        cells=cells,
        text=" | ".join(cell.text for cell in cells),
        locator=SourceLocator(format="pdf", page=1),
    )


def _table(
    rows: list[TableRow],
    *,
    table_id: str = TABLE_ID,
    page: int = 1,
    kind: Literal["data", "layout", "matrix"] = "data",
    table_index: int = 1,
) -> TableStructure:
    return TableStructure(
        table_id=table_id,
        block_id=f"block-{table_id}",
        table_index=table_index,
        page=page,
        rows=rows,
        header_row_indices=[row.row_index for row in rows if row.is_header],
        column_count=max((len(row.cells) for row in rows), default=0),
        row_count=len(rows),
        kind=kind,
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=page),
    )


def _simple_table(text: str, bbox: list[float] | None) -> TableStructure:
    return _table([_row(0, [_cell(0, 0, text, bbox=bbox)])])


def test_matching_cell_is_not_flagged(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    native = _simple_table("48", VALUE_BBOX)
    before = native.model_dump()

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert isinstance(audit, NativeSourceAudit)
    assert audit.source_path == pdf
    assert audit.source_sha256 == sha
    assert audit.page_no == 1
    assert audit.page_size == (PAGE_WIDTH, PAGE_HEIGHT)
    assert audit.has_text_layer is True
    assert [item["status"] for item in audit.checked_cells] == ["source_text_match"]
    assert "".join(audit.checked_cells[0]["clip_text"].split()) == "48"
    assert audit.checked_cells[0]["bbox"] == VALUE_BBOX
    assert audit.mismatches == []
    assert audit.warnings == []
    assert audit.tables[0] is not native
    assert audit.tables[0].rows[0].cells[0].needs_review is False
    assert native.model_dump() == before


def test_candidate_value_different_from_source_is_marked_for_review(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    native = _simple_table("84", VALUE_BBOX)
    before = native.model_dump()

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert audit.checked_cells[0]["status"] == "source_clip_mismatch"
    mismatch = audit.mismatches[0]
    assert mismatch["reason"] == "source_clip_mismatch"
    assert mismatch["id"] == f"{TABLE_ID}#r0c0"
    assert mismatch["row"] == 0
    assert mismatch["col"] == 0
    assert mismatch["native_text"] == "84"
    assert "".join(mismatch["clip_text"].split()) == "48"
    assert mismatch["bbox"] == VALUE_BBOX
    assert mismatch["page"] == 1
    assert mismatch["source_sha256"] == sha
    assert mismatch["extractor"] == EXTRACTOR_METHOD

    copied = audit.tables[0].rows[0].cells[0]
    assert copied.needs_review is True
    assert copied.text == "84"
    assert native.rows[0].cells[0].needs_review is False
    assert native.model_dump() == before


def test_source_sha_and_page_fail_closed(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    native = _simple_table("48", VALUE_BBOX)

    with pytest.raises(ValueError, match="SHA-256"):
        audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256="0" * 64)
    with pytest.raises(ValueError, match="64 位十六进制"):
        audit_pdf_grid_cells(
            pdf, page_no=1, tables=[native], source_sha256="not-a-sha256"
        )
    with pytest.raises(ValueError, match="超出 PDF 页数"):
        audit_pdf_grid_cells(pdf, page_no=2, tables=[native], source_sha256=sha)
    with pytest.raises(ValueError, match="正整数"):
        audit_pdf_grid_cells(pdf, page_no=0, tables=[native], source_sha256=sha)
    with pytest.raises(ValueError, match="正整数"):
        audit_pdf_grid_cells(pdf, page_no=True, tables=[native], source_sha256=sha)


def test_missing_or_invalid_bbox_is_unresolved(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    row = _row(
        0,
        [
            _cell(0, 0, "48", bbox=None),
            _cell(0, 1, "48", bbox=[400.0, 400.0, 500.0, 500.0]),
            _cell(0, 2, "48", bbox=[10.0, 10.0, 10.0, 30.0]),
            _cell(0, 3, "", bbox=None),
        ],
    )
    native = _table([row])
    before = native.model_dump()

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert [item["status"] for item in audit.checked_cells] == [
        "unresolved_bbox",
        "unresolved_bbox",
        "unresolved_bbox",
        "skipped_no_bbox",
    ]
    cells = audit.tables[0].rows[0].cells
    assert [cell.needs_review for cell in cells] == [True, True, True, False]
    assert {item["reason"] for item in audit.warnings} == {
        "cell_bbox_missing",
        "cell_bbox_invalid",
    }
    assert audit.mismatches == []
    assert native.model_dump() == before


def test_covered_cell_is_not_a_fact_or_checked(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    origin = _cell(0, 0, "48", bbox=VALUE_BBOX, rowspan=2)
    covered = _cell(
        1,
        0,
        "",
        bbox=[900.0, 900.0, 1000.0, 1000.0],
        covered=True,
        origin_row=0,
        origin_col=0,
        rowspan=2,
    )
    native = _table([_row(0, [origin]), _row(1, [covered])])

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert [item["status"] for item in audit.checked_cells] == [
        "source_text_match",
        "skipped_covered",
    ]
    assert audit.checked_cells[1]["bbox"] is None
    assert audit.checked_cells[1]["clip_text"] is None
    assert audit.tables[0].rows[1].cells[0].needs_review is False
    assert audit.mismatches == []
    assert audit.warnings == []


def test_line_break_and_whitespace_are_equivalent(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"

    def draw(page: pymupdf.Page) -> None:
        page.insert_text((50, 60), "1", fontname="helv", fontsize=12)
        page.insert_text((50, 80), "2", fontname="helv", fontsize=12)

    sha = _make_pdf(pdf, draw)
    bbox = [40.0, 40.0, 120.0, 95.0]
    native = _table(
        [
            _row(0, [_cell(0, 0, "12", bbox=bbox)]),
            _row(1, [_cell(1, 0, "1 2", bbox=bbox)]),
            _row(2, [_cell(2, 0, "1\n2", bbox=bbox)]),
        ]
    )

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert [item["status"] for item in audit.checked_cells] == ["source_text_match"] * 3
    assert [
        "".join(item["clip_text"].split()) for item in audit.checked_cells
    ] == ["12", "12", "12"]
    assert audit.mismatches == []
    assert audit.warnings == []


def test_reordered_characters_are_not_equivalent(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"

    def draw(page: pymupdf.Page) -> None:
        page.insert_text((50, 66), "21", fontname="helv", fontsize=12)

    sha = _make_pdf(pdf, draw)
    native = _simple_table("12", VALUE_BBOX)

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert audit.checked_cells[0]["status"] == "source_clip_mismatch"
    assert audit.mismatches[0]["native_text"] == "12"
    assert "".join(audit.mismatches[0]["clip_text"].split()) == "21"
    assert audit.tables[0].rows[0].cells[0].needs_review is True
    assert audit.tables[0].rows[0].cells[0].text == "12"


def test_blank_native_cell_with_source_text_is_not_ignored(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    native = _table(
        [
            _row(
                0,
                [
                    _cell(0, 0, "", bbox=VALUE_BBOX),
                    _cell(0, 1, "   ", bbox=VALUE_BBOX),
                ],
            )
        ]
    )

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert [item["status"] for item in audit.checked_cells] == [
        "source_clip_mismatch",
        "source_clip_mismatch",
    ]
    assert [item["native_text"] for item in audit.mismatches] == ["", "   "]
    assert [
        "".join(item["clip_text"].split()) for item in audit.mismatches
    ] == ["48", "48"]
    assert all(cell.needs_review for cell in audit.tables[0].rows[0].cells)


def test_page_without_text_layer_is_unresolved_not_equal(tmp_path: Path) -> None:
    pdf = tmp_path / "scanned.pdf"

    def draw(page: pymupdf.Page) -> None:
        page.draw_rect(pymupdf.Rect(20, 20, 280, 280), color=(0, 0, 0), width=1)

    sha = _make_pdf(pdf, draw)
    native = _simple_table("48", VALUE_BBOX)
    before = native.model_dump()

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert audit.has_text_layer is False
    assert [item["status"] for item in audit.checked_cells] == [
        "unresolved_no_text_layer"
    ]
    assert audit.checked_cells[0]["clip_text"] is None
    assert [item["reason"] for item in audit.warnings] == ["page_text_layer_missing"]
    assert audit.mismatches == []
    cell = audit.tables[0].rows[0].cells[0]
    assert cell.text == "48"
    assert cell.needs_review is True
    assert native.rows[0].cells[0].needs_review is False
    assert native.model_dump() == before


def test_empty_clip_with_native_text_is_pending_on_text_layer_page(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "source.pdf"

    def draw(page: pymupdf.Page) -> None:
        page.insert_text((230, 240), "outside", fontname="helv", fontsize=12)

    sha = _make_pdf(pdf, draw)
    native = _table(
        [
            _row(
                0,
                [
                    _cell(0, 0, "48", bbox=VALUE_BBOX),
                    _cell(0, 1, "", bbox=EMPTY_BBOX),
                ],
            )
        ]
    )

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert audit.has_text_layer is True
    assert [item["status"] for item in audit.checked_cells] == [
        "source_clip_mismatch",
        "source_text_empty",
    ]
    assert audit.mismatches[0]["native_text"] == "48"
    assert audit.mismatches[0]["clip_text"] == ""
    assert audit.tables[0].rows[0].cells[0].needs_review is True
    assert audit.tables[0].rows[0].cells[1].needs_review is False


def test_only_current_page_non_layout_tables_are_returned(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    # 外部改表 ID 时，真实单元格 ID 仍来自原表；审计不得重写 cell_id。
    data = _table(
        [_row(0, [_cell(0, 0, "48", bbox=VALUE_BBOX)])],
    ).model_copy(update={"table_id": "pdf#data"})
    layout = _table(
        [_row(0, [_cell(0, 0, "48", bbox=VALUE_BBOX)], table_id="pdf#layout")],
        table_id="pdf#layout",
        kind="layout",
    )
    other_page = _table(
        [_row(0, [_cell(0, 0, "48", bbox=VALUE_BBOX)], table_id="pdf#page2")],
        table_id="pdf#page2",
        page=2,
        table_index=2,
    )

    audit = audit_pdf_grid_cells(
        pdf,
        page_no=1,
        tables=[data, layout, other_page],
        source_sha256=sha,
    )

    assert [table.table_id for table in audit.tables] == ["pdf#data"]
    assert [item["id"] for item in audit.checked_cells] == ["pdf#table1#r0c0"]
    assert audit.checked_cells[0]["status"] == "source_text_match"


def test_existing_needs_review_is_preserved_when_source_matches(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    sha = _make_pdf(pdf, _draw_48)
    native = _table(
        [_row(0, [_cell(0, 0, "48", bbox=VALUE_BBOX, needs_review=True)])]
    )

    audit = audit_pdf_grid_cells(pdf, page_no=1, tables=[native], source_sha256=sha)

    assert audit.checked_cells[0]["status"] == "source_text_match"
    assert audit.mismatches == []
    assert audit.tables[0].rows[0].cells[0].needs_review is True
    assert native.rows[0].cells[0].needs_review is True


def test_missing_source_or_non_pdf_fails(tmp_path: Path) -> None:
    native = _simple_table("48", VALUE_BBOX)
    missing = tmp_path / "missing.pdf"

    with pytest.raises(FileNotFoundError):
        audit_pdf_grid_cells(
            missing, page_no=1, tables=[native], source_sha256="0" * 64
        )

    bad = tmp_path / "bad.pdf"
    bad.write_text("not a pdf", encoding="utf-8")
    bad_sha = hashlib.sha256(bad.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        audit_pdf_grid_cells(bad, page_no=1, tables=[native], source_sha256=bad_sha)
