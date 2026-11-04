"""Offline protocol tests for the native-grid context, parser, and resolver.

Synthetic Pillow images prove the evidence wiring only; they do not measure
real OCR accuracy.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from campusguide.ingest.structure import SourceLocator, TableCell, TableRow, TableStructure
from campusguide.ingest.visual_verify import (
    CONTEXT_SCHEMA,
    NATIVE_CELL_CROP_PROMPT,
    NATIVE_CELL_CROP_PROMPT_SHA256,
    NATIVE_VERIFICATION_PROMPT,
    NativeCellObservation,
    build_native_grid_context,
    expected_native_crop_bbox,
    parse_native_grid_check,
    resolve_native_grid_check,
)

SOURCE_SHA = "a" * 64
IMAGE_SHA = "b" * 64


def _cell(row: int, col: int, text: str, *, needs_review: bool = False) -> TableCell:
    return TableCell(
        cell_id=f"cell-r{row}c{col}",
        row=row,
        col=col,
        origin_row=row,
        origin_col=col,
        text=text,
        needs_review=needs_review,
        bbox=[10.0 + 40 * col, 10.0 + 20 * row, 40.0 + 40 * col, 26.0 + 20 * row],
    )


def _table(
    table_id: str = "t1",
    *,
    page: int = 1,
    fmt: str = "pdf",
    kind: str = "data",
    texts: tuple[tuple[str, str], ...] = (("科目", "金额"), ("测试项", "18000")),
    needs_review: set[tuple[int, int]] | None = None,
) -> TableStructure:
    review = needs_review or set()
    rows = [
        TableRow(
            row_id=f"{table_id}-r{row_index}",
            row_index=row_index,
            is_header=row_index == 0,
            cells=[
                _cell(row_index, col, text, needs_review=(row_index, col) in review)
                for col, text in enumerate(values)
            ],
            text=" | ".join(values),
            locator=SourceLocator(format=fmt, page=page),
        )
        for row_index, values in enumerate(texts)
    ]
    bbox = [5.0, 5.0, 200.0, 80.0]
    return TableStructure(
        table_id=table_id,
        block_id=f"block-{table_id}",
        table_index=0,
        page=page,
        bbox=bbox,
        rows=rows,
        header_row_indices=[0],
        column_count=len(texts[0]),
        row_count=len(texts),
        column_headers=list(texts[0]),
        header_paths=[[text] for text in texts[0]],
        kind=kind,
        source="pymupdf" if fmt == "pdf" else "html_dom",
        locator=SourceLocator(format=fmt, page=page, bbox=bbox),
    )


def _pdf_context(
    tables: list[TableStructure] | None = None,
    *,
    page_size: tuple[float, float] | None = (240.0, 160.0),
) -> dict:
    return build_native_grid_context(
        tables if tables is not None else [_table()],
        page_no=1,
        source_format="pdf",
        page_size=page_size,
    )


def _payload(**check_overrides: object) -> dict:
    check = {
        "table_id": "t1",
        "checked_rows": [0, 1],
        "verified": True,
        "corrections": [],
        "uncertain_cells": [],
        "missing_rows": False,
        "missing_columns": False,
    }
    check.update(check_overrides)
    return {
        "page": 1,
        "table_checks": [check],
        "additional_content": [],
        "warnings": [],
    }


def _parse(
    payload: dict,
    context: dict,
    *,
    source_sha: str = SOURCE_SHA,
    page_sha: str = IMAGE_SHA,
):
    return parse_native_grid_check(
        payload, context=context, source_sha256=source_sha, page_image_sha256=page_sha
    )


def _write_proof_images(tmp_path: Path) -> tuple[Path, str, Path, str]:
    parent = Image.new("RGB", (240, 160), (240, 240, 240))
    for x in range(20, 120):
        for y in range(30, 90):
            parent.putpixel((x, y), (x % 256, y % 256, (x * y) % 256))
    crop = parent.crop((51, 31, 79, 45))
    parent_path = tmp_path / "page.png"
    crop_path = tmp_path / "cell.png"
    parent.save(parent_path, format="PNG")
    crop.save(crop_path, format="PNG")
    return (
        parent_path,
        hashlib.sha256(parent_path.read_bytes()).hexdigest(),
        crop_path,
        hashlib.sha256(crop_path.read_bytes()).hexdigest(),
    )


def _context_cell(context: dict, table_id: str, row: int, col: int) -> dict:
    table = next(item for item in context["tables"] if item["table_id"] == table_id)
    row_data = next(item for item in table["rows"] if item["row"] == row)
    return next(item for item in row_data["cells"] if item["col"] == col)


def _observation(
    check,
    context: dict,
    proof: tuple[Path, str, Path, str],
    *,
    table_id: str = "t1",
    row: int = 1,
    col: int = 1,
    observed_text: str = "",
    uncertain: bool = False,
    crop_bbox: tuple[int, int, int, int] = (51, 31, 79, 45),
    native_bbox: tuple[float, float, float, float] | None = None,
    model: str = "fixture-crop-model",
    prompt_sha256: str = NATIVE_CELL_CROP_PROMPT_SHA256,
    source_sha256: str | None = None,
    page_no: int | None = None,
    native_context_sha256: str | None = None,
) -> NativeCellObservation:
    parent_path, parent_sha, crop_path, crop_sha = proof
    if native_bbox is None:
        cell = _context_cell(context, table_id, row, col)
        native_bbox = tuple(cell["bbox"])
    return NativeCellObservation(
        table_id=table_id,
        row=row,
        col=col,
        source_sha256=check.source_sha256 if source_sha256 is None else source_sha256,
        parent_image_sha256=parent_sha,
        crop_image_sha256=crop_sha,
        parent_image_path=str(parent_path),
        crop_image_path=str(crop_path),
        crop_bbox=crop_bbox,
        native_bbox=native_bbox,
        observed_text=observed_text,
        uncertain=uncertain,
        model=model,
        prompt_sha256=prompt_sha256,
        page_no=check.page_no if page_no is None else page_no,
        native_context_sha256=(
            check.native_context_sha256
            if native_context_sha256 is None
            else native_context_sha256
        ),
    )


def test_context_is_canonical_native_grid_with_own_hash() -> None:
    context = build_native_grid_context(
        [_table(needs_review={(1, 1)})], page_no=1, source_format="pdf"
    )
    body = {key: value for key, value in context.items() if key != "native_context_sha256"}
    digest = hashlib.sha256(
        json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    assert context["native_context_sha256"] == digest
    assert context["schema"] == CONTEXT_SCHEMA
    entry = context["tables"][0]
    assert entry["row_indices"] == [0, 1]
    assert entry["header_row_indices"] == [0]
    assert entry["header_paths"] == [["科目"], ["金额"]]
    assert entry["needs_review_cells"] == [[1, 1]]
    cell = entry["rows"][1]["cells"][1]
    assert (cell["row"], cell["col"]) == (1, 1)
    assert (cell["origin_row"], cell["origin_col"]) == (1, 1)
    assert (cell["rowspan"], cell["colspan"]) == (1, 1)
    assert cell["covered"] is False
    assert cell["text"] == "18000"
    assert cell["needs_review"] is True
    assert cell["bbox_reliable"] is True
    assert '"table_checks"' in NATIVE_VERIFICATION_PROMPT


def test_context_filters_layout_and_scope() -> None:
    pdf = build_native_grid_context(
        [_table("other-page", page=2), _table("layout", kind="layout"), _table("t1")],
        page_no=1,
        source_format="pdf",
    )
    assert [entry["table_id"] for entry in pdf["tables"]] == ["t1"]
    html = build_native_grid_context(
        [
            _table("h1", fmt="html"),
            _table("h2", page=2, fmt="html"),
            _table("layout", fmt="html", kind="layout"),
        ],
        page_no=1,
        source_format="html",
    )
    assert [entry["table_id"] for entry in html["tables"]] == ["h1", "h2"]


def test_valid_pdf_check_keeps_all_pending_evidence() -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    payload = _payload(
        corrections=[
            {"row": 1, "col": 1, "observed_text": "18000.00", "reason": "原图可见小数"}
        ],
        uncertain_cells=[{"row": 0, "col": 0}],
    )
    payload["warnings"] = ["印章遮挡"]
    check = _parse(payload, context)
    assert check.source_sha256 == SOURCE_SHA
    assert check.page_image_sha256 == IMAGE_SHA
    assert check.native_context_sha256 == context["native_context_sha256"]
    assert check.source_format == "pdf"
    assert check.table_checks[0].checked_rows == (0, 1)
    assert check.checked_rows_by_table == {"t1": (0, 1)}
    assert check.unresolved == ()
    assert check.warnings == ("印章遮挡",)
    pending = {(item.row, item.col, item.reason) for item in check.pending_cells}
    assert pending == {
        (1, 1, "needs_review"),
        (1, 1, "correction"),
        (0, 0, "uncertain"),
    }


def test_unknown_table_and_non_integer_coordinates_raise() -> None:
    context = _pdf_context()
    with pytest.raises(ValueError, match="未知 table_id"):
        _parse(_payload(table_id="ghost"), context)
    with pytest.raises(ValueError, match="整数"):
        _parse(_payload(checked_rows=[0, 1.0]), context)
    with pytest.raises(ValueError, match="整数"):
        _parse(_payload(checked_rows=[0, True]), context)
    with pytest.raises(ValueError, match="整数"):
        _parse(
            _payload(
                corrections=[{"row": True, "col": 0, "observed_text": "X", "reason": "test"}]
            ),
            context,
        )


def test_duplicate_table_check_is_rejected() -> None:
    context = _pdf_context()
    payload = _payload()
    payload["table_checks"].append(copy.deepcopy(payload["table_checks"][0]))
    with pytest.raises(ValueError, match="重复"):
        _parse(payload, context)


def test_checked_rows_must_be_explicit_and_pdf_complete() -> None:
    context = _pdf_context()
    missing = _payload()
    del missing["table_checks"][0]["checked_rows"]
    with pytest.raises(ValueError, match="checked_rows"):
        _parse(missing, context)
    with pytest.raises(ValueError, match="所有 native 行"):
        _parse(_payload(checked_rows=[0]), context)


def test_html_partial_coverage_is_preserved_and_unresolved() -> None:
    context = build_native_grid_context([_table(fmt="html")], page_no=1, source_format="html")
    check = _parse(_payload(checked_rows=[1]), context)
    assert check.table_checks[0].checked_rows == (1,)
    assert check.checked_rows_by_table == {"t1": (1,)}
    assert [(item.table_id, item.reason) for item in check.unresolved] == [
        ("t1", "partial_coverage")
    ]
    with pytest.raises(ValueError, match="显式 checked"):
        _parse(
            _payload(
                checked_rows=[1],
                corrections=[{"row": 0, "col": 0, "observed_text": "X", "reason": "test"}],
            ),
            context,
        )


def test_tampered_context_is_rejected() -> None:
    context = _pdf_context()
    tampered = copy.deepcopy(context)
    tampered["tables"][0]["rows"][1]["cells"][1]["text"] = "99999"
    with pytest.raises(ValueError, match="重算不匹配"):
        _parse(_payload(), tampered)


def test_correction_equal_to_native_is_rejected() -> None:
    context = _pdf_context()
    with pytest.raises(ValueError, match="同值"):
        _parse(
            _payload(
                corrections=[
                    {"row": 0, "col": 0, "observed_text": " 科目 ", "reason": "duplicate"}
                ]
            ),
            context,
        )


def test_empty_context_needs_matching_page_and_allows_additional_content() -> None:
    context = build_native_grid_context([], page_no=3, source_format="html")
    payload = {
        "page": 3,
        "table_checks": [],
        "additional_content": [{"kind": "image", "text": "图 1"}],
        "warnings": [],
    }
    check = _parse(payload, context)
    assert check.page_no == 3
    assert check.checked_rows_by_table == {}
    assert check.additional_content == ({"kind": "image", "text": "图 1"},)
    payload["page"] = 2
    with pytest.raises(ValueError, match="页码"):
        _parse(payload, context)


def test_resolver_requires_current_context_hash() -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    check = _parse(_payload(), context)
    other = _pdf_context([_table()])
    with pytest.raises(ValueError, match="hash 不一致"):
        resolve_native_grid_check(check, [], context=other)


def test_expected_native_crop_bbox_matches_render_geometry() -> None:
    assert expected_native_crop_bbox(
        (50.0, 30.0, 80.0, 46.0),
        page_width=240.0,
        page_height=160.0,
        render_width=240,
        render_height=160,
    ) == (51, 31, 79, 45)
    assert expected_native_crop_bbox(
        (50.0, 30.0, 80.0, 46.0),
        page_width=240.0,
        page_height=160.0,
        render_width=480,
        render_height=320,
    ) == (102, 62, 158, 90)
    assert expected_native_crop_bbox(
        (0.0, 0.0, 2.0, 2.0),
        page_width=100.0,
        page_height=100.0,
        render_width=100,
        render_height=100,
    ) == (0, 0, 2, 2)
    with pytest.raises(ValueError, match="超出页面范围"):
        expected_native_crop_bbox(
            (0.0, 0.0, 101.0, 2.0),
            page_width=100.0,
            page_height=100.0,
            render_width=100,
            render_height=100,
        )
    with pytest.raises(ValueError, match="有限"):
        expected_native_crop_bbox(
            (0.0, 0.0, float("nan"), 2.0),
            page_width=100.0,
            page_height=100.0,
            render_width=100,
            render_height=100,
        )


def test_page_geometry_is_hashed_and_validated() -> None:
    base = build_native_grid_context(
        [_table()], page_no=1, source_format="pdf", page_size=(240, 160)
    )
    wider = build_native_grid_context(
        [_table()], page_no=1, source_format="pdf", page_size=(241, 160)
    )
    assert base["native_page_size"] == [240.0, 160.0]
    assert base["native_context_sha256"] != wider["native_context_sha256"]
    tampered = copy.deepcopy(base)
    tampered["native_page_size"] = [240.0, 161.0]
    with pytest.raises(ValueError, match="重算不匹配"):
        _parse(_payload(), tampered)
    for bad_size in ((0, 160), (True, 160), (float("nan"), 160), (240, 160, 1)):
        with pytest.raises(ValueError):
            build_native_grid_context(
                [], page_no=1, source_format="pdf", page_size=bad_size
            )


def test_neighbor_cell_crop_is_rejected(tmp_path: Path) -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    parent_path, parent_sha, crop_path, crop_sha = _write_proof_images(tmp_path)
    check = _parse(_payload(), context, page_sha=parent_sha)
    neighbor_bbox = (11, 31, 39, 45)
    neighbor_path = tmp_path / "neighbor.png"
    with Image.open(parent_path) as parent_image:
        parent_image.crop(neighbor_bbox).save(neighbor_path, format="PNG")
    neighbor_sha = hashlib.sha256(neighbor_path.read_bytes()).hexdigest()
    observation = _observation(
        check,
        context,
        (parent_path, parent_sha, crop_path, crop_sha),
        observed_text="18000",
        crop_bbox=neighbor_bbox,
    )
    assert neighbor_bbox != expected_native_crop_bbox(
        (50.0, 30.0, 80.0, 46.0),
        page_width=240.0,
        page_height=160.0,
        render_width=240,
        render_height=160,
    )
    wrong = replace(
        observation,
        crop_image_path=str(neighbor_path),
        crop_image_sha256=neighbor_sha,
    )
    with pytest.raises(ValueError, match="expected_native_crop_bbox"):
        resolve_native_grid_check(check, [wrong], context=context)


def test_shrunken_white_crop_inside_cell_is_rejected(tmp_path: Path) -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    parent_path = tmp_path / "white-page.png"
    Image.new("RGB", (240, 160), (255, 255, 255)).save(parent_path, format="PNG")
    parent_sha = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    check = _parse(_payload(), context, page_sha=parent_sha)
    white_bbox = (60, 35, 70, 42)
    crop_path = tmp_path / "white-subcell.png"
    with Image.open(parent_path) as parent_image:
        parent_image.crop(white_bbox).save(crop_path, format="PNG")
    crop_sha = hashlib.sha256(crop_path.read_bytes()).hexdigest()
    observation = _observation(
        check,
        context,
        (parent_path, parent_sha, crop_path, crop_sha),
        observed_text="18000",
        crop_bbox=white_bbox,
    )
    with pytest.raises(ValueError, match="expected_native_crop_bbox"):
        resolve_native_grid_check(check, [observation], context=context)


def test_missing_page_geometry_cannot_authorize_crop_change(tmp_path: Path) -> None:
    context = build_native_grid_context(
        [_table(texts=(("科目", "金额"), ("测试项", "48")))],
        page_no=1,
        source_format="pdf",
        page_size=None,
    )
    assert "native_page_size" not in context
    proof = _write_proof_images(tmp_path)
    payload = _payload(
        corrections=[{"row": 1, "col": 1, "observed_text": "84", "reason": "整页复核"}]
    )
    check = _parse(payload, context, page_sha=proof[1])
    observation = _observation(check, context, proof, observed_text="84")
    decision = resolve_native_grid_check(
        check,
        [observation],
        context=context,
        allow_confirmed_corrections=True,
    )
    assert decision.approved_corrections == ()
    assert decision.confirmed_cells == ()
    assert decision.publishable is False
    assert [(item.table_id, item.row, item.col, item.reason) for item in decision.unresolved] == [
        ("t1", 1, 1, "page_geometry_missing")
    ]


def test_needs_review_crop_matching_native_confirms_cell(tmp_path: Path) -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    proof = _write_proof_images(tmp_path)
    check = _parse(_payload(), context, page_sha=proof[1])
    observation = _observation(check, context, proof, observed_text="18000")

    decision = resolve_native_grid_check(check, [observation], context=context)
    assert decision.publishable is True
    assert decision.unresolved == ()
    assert decision.approved_corrections == ()
    assert decision.rejected_proposals == ()
    assert len(decision.confirmed_cells) == 1
    confirmed = decision.confirmed_cells[0]
    assert (confirmed.table_id, confirmed.row, confirmed.col) == ("t1", 1, 1)
    assert confirmed.native_text == "18000"
    assert confirmed.observed_text == "18000"
    assert confirmed.clear_needs_review is True
    assert decision.observations == (observation,)

    missing = resolve_native_grid_check(check, [], context=context)
    assert missing.publishable is False
    assert [(item.table_id, item.row, item.col, item.reason) for item in missing.unresolved] == [
        ("t1", 1, 1, "observation_missing")
    ]


def test_blank_native_rejects_invented_id_proposal(tmp_path: Path) -> None:
    context = _pdf_context([_table(texts=(("科目", "金额"), ("测试项", "")))])
    proof = _write_proof_images(tmp_path)
    payload = _payload(
        corrections=[{"row": 1, "col": 1, "observed_text": "CS101", "reason": "编号规律推测"}]
    )
    check = _parse(payload, context, page_sha=proof[1])
    observation = _observation(check, context, proof, observed_text="")

    decision = resolve_native_grid_check(check, [observation], context=context)
    assert decision.publishable is True
    assert decision.approved_corrections == ()
    assert decision.confirmed_cells[0].native_text == ""
    assert decision.confirmed_cells[0].observed_text == ""
    assert [item.reason for item in decision.rejected_proposals] == ["crop_matches_native"]
    assert decision.rejected_proposals[0].proposed_text == "CS101"
    assert decision.check is check
    assert check.raw_payload["table_checks"][0]["corrections"][0]["observed_text"] == "CS101"
    assert "observed_text" in NATIVE_CELL_CROP_PROMPT


def test_double_read_requires_explicit_opt_in(tmp_path: Path) -> None:
    context = _pdf_context([_table(texts=(("科目", "金额"), ("测试项", "48")))])
    proof = _write_proof_images(tmp_path)
    payload = _payload(
        corrections=[{"row": 1, "col": 1, "observed_text": "84", "reason": "整页复核"}]
    )
    check = _parse(payload, context, page_sha=proof[1])
    observation = _observation(check, context, proof, observed_text="84")

    default = resolve_native_grid_check(check, [observation], context=context)
    assert default.approved_corrections == ()
    assert default.publishable is False
    assert [(item.table_id, item.row, item.col, item.reason) for item in default.unresolved] == [
        ("t1", 1, 1, "confirmed_change_requires_opt_in")
    ]

    opted = resolve_native_grid_check(
        check, [observation], context=context, allow_confirmed_corrections=True
    )
    assert opted.publishable is True
    assert len(opted.approved_corrections) == 1
    approved = opted.approved_corrections[0]
    assert (approved.table_id, approved.row, approved.col) == ("t1", 1, 1)
    assert approved.observed_text == "84"
    assert opted.allow_confirmed_corrections is True
    assert opted.check is check
    assert check.raw_payload["table_checks"][0]["corrections"][0]["observed_text"] == "84"


def test_third_value_and_uncertain_observations_stay_unresolved(tmp_path: Path) -> None:
    context = _pdf_context([_table(texts=(("科目", "金额"), ("测试项", "48")))])
    proof = _write_proof_images(tmp_path)
    payload = _payload(
        corrections=[{"row": 1, "col": 1, "observed_text": "84", "reason": "整页复核"}]
    )
    check = _parse(payload, context, page_sha=proof[1])

    third = _observation(check, context, proof, observed_text="840")
    decision = resolve_native_grid_check(check, [third], context=context)
    assert decision.publishable is False
    assert [(item.row, item.col, item.reason) for item in decision.unresolved] == [
        (1, 1, "crop_third_value")
    ]

    unsure = _observation(check, context, proof, observed_text="84", uncertain=True)
    decision = resolve_native_grid_check(check, [unsure], context=context)
    assert decision.publishable is False
    assert [(item.row, item.col, item.reason) for item in decision.unresolved] == [
        (1, 1, "crop_uncertain")
    ]


def test_proof_tampering_cross_source_float_and_duplicates_are_rejected(
    tmp_path: Path,
) -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    parent_path, parent_sha, crop_path, crop_sha = _write_proof_images(tmp_path)
    check = _parse(_payload(), context, page_sha=parent_sha)
    observation = _observation(
        check, context, (parent_path, parent_sha, crop_path, crop_sha), observed_text="18000"
    )

    with pytest.raises(ValueError, match="crop_bbox"):
        resolve_native_grid_check(
            check,
            [replace(observation, crop_bbox=(51.0, 31, 79, 45))],
            context=context,
        )
    with pytest.raises(ValueError, match="整数"):
        resolve_native_grid_check(check, [replace(observation, row=1.0)], context=context)
    with pytest.raises(ValueError, match="source_sha256"):
        resolve_native_grid_check(
            check, [replace(observation, source_sha256="c" * 64)], context=context
        )
    with pytest.raises(ValueError, match="重复"):
        resolve_native_grid_check(check, [observation, observation], context=context)
    with pytest.raises(ValueError, match="prompt_sha256"):
        resolve_native_grid_check(
            check, [replace(observation, prompt_sha256="d" * 64)], context=context
        )
    with pytest.raises(ValueError, match="不存在"):
        resolve_native_grid_check(
            check,
            [replace(observation, crop_image_path=str(tmp_path / "missing.png"))],
            context=context,
        )
    with pytest.raises(ValueError, match="native_bbox"):
        resolve_native_grid_check(
            check,
            [replace(observation, native_bbox=(0.0, 0.0, 1.0, 1.0))],
            context=context,
        )
    with pytest.raises(ValueError, match="model"):
        resolve_native_grid_check(check, [replace(observation, model="  ")], context=context)
    with pytest.raises(ValueError, match="page_no"):
        resolve_native_grid_check(check, [replace(observation, page_no=2)], context=context)
    with pytest.raises(ValueError, match="native_context_sha256"):
        resolve_native_grid_check(
            check, [replace(observation, native_context_sha256="e" * 64)], context=context
        )

    crop_path.write_bytes(b"not-a-png")
    with pytest.raises(ValueError, match="裁剪图文件重算不一致"):
        resolve_native_grid_check(check, [observation], context=context)
    parent_path.write_bytes(parent_path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="父图文件重算不一致"):
        resolve_native_grid_check(check, [observation], context=context)


def test_fake_crop_bytes_are_rejected_even_with_matching_sha(tmp_path: Path) -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    parent_path, parent_sha, crop_path, crop_sha = _write_proof_images(tmp_path)
    check = _parse(_payload(), context, page_sha=parent_sha)
    fake_path = tmp_path / "fake.png"
    Image.new("RGB", (28, 14), (0, 0, 0)).save(fake_path, format="PNG")
    fake_sha = hashlib.sha256(fake_path.read_bytes()).hexdigest()
    observation = _observation(
        check, context, (parent_path, parent_sha, crop_path, crop_sha), observed_text="18000"
    )
    fake = replace(
        observation, crop_image_path=str(fake_path), crop_image_sha256=fake_sha
    )
    with pytest.raises(ValueError, match="像素不等于"):
        resolve_native_grid_check(check, [fake], context=context)


def test_reencoded_crop_is_accepted_when_pixels_match(tmp_path: Path) -> None:
    context = _pdf_context([_table(needs_review={(1, 1)})])
    parent_path, parent_sha, crop_path, crop_sha = _write_proof_images(tmp_path)
    check = _parse(_payload(), context, page_sha=parent_sha)
    reencoded_path = tmp_path / "cell-reencoded.bmp"
    with Image.open(crop_path) as crop_image:
        crop_image.save(reencoded_path, format="BMP")
    original_bytes = crop_path.read_bytes()
    reencoded_bytes = reencoded_path.read_bytes()
    assert reencoded_bytes != original_bytes
    observation = _observation(
        check, context, (parent_path, parent_sha, crop_path, crop_sha), observed_text="18000"
    )
    reencoded = replace(
        observation,
        crop_image_path=str(reencoded_path),
        crop_image_sha256=hashlib.sha256(reencoded_bytes).hexdigest(),
    )
    decision = resolve_native_grid_check(check, [reencoded], context=context)
    assert decision.publishable is True
    assert decision.confirmed_cells[0].clear_needs_review is True


def test_verified_false_releases_only_fully_resolved_corrections(tmp_path: Path) -> None:
    context = _pdf_context([_table()])
    proof = _write_proof_images(tmp_path)
    payload = _payload(
        verified=False,
        corrections=[{"row": 1, "col": 1, "observed_text": "18000.00", "reason": "原图小数"}],
    )
    check = _parse(payload, context, page_sha=proof[1])
    assert [(item.table_id, item.reason) for item in check.unresolved] == [
        ("t1", "not_verified")
    ]

    blocked = resolve_native_grid_check(check, [], context=context)
    assert blocked.publishable is False
    assert {item.reason for item in blocked.unresolved} == {
        "observation_missing",
        "not_verified",
    }

    rejected = resolve_native_grid_check(
        check,
        [_observation(check, context, proof, observed_text="18000")],
        context=context,
    )
    assert rejected.released_not_verified == ("t1",)
    assert rejected.unresolved == ()
    assert rejected.publishable is True
    assert [item.reason for item in rejected.rejected_proposals] == ["crop_matches_native"]

    approved = resolve_native_grid_check(
        check,
        [_observation(check, context, proof, observed_text="18000.00")],
        context=context,
        allow_confirmed_corrections=True,
    )
    assert approved.released_not_verified == ("t1",)
    assert approved.unresolved == ()
    assert approved.publishable is True
    assert approved.check.raw_payload["table_checks"][0]["verified"] is False


def test_missing_rows_blocks_release_even_with_resolved_crop(tmp_path: Path) -> None:
    context = _pdf_context([_table()])
    proof = _write_proof_images(tmp_path)
    payload = _payload(
        verified=False,
        missing_rows=True,
        corrections=[{"row": 1, "col": 1, "observed_text": "18000.00", "reason": "原图小数"}],
    )
    check = _parse(payload, context, page_sha=proof[1])
    decision = resolve_native_grid_check(
        check,
        [_observation(check, context, proof, observed_text="18000.00")],
        context=context,
        allow_confirmed_corrections=True,
    )
    assert decision.released_not_verified == ()
    assert decision.publishable is False
    reasons = {(item.table_id, item.reason) for item in decision.unresolved}
    assert ("t1", "missing_rows") in reasons
    assert ("t1", "not_verified") in reasons


def test_html_partial_gap_and_native_review_are_never_published(tmp_path: Path) -> None:
    context = build_native_grid_context(
        [
            _table(
                fmt="html",
                texts=(("科目", "金额"), ("甲", "1"), ("乙", "2")),
                needs_review={(0, 0)},
            )
        ],
        page_no=1,
        source_format="html",
    )
    proof = _write_proof_images(tmp_path)
    check = _parse(_payload(checked_rows=[1], verified=True), context, page_sha=proof[1])
    decision = resolve_native_grid_check(check, [], context=context)
    reasons = {(item.table_id, item.row, item.col, item.reason) for item in decision.unresolved}
    assert ("t1", None, None, "partial_coverage") in reasons
    assert ("t1", 0, 0, "page_geometry_missing") in reasons
    assert decision.publishable is False


def test_unreported_context_table_blocks_publication() -> None:
    context = build_native_grid_context(
        [_table("t1", fmt="html"), _table("t2", fmt="html")],
        page_no=1,
        source_format="html",
    )
    check = _parse(_payload(), context)
    decision = resolve_native_grid_check(check, [], context=context)
    reasons = {(item.table_id, item.reason) for item in decision.unresolved}
    assert ("t2", "table_not_reported") in reasons
    assert decision.publishable is False
