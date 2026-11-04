"""Offline tests for the source-native expectation adapter."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from campusguide.ingest.native_source import NativeSourceAudit
from campusguide.ingest.ocr_layout import (
    LayoutDetection,
    PageImageEvidence,
    RegionCrop,
    build_layout,
    crop_layout_region,
)
from campusguide.ingest.ocr_native_expectation import (
    NativeExpectationAudit,
    NativeExpectationValidationError,
    build_native_expectation,
)
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.structure import (
    ParsedStructure,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)

ROOT = Path(__file__).resolve().parents[2]
ROWS = [
    ["课程", "学分", "成绩", "备注"],
    ["高等数学", "4", "85", ""],
    ["大学物理", "3", "M", ""],
    ["线性代数", "2", "H", ""],
    ["概率论", "2", "L", ""],
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pdf(tmp_path: Path, *, rotation: int = 0) -> tuple[Path, Path]:
    pdf_path = tmp_path / f"source-{rotation}.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((20, 30), f"native table {rotation}")
    if rotation:
        page.set_rotation(rotation)
    doc.save(pdf_path)
    doc.close()
    png_path = tmp_path / f"page-{rotation}.png"
    doc = pymupdf.open(pdf_path)
    doc[0].get_pixmap(dpi=144).save(png_path)
    doc.close()
    return pdf_path, png_path


def _table(
    rows: list[list[str]],
    *,
    table_id: str = "t1",
    bbox: tuple[float, float, float, float] = (50.0, 50.0, 250.0, 200.0),
) -> TableStructure:
    row_height = (bbox[3] - bbox[1]) / len(rows)
    col_width = (bbox[2] - bbox[0]) / len(rows[0])
    built: list[TableRow] = []
    for row_index, values in enumerate(rows):
        cells: list[TableCell] = []
        for col_index, text in enumerate(values):
            x0 = bbox[0] + col_index * col_width
            y0 = bbox[1] + row_index * row_height
            cells.append(
                TableCell(
                    cell_id=f"{table_id}#r{row_index}c{col_index}",
                    row=row_index,
                    col=col_index,
                    origin_row=row_index,
                    origin_col=col_index,
                    text=text,
                    native_text=text,
                    text_source="native" if text else None,
                    is_header=row_index == 0,
                    bbox=[x0, y0, x0 + col_width, y0 + row_height],
                )
            )
        built.append(
            TableRow(
                row_id=f"{table_id}#row{row_index}",
                row_index=row_index,
                is_header=row_index == 0,
                cells=cells,
                locator=SourceLocator(format="pdf", page=1),
            )
        )
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}-block",
        table_index=0,
        page=1,
        bbox=list(bbox),
        rows=built,
        header_row_indices=[0],
        column_count=len(rows[0]),
        row_count=len(rows),
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=1),
    )


def _fake_audit(
    sha: str,
    *,
    statuses: dict[str, str] | None = None,
    entry_overrides: dict[str, dict[str, Any]] | None = None,
    has_text_layer: bool = True,
) -> Callable[..., NativeSourceAudit]:
    calls: list[tuple[int, int]] = []

    def audit(
        source_path: Path,
        *,
        page_no: int,
        tables: list[TableStructure],
        source_sha256: str,
    ) -> NativeSourceAudit:
        assert source_sha256 == sha
        calls.append((page_no, len(tables)))
        overrides = statuses or {}
        entries: list[dict[str, Any]] = []
        for table in tables:
            for row in table.rows:
                for cell in row.cells:
                    status = (
                        "skipped_covered"
                        if cell.covered
                        else overrides.get(
                            cell.cell_id,
                            "source_text_match"
                            if cell.text.strip()
                            else "source_text_empty",
                        )
                    )
                    entry = {
                        "table_id": table.table_id,
                        "id": cell.cell_id,
                        "row": cell.row,
                        "col": cell.col,
                        "page": page_no,
                        "covered": cell.covered,
                        "native_text": cell.text,
                        "clip_text": None,
                        "bbox": list(cell.bbox) if cell.bbox else None,
                        "status": status,
                        "source_sha256": source_sha256,
                        "extractor": "test",
                    }
                    entry.update((entry_overrides or {}).get(cell.cell_id, {}))
                    entries.append(entry)
        return NativeSourceAudit(
            source_path=source_path,
            source_sha256=source_sha256,
            page_no=page_no,
            page_width=300.0,
            page_height=300.0,
            has_text_layer=has_text_layer,
            tables=list(tables),
            checked_cells=entries,
            mismatches=[],
            warnings=[],
        )

    audit.calls = calls  # type: ignore[attr-defined]
    return audit


def _fixture(
    tmp_path: Path,
    *,
    rotation: int = 0,
    label: str = "table",
    tables: list[TableStructure] | None = None,
    statuses: dict[str, str] | None = None,
    entry_overrides: dict[str, dict[str, Any]] | None = None,
    has_text_layer: bool = True,
) -> tuple[Path, ParsedDocument, RegionCrop, Callable[..., NativeSourceAudit]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf_path, png_path = _pdf(tmp_path, rotation=rotation)
    sha = _sha(pdf_path)
    page = PageImageEvidence.from_png(
        doc_id="doc-1",
        version_id="v1",
        source_format="pdf",
        source_sha256=sha,
        page=1,
        image_path=png_path,
    )
    layout = build_layout(
        page,
        [
            LayoutDetection(
                label=label,
                score=0.9,
                bbox_px=[100.0, 100.0, 500.0, 400.0],
                order=0,
            )
        ],
        detector_id="test-layout",
        detector_revision="rev-1",
    )
    crop = crop_layout_region(
        page, layout.regions[0], output_dir=tmp_path, padding_px=0
    )
    built = tables if tables is not None else [_table(ROWS)]
    parsed = ParsedDocument(
        doc_id="doc-1",
        version_id="v1",
        title="probe",
        text="",
        format="pdf",
        source_sha256=sha,
        parser_version="test",
        page_count=1,
        structure=ParsedStructure(format="pdf", tables=built),
    )
    return pdf_path, parsed, crop, _fake_audit(
        sha,
        statuses=statuses,
        entry_overrides=entry_overrides,
        has_text_layer=has_text_layer,
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch, audit: Callable[..., NativeSourceAudit]
) -> None:
    monkeypatch.setattr(
        "campusguide.ingest.ocr_native_expectation.audit_pdf_grid_cells", audit
    )


def test_ready_source_bound_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path, parsed, crop, audit = _fixture(tmp_path)
    _patch(monkeypatch, audit)
    before = (parsed.model_dump(mode="json"), crop.model_dump(mode="json"))

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert isinstance(result, NativeExpectationAudit)
    assert (parsed.model_dump(mode="json"), crop.model_dump(mode="json")) == before
    assert audit.calls == [(1, 1)]  # type: ignore[attr-defined]
    assert result.status == "ready"
    assert result.needs_review is False
    assert result.reasons == []
    expectation = result.expectation
    assert expectation is not None
    assert expectation.source_sha256 == crop.page.source_sha256
    assert expectation.page_image_sha256 == crop.page.image_sha256
    assert expectation.page == crop.page.page
    assert expectation.region_id == crop.region.region_id
    assert expectation.crop_sha256 == crop.image_sha256
    assert expectation.expected_table_column_count == 4
    assert expectation.min_table_rows == 5
    values = {
        (check.row_key, check.col): check.value
        for check in expectation.numeric_checks
    }
    assert len(values) == 12
    assert all(
        check.key_col == 0 and check.col != 0
        for check in expectation.numeric_checks
    )
    assert values[("高等数学", 2)] == "85"
    assert values[("高等数学", 3)] == ""
    assert values[("大学物理", 2)] == "M"
    assert values[("线性代数", 2)] == "H"
    assert values[("概率论", 2)] == "L"
    assert result.audit["diagnostic_only"] is True
    assert result.audit["not_truth"] is True
    assert result.audit["native_agreement_is_not_accuracy_guarantee"] is True
    assert result.audit["units"] == {
        "table_bbox": "pdf_points",
        "region_bbox": "parent_pixels",
        "cell_bbox": "pdf_points",
    }
    assert result.audit["table_id"] == "t1"
    assert result.audit["key_col"] == 0
    assert result.audit["key_rows"] == 4
    assert result.audit["checked_count"] == 12
    assert result.audit["skipped_count"] == 0
    assert result.audit["skipped_rows"] == 0
    assert result.audit["iou"] == 1.0
    assert result.audit["table_bbox_pdf_points"] == [50.0, 50.0, 250.0, 200.0]
    assert result.audit["table_bbox_parent_pixels"] == [100.0, 100.0, 500.0, 400.0]
    assert result.audit["reason_codes"] == []
    expected_hash = hashlib.sha256(
        expectation.model_dump_json().encode("utf-8")
    ).hexdigest()
    assert result.audit["expectation_sha256"] == expected_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "1.0"),
        ("doc_id", "other"),
        ("version_id", "other"),
        ("source_sha256", "f" * 64),
        ("format", "html"),
        ("page_count", 0),
        ("page_count", True),
        ("ocr_used", 1),
    ],
)
def test_identity_errors_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    pdf_path, parsed, crop, audit = _fixture(tmp_path)
    _patch(monkeypatch, audit)
    forged = parsed.model_copy(update={field: value})

    with pytest.raises(NativeExpectationValidationError):
        build_native_expectation(crop, source_path=pdf_path, parsed=forged)

    assert audit.calls == []  # type: ignore[attr-defined]


def test_untyped_inputs_fail_closed(tmp_path: Path) -> None:
    pdf_path, parsed, crop, _audit = _fixture(tmp_path)
    with pytest.raises(NativeExpectationValidationError):
        build_native_expectation(  # type: ignore[arg-type]
            object(), source_path=pdf_path, parsed=parsed
        )
    with pytest.raises(NativeExpectationValidationError):
        build_native_expectation(  # type: ignore[arg-type]
            crop, source_path=str(pdf_path), parsed=parsed
        )
    with pytest.raises(NativeExpectationValidationError):
        build_native_expectation(  # type: ignore[arg-type]
            crop, source_path=pdf_path, parsed=object()
        )


def test_changed_source_sha_fails_closed(tmp_path: Path) -> None:
    _pdf_path, parsed, crop, _audit = _fixture(tmp_path)
    other_path = tmp_path / "other.pdf"
    doc = pymupdf.open()
    doc.new_page(width=300, height=300).insert_text((10, 10), "other")
    doc.save(other_path)
    doc.close()

    with pytest.raises(ValueError):
        build_native_expectation(crop, source_path=other_path, parsed=parsed)


def test_stale_page_png_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path, parsed, crop, audit = _fixture(tmp_path)
    _patch(monkeypatch, audit)
    crop.page.image_path.write_bytes(b"changed")

    with pytest.raises(ValueError):
        build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert audit.calls == []  # type: ignore[attr-defined]


def test_ambiguous_match_needs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = [_table(ROWS, table_id="t1"), _table(ROWS, table_id="t2")]
    pdf_path, parsed, crop, audit = _fixture(tmp_path, tables=tables)
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "needs_review"
    assert result.needs_review is True
    assert result.expectation is None
    assert result.reasons == ["ambiguous_native_table_match"]
    assert len(result.audit["table_matches"]) == 2
    assert result.audit["reason_codes"] == ["ambiguous_native_table_match"]


def test_clip_mismatch_cell_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = {"t1#r1c2": "source_clip_mismatch"}
    pdf_path, parsed, crop, audit = _fixture(tmp_path, statuses=statuses)
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "ready"
    assert result.expectation is not None
    values = {
        (check.row_key, check.col): check.value
        for check in result.expectation.numeric_checks
    }
    assert ("高等数学", 2) not in values
    assert result.audit["checked_count"] == 11
    assert result.audit["skipped_count"] == 1
    assert result.audit["skipped_reasons"] == {"source_clip_mismatch": 1}


def test_duplicate_aliased_cell_id_never_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [["课程", "成绩", "备注"], ["高等数学", "85", ""]]
    table = _table(rows)
    table.rows[1].cells[1].cell_id = table.rows[1].cells[2].cell_id
    pdf_path, parsed, crop, audit = _fixture(tmp_path, tables=[table])
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["native_table_geometry_malformed"]
    assert result.audit["malformed_table_ids"] == ["t1"]


@pytest.mark.parametrize("cell_id", ["", "   "])
def test_empty_cell_id_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cell_id: str
) -> None:
    table = _table(ROWS)
    table.rows[1].cells[1].cell_id = cell_id
    pdf_path, parsed, crop, audit = _fixture(tmp_path, tables=[table])
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["native_table_geometry_malformed"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row", 2),
        ("col", 3),
        ("native_text", "85"),
        ("bbox", [0.0, 0.0, 50.0, 50.0]),
    ],
)
def test_misbound_audit_entry_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    table = _table(ROWS)
    table.rows[1].cells[2].text = "999"
    pdf_path, parsed, crop, audit = _fixture(
        tmp_path,
        tables=[table],
        entry_overrides={"t1#r1c2": {field: value}},
    )
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "ready"
    expectation = result.expectation
    assert expectation is not None
    values = {
        (check.row_key, check.col): check.value
        for check in expectation.numeric_checks
    }
    assert ("高等数学", 2) not in values
    assert result.audit["checked_count"] == 11
    assert result.audit["skipped_count"] == 1
    assert result.audit["skipped_reasons"] == {"source_text_match": 1}


def test_non_table_region_unavailable_before_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table_pdf, table_parsed, table_crop, table_audit = _fixture(
        tmp_path / "table", label="table"
    )
    text_pdf, text_parsed, text_crop, text_audit = _fixture(
        tmp_path / "text", label="text"
    )
    _patch(monkeypatch, table_audit)
    table_result = build_native_expectation(
        table_crop, source_path=table_pdf, parsed=table_parsed
    )
    _patch(monkeypatch, text_audit)

    result = build_native_expectation(
        text_crop, source_path=text_pdf, parsed=text_parsed
    )

    assert text_crop.region.bbox_px == table_crop.region.bbox_px
    assert text_crop.crop_bbox_px == table_crop.crop_bbox_px
    assert table_result.status == "ready"
    assert table_result.expectation is not None
    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["unsupported_region_kind"]
    assert result.audit["region_id"] == text_crop.region.region_id
    assert text_audit.calls == []  # type: ignore[attr-defined]


def test_scanned_page_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path, parsed, crop, audit = _fixture(tmp_path, has_text_layer=False)
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.needs_review is False
    assert result.expectation is None
    assert result.reasons == ["page_text_layer_missing"]
    assert result.audit["has_text_layer"] is False


@pytest.mark.parametrize("mutation", ["source", "vision", "text_source"])
def test_model_derived_table_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    table = _table(ROWS)
    if mutation == "source":
        table = table.model_copy(update={"source": "vision"})
    elif mutation == "vision":
        table = table.model_copy(update={"vision": {"backend": "test"}})
    else:
        table.rows[0].cells[0].text_source = "vision"
    pdf_path, parsed, crop, audit = _fixture(tmp_path, tables=[table])
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["no_native_data_tables"]


def test_rotated_page_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path, parsed, crop, audit = _fixture(tmp_path, rotation=90)
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["unsupported_pdf_rotation"]


def test_no_reliable_key_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [["课程", "备注"], ["", ""], ["", ""]]
    pdf_path, parsed, crop, audit = _fixture(tmp_path, tables=[_table(rows)])
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["no_reliable_key_column"]


def test_no_numeric_checks_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [["课程", "评价"], ["甲", "优秀"], ["乙", "良好"]]
    pdf_path, parsed, crop, audit = _fixture(tmp_path, tables=[_table(rows)])
    _patch(monkeypatch, audit)

    result = build_native_expectation(crop, source_path=pdf_path, parsed=parsed)

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["no_numeric_or_marker_checks"]
    assert result.audit["skipped_reasons"] == {"non_numeric_text": 2}


def test_html_source_unavailable(tmp_path: Path) -> None:
    pdf_path, parsed, crop, _audit = _fixture(tmp_path)
    html_page = PageImageEvidence.from_png(
        doc_id="doc-1",
        version_id="v1",
        source_format="html",
        source_sha256=crop.page.source_sha256,
        page=1,
        image_path=crop.page.image_path,
    )
    layout = build_layout(
        html_page,
        [
            LayoutDetection(
                label="table",
                score=0.9,
                bbox_px=[100.0, 100.0, 500.0, 400.0],
                order=0,
            )
        ],
        detector_id="test-layout",
        detector_revision="rev-1",
    )
    html_crop = crop.model_copy(
        update={"page": html_page, "region": layout.regions[0]}
    )
    html_parsed = parsed.model_copy(
        update={
            "format": "html",
            "structure": ParsedStructure(format="html", tables=[]),
        }
    )

    result = build_native_expectation(
        html_crop, source_path=pdf_path, parsed=html_parsed
    )

    assert result.status == "unavailable"
    assert result.expectation is None
    assert result.reasons == ["unsupported_source_format"]


def test_import_does_not_load_torch() -> None:
    code = (
        "import sys\n"
        "import campusguide.ingest.ocr_native_expectation\n"
        "assert 'torch' not in sys.modules\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
