"""Offline tests for the PDF native-grid verification runtime.

Every image here is a real Pillow PNG so crop/父图 byte and pixel proofs are
exercised; providers are deterministic stubs, no network or model is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import campusguide.ingest.native_visual as native
from campusguide.ingest.native_source import NativeSourceAudit
from campusguide.ingest.parse import ParseOutput
from campusguide.ingest.structure import (
    PageStructure,
    ParsedStructure,
    SourceLocator,
    StructureBlock,
    TableCell,
    TableRow,
    TableStructure,
)
from campusguide.ingest.visual import (
    GeminiVisionProvider,
    RenderedPage,
    VisionConfig,
    VisionRateLimited,
    VisualPreparser,
    apply_vision_result,
)

TABLE_ID = "doc@ver#table0"
PAGE_SIZE = 200


class NativeRenderer:
    version = "native-renderer/1"

    def __init__(self, width: int = PAGE_SIZE, height: int = PAGE_SIZE) -> None:
        self.width = width
        self.height = height

    def render(
        self, source_path: Path, *, source_format: str, dpi: int
    ) -> list[RenderedPage]:
        image = Image.new("RGB", (self.width, self.height), "white")
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(
            ["#d62728", "#2ca02c", "#1f77b4", "#ff7f0e", "#9467bd", "#17becf"]
        ):
            row, col = divmod(index, 2)
            x0 = 10 + col * 80
            y0 = 10 + row * 30
            draw.rectangle([x0, y0, x0 + 70, y0 + 20], fill=color)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        png = buffer.getvalue()
        return [
            RenderedPage(
                page_no=1,
                image_sha256=hashlib.sha256(png).hexdigest(),
                width=self.width,
                height=self.height,
                dpi=dpi,
                png_base64=base64.b64encode(png).decode("ascii"),
            )
        ]


class NativeProvider:
    version = "native-provider/1"
    model = "native-model"

    def __init__(
        self,
        verify_payload,
        cell_payloads: dict[tuple[int, int, int, int], dict] | None = None,
        *,
        verify_error: Exception | None = None,
    ) -> None:
        self.verify_payload = verify_payload
        self.cell_payloads = dict(cell_payloads or {})
        self.verify_error = verify_error
        self.verify_calls: list[dict] = []
        self.cell_calls: list[dict] = []
        self.transcribe_calls: list[dict] = []
        self.last_native_audit: dict = {}

    def verify_native_grid(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        self.verify_calls.append(
            {
                "page_no": page_no,
                "image_sha256": hashlib.sha256(image_png).hexdigest(),
                "context": dict(context),
            }
        )
        if self.verify_error is not None:
            raise self.verify_error
        payload = (
            self.verify_payload(page_no)
            if callable(self.verify_payload)
            else json.loads(json.dumps(self.verify_payload))
        )
        self.last_native_audit = {
            "stage": "native_grid_verify",
            "response_sha256": _payload_sha(payload),
            "model": self.model,
        }
        return payload

    def recognize_cell(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        bbox = tuple(int(value) for value in context["crop_bbox"])
        self.cell_calls.append(
            {
                "page_no": page_no,
                "image_sha256": hashlib.sha256(image_png).hexdigest(),
                "context": dict(context),
                "bbox": bbox,
            }
        )
        payload = self.cell_payloads[bbox]
        self.last_native_audit = {
            "stage": "native_cell",
            "response_sha256": _payload_sha(payload),
            "model": self.model,
        }
        return json.loads(json.dumps(payload))

    def recognize_page(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        self.transcribe_calls.append({"page_no": page_no, "context": dict(context)})
        return {"page": page_no, "blocks": [{"id": "b1", "kind": "paragraph", "text": "正文"}]}


class LegacyProvider:
    version = "legacy-provider/1"
    model = "legacy-model"

    def __init__(self) -> None:
        self.transcribe_calls: list[dict] = []

    def recognize_page(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        self.transcribe_calls.append({"page_no": page_no, "context": dict(context)})
        return {
            "page": page_no,
            "blocks": [{"id": "b1", "kind": "paragraph", "text": "正文"}],
        }


def _payload_sha(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _source(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\nnative grid fixture\n")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path, **overrides) -> VisionConfig:
    values = {
        "cache_dir": tmp_path / "cache",
        "dpi": 144,
        "max_requests": 20,
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "max_tables_per_page": 8,
    }
    values.update(overrides)
    return VisionConfig(**values)


def _cell(
    row: int,
    col: int,
    text: str,
    bbox: list[float] | None,
    *,
    needs_review: bool = False,
    is_header: bool = False,
) -> TableCell:
    return TableCell(
        cell_id=f"{TABLE_ID}#r{row}c{col}",
        row=row,
        col=col,
        origin_row=row,
        origin_col=col,
        text=text,
        text_source="native" if text else None,
        needs_review=needs_review,
        is_header=is_header,
        bbox=bbox,
    )


def _row(index: int, cells: list[TableCell], *, header: bool = False) -> TableRow:
    return TableRow(
        row_id=f"{TABLE_ID}#row{index}",
        row_index=index,
        is_header=header,
        cells=cells,
        text=" | ".join(cell.text for cell in cells),
        locator=SourceLocator(format="pdf", page=1, bbox=[10, 10, 160, 100]),
    )


def _native_table(
    *,
    blank_needs_review: bool = False,
    blank_bbox_missing: bool = False,
    review_needs_review: bool = False,
    correction_needs_review: bool = False,
) -> TableStructure:
    blank_bbox = None if blank_bbox_missing else [90.0, 70.0, 160.0, 100.0]
    rows = [
        _row(
            0,
            [
                _cell(0, 0, "科目", [10, 10, 90, 40], is_header=True),
                _cell(0, 1, "金额", [90, 10, 160, 40], is_header=True),
            ],
            header=True,
        ),
        _row(
            1,
            [
                _cell(1, 0, "测试项", [10, 40, 90, 70]),
                _cell(
                    1,
                    1,
                    "18000",
                    [90, 40, 160, 70],
                    needs_review=correction_needs_review,
                ),
            ],
        ),
        _row(
            2,
            [
                _cell(
                    2,
                    0,
                    "空值",
                    [10, 70, 90, 100],
                    needs_review=review_needs_review,
                ),
                _cell(
                    2,
                    1,
                    "",
                    blank_bbox,
                    needs_review=blank_needs_review,
                ),
            ],
        ),
    ]
    return TableStructure(
        table_id=TABLE_ID,
        block_id=f"{TABLE_ID}#block",
        table_index=0,
        page=1,
        bbox=[10, 10, 160, 100],
        rows=rows,
        header_row_indices=[0],
        column_count=2,
        row_count=3,
        column_headers=["科目", "金额"],
        header_paths=[["科目"], ["金额"]],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=1, bbox=[10, 10, 160, 100]),
    )


def _span_cell(
    row: int,
    col: int,
    text: str,
    *,
    origin_row: int | None = None,
    origin_col: int | None = None,
    rowspan: int = 1,
    colspan: int = 1,
    covered: bool = False,
    needs_review: bool = False,
    is_header: bool = False,
    bbox: list[float] | None = None,
) -> TableCell:
    return TableCell(
        cell_id=f"{TABLE_ID}#r{row}c{col}",
        row=row,
        col=col,
        rowspan=rowspan,
        colspan=colspan,
        origin_row=row if origin_row is None else origin_row,
        origin_col=col if origin_col is None else origin_col,
        covered=covered,
        text=text,
        text_source="native" if text and not covered else None,
        needs_review=needs_review,
        is_header=is_header,
        bbox=bbox,
    )


def _spanned_table() -> TableStructure:
    rows = [
        _row(
            0,
            [
                _span_cell(
                    0,
                    0,
                    "科目",
                    colspan=2,
                    is_header=True,
                    bbox=[10, 10, 80, 30],
                ),
                _span_cell(
                    0,
                    1,
                    "覆盖泄漏",
                    origin_col=0,
                    colspan=2,
                    covered=True,
                    is_header=True,
                    bbox=[10, 10, 80, 30],
                ),
                _span_cell(0, 2, "金额", is_header=True, bbox=[80, 10, 160, 30]),
            ],
            header=True,
        ),
        _row(
            1,
            [
                _span_cell(1, 0, "学期", rowspan=2, bbox=[10, 30, 80, 90]),
                _span_cell(1, 1, "课程", bbox=[80, 30, 120, 60]),
                _span_cell(1, 2, "学分", bbox=[120, 30, 160, 60]),
            ],
        ),
        _row(
            2,
            [
                _span_cell(
                    2,
                    0,
                    "覆盖泄漏",
                    origin_row=1,
                    origin_col=0,
                    rowspan=2,
                    covered=True,
                ),
                _span_cell(2, 1, "", needs_review=True, bbox=[80, 60, 120, 90]),
                _span_cell(2, 2, "3", needs_review=True, bbox=[120, 60, 160, 90]),
            ],
        ),
        _row(
            3,
            [
                _span_cell(3, 0, "2025", bbox=[10, 90, 80, 120]),
                _span_cell(3, 1, "高数", bbox=[80, 90, 120, 120]),
            ],
        ),
    ]
    return TableStructure(
        table_id=TABLE_ID,
        block_id=f"{TABLE_ID}#block",
        table_index=0,
        page=1,
        bbox=[10, 10, 160, 120],
        rows=rows,
        header_row_indices=[0],
        column_count=3,
        row_count=4,
        column_headers=["科目", "金额", "学分"],
        header_paths=[["科目"], ["金额"], ["学分"]],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=1, bbox=[10, 10, 160, 120]),
    )


def _parsed(table: TableStructure | None) -> ParseOutput:
    tables = [table] if table is not None else []
    blocks = (
        [
            StructureBlock(
                block_id=table.block_id,
                order=0,
                kind="table",
                text="\n".join(row.text for row in table.rows if row.text.strip()),
                locator=SourceLocator(format="pdf", page=1, bbox=table.bbox),
                table_id=table.table_id,
            )
        ]
        if table is not None
        else []
    )
    structure = ParsedStructure(
        format="pdf",
        pages=[
            PageStructure(
                page=1,
                width=float(PAGE_SIZE),
                height=float(PAGE_SIZE),
                text_chars=20,
                has_text_layer=True,
            )
        ],
        tables=tables,
        blocks=blocks,
    )
    return ParseOutput(
        format="pdf",
        structure=structure,
        text="\n".join(block.text for block in blocks if block.text.strip()),
        title="费用明细",
        parser_version="test",
    )


def _verify_payload(
    *,
    checked_rows: tuple[int, ...] = (0, 1, 2),
    verified: bool = True,
    corrections: list[dict] | None = None,
    uncertain: list[dict] | None = None,
    additional: list[dict] | None = None,
) -> dict:
    table: dict = {
        "table_id": TABLE_ID,
        "checked_rows": list(checked_rows),
        "verified": verified,
        "missing_rows": False,
        "missing_columns": False,
    }
    if corrections is not None:
        table["corrections"] = corrections
    if uncertain is not None:
        table["uncertain_cells"] = uncertain
    return {
        "page": 1,
        "table_checks": [table],
        "additional_content": additional or [],
        "warnings": [],
    }


def _preparer(tmp_path: Path, provider, config: VisionConfig | None = None):
    return VisualPreparser(
        config or _config(tmp_path),
        renderer=NativeRenderer(),
        provider=provider,
    )


def _prepare(tmp_path: Path, provider, parsed, config: VisionConfig | None = None):
    source, sha = _source(tmp_path)
    preparser = _preparer(tmp_path, provider, config)
    result = preparser.prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=sha,
        source_format="pdf",
        parsed=parsed,
    )
    return preparser, result, sha


def _crop_bbox(
    bbox: list[float], *, page_width: float = PAGE_SIZE, page_height: float = PAGE_SIZE
) -> tuple[int, int, int, int]:
    return native.native_crop_bbox(
        bbox,
        page_width=page_width,
        page_height=page_height,
        render_width=PAGE_SIZE,
        render_height=PAGE_SIZE,
    )


def _source_audit_result(
    source_path: Path,
    *,
    page_no: int,
    tables: list[TableStructure],
    source_sha256: str,
    has_text_layer: bool = True,
    mismatch_cell: tuple[str, int, int] | None = None,
    mismatch_clip: str = "",
) -> NativeSourceAudit:
    """合成真实 guard 形状；fixture PDF 不是有效 PDF，生产路径由 Codex 真跑。"""
    audited = [table.model_copy(deep=True) for table in tables]
    checked_cells: list[dict] = []
    mismatches: list[dict] = []
    for table in audited:
        for row in table.rows:
            for cell in row.cells:
                entry: dict = {
                    "table_id": table.table_id,
                    "id": cell.cell_id,
                    "row": cell.row,
                    "col": cell.col,
                    "page": page_no,
                    "covered": bool(cell.covered),
                    "native_text": str(cell.text),
                    "clip_text": None,
                    "bbox": list(cell.bbox) if cell.bbox is not None else None,
                    "status": "",
                    "source_sha256": source_sha256,
                    "extractor": "test.source.audit",
                }
                if cell.covered:
                    entry["status"] = "skipped_covered"
                elif not has_text_layer:
                    entry["status"] = "unresolved_no_text_layer"
                    cell.needs_review = True
                elif mismatch_cell == (table.table_id, cell.row, cell.col):
                    entry["status"] = "source_clip_mismatch"
                    entry["clip_text"] = mismatch_clip
                    cell.needs_review = True
                    mismatches.append(
                        {
                            "reason": "source_clip_mismatch",
                            "id": cell.cell_id,
                            "table_id": table.table_id,
                            "row": cell.row,
                            "col": cell.col,
                            "native_text": str(cell.text),
                            "clip_text": mismatch_clip,
                            "bbox": entry["bbox"],
                            "source_sha256": source_sha256,
                            "page": page_no,
                            "extractor": "test.source.audit",
                        }
                    )
                elif cell.bbox is None and str(cell.text).strip():
                    entry["status"] = "unresolved_bbox"
                    cell.needs_review = True
                elif str(cell.text).strip():
                    entry["status"] = "source_text_match"
                elif cell.bbox is None:
                    entry["status"] = "skipped_no_bbox"
                else:
                    entry["status"] = "source_text_empty"
                checked_cells.append(entry)
    return NativeSourceAudit(
        source_path=Path(source_path),
        source_sha256=source_sha256,
        page_no=page_no,
        page_width=float(PAGE_SIZE),
        page_height=float(PAGE_SIZE),
        has_text_layer=has_text_layer,
        tables=audited,
        checked_cells=checked_cells,
        mismatches=mismatches,
        warnings=[],
    )


@pytest.fixture(autouse=True)
def _source_audit_stub(monkeypatch):
    calls: list[int] = []

    def fake_audit(source_path, *, page_no, tables, source_sha256):
        calls.append(page_no)
        return _source_audit_result(
            source_path,
            page_no=page_no,
            tables=list(tables),
            source_sha256=source_sha256,
        )

    monkeypatch.setattr(
        "campusguide.ingest.visual.audit_pdf_grid_cells", fake_audit
    )
    return calls


def test_native_crop_bbox_insets_one_point_and_keeps_tiny_cells():
    assert native.native_crop_bbox(
        [10, 10, 90, 40],
        page_width=100,
        page_height=100,
        render_width=200,
        render_height=200,
    ) == (22, 22, 178, 78)
    # 宽度 1.5pt 的过小格不内缩，高度 4pt 正常内缩。
    assert native.native_crop_bbox(
        [10, 10, 11.5, 14],
        page_width=100,
        page_height=100,
        render_width=200,
        render_height=200,
    ) == (20, 22, 23, 26)


def test_model_native_context_is_compact_and_preserves_spans_and_empties():
    from campusguide.ingest.visual_verify import build_native_grid_context

    table = _spanned_table()
    context = build_native_grid_context(
        [table],
        page_no=1,
        source_format="pdf",
        page_size=(PAGE_SIZE, PAGE_SIZE),
    )
    compact = native.model_native_context(context)

    assert set(compact) == {"page_no", "source_format", "tables"}
    entry = compact["tables"][0]
    assert set(entry) == {
        "table_id",
        "column_count",
        "header_rows",
        "header_paths",
        "rows",
        "spans",
        "needs_review",
    }
    assert entry["table_id"] == TABLE_ID
    assert entry["column_count"] == 3
    assert entry["header_rows"] == [0]
    assert entry["header_paths"] == [["科目"], ["金额"], ["学分"]]
    assert entry["spans"] == [[0, 0, 1, 2], [1, 0, 2, 1]]
    assert entry["needs_review"] == [[2, 1], [2, 2]]
    assert entry["rows"][0]["cells"] == [[0, "科目"], [1, ""], [2, "金额"]]
    assert entry["rows"][2]["cells"] == [[0, ""], [1, ""], [2, "3"]]
    assert entry["rows"][3] == {
        "row": 3,
        "cells": [[0, "2025"], [1, "高数"]],
        "missing_cols": [2],
        "error": "row_missing_columns",
    }

    serialized = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    full_serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
    assert len(serialized) < len(full_serialized)
    assert "覆盖泄漏" not in serialized
    assert "科目 | 覆盖泄漏 | 金额" not in serialized
    assert "覆盖泄漏" in full_serialized
    for forbidden_key in (
        "cell_id",
        "bbox",
        "origin_row",
        "origin_col",
        "rowspan",
        "colspan",
        "covered",
        "column_headers",
        "row_indices",
        "header_row_indices",
        "native_context_sha256",
        "native_text",
        "text_source",
        "locator",
        "block_id",
        "table_index",
        "warnings",
        "schema",
        "text",
    ):
        assert f'"{forbidden_key}"' not in serialized


def test_canonical_context_hash_tracks_geometry_and_text():
    from campusguide.ingest.visual_verify import build_native_grid_context

    def build(table: TableStructure) -> dict:
        return build_native_grid_context(
            [table],
            page_no=1,
            source_format="pdf",
            page_size=(PAGE_SIZE, PAGE_SIZE),
        )

    base_context = build(_native_table())
    compact_json = json.dumps(
        native.model_native_context(base_context),
        ensure_ascii=False,
        sort_keys=True,
    )
    full_json = json.dumps(base_context, ensure_ascii=False, sort_keys=True)
    assert compact_json.count("18000") == 1
    assert full_json.count("18000") >= 2

    moved = _native_table()
    moved.rows[1].cells[1].bbox = [95.0, 40.0, 165.0, 70.0]
    moved_context = build(moved)
    assert moved_context["native_context_sha256"] != (
        base_context["native_context_sha256"]
    )
    assert native.model_native_context(moved_context) == (
        native.model_native_context(base_context)
    )

    changed = _native_table()
    changed.rows[1].cells[1].text = "19000"
    changed.rows[1].text = "测试项 | 19000"
    changed_context = build(changed)
    assert changed_context["native_context_sha256"] != (
        base_context["native_context_sha256"]
    )
    assert native.model_native_context(changed_context) != (
        native.model_native_context(base_context)
    )


def test_native_context_hash_tracks_source_guard_needs_review():
    from campusguide.ingest.visual_verify import build_native_grid_context

    clean = _native_table()
    flagged = _native_table()
    flagged.rows[1].cells[0].needs_review = True

    def build(table: TableStructure) -> dict:
        return build_native_grid_context(
            [table],
            page_no=1,
            source_format="pdf",
            page_size=(PAGE_SIZE, PAGE_SIZE),
        )

    clean_context = build(clean)
    flagged_context = build(flagged)
    assert flagged_context["tables"][0]["needs_review_cells"] == [[1, 0]]
    assert native.model_native_context(flagged_context)["tables"][0][
        "needs_review"
    ] == [[1, 0]]
    assert flagged_context["native_context_sha256"] != (
        clean_context["native_context_sha256"]
    )


def test_native_prompt_and_cache_key_include_compaction_version():
    from campusguide.ingest.visual_verify import NATIVE_VERIFICATION_PROMPT

    legacy_prefix = (
        "请核验下面这页实际渲染图像。只输出协议 JSON；native 网格上下文"
        "（仅用于定位，不是答案，不得照抄）："
    )
    legacy_sha = hashlib.sha256(
        (NATIVE_VERIFICATION_PROMPT + "\n" + legacy_prefix).encode("utf-8")
    ).hexdigest()
    current_sha = native.native_verification_prompt_sha256()
    assert current_sha != legacy_sha
    assert current_sha == hashlib.sha256(
        (
            native.NATIVE_MODEL_CONTEXT_VERSION
            + "\n"
            + NATIVE_VERIFICATION_PROMPT
            + "\n"
            + native.NATIVE_VERIFY_USER_PREFIX
        ).encode("utf-8")
    ).hexdigest()
    assert native.NATIVE_MODEL_CONTEXT_VERSION in native.NATIVE_VERIFY_USER_PREFIX
    for fragment in (
        "needs_review",
        "checked_rows",
        "covered",
        "spans",
        "missing_cols",
    ):
        assert fragment in native.NATIVE_VERIFY_USER_PREFIX

    cache_values = {
        "source_sha256": "a" * 64,
        "image_sha256": "b" * 64,
        "render_sha256": "c" * 64,
        "renderer_version": "renderer/1",
        "provider_version": "provider/1",
        "model": "model",
        "cell_prompt_sha256": native.native_cell_prompt_sha256(),
        "native_context_sha256": "d" * 64,
        "page_no": 1,
        "dpi": 144,
    }
    assert native.native_grid_cache_key(
        prompt_sha256=legacy_sha, **cache_values
    ) != native.native_grid_cache_key(
        prompt_sha256=current_sha, **cache_values
    )


def test_clean_source_audit_sends_no_extra_crop_and_reruns_on_cache_hit(
    tmp_path, _source_audit_stub
):
    table = _native_table()
    provider = NativeProvider(_verify_payload())
    _, first, _ = _prepare(tmp_path, provider, _parsed(table))

    assert first.publishable is True
    assert provider.transcribe_calls == []
    assert provider.cell_calls == []
    assert _source_audit_stub == [1]
    summary = first.native_pages[1]["source_audit"]
    assert summary["version"] == native.NATIVE_SOURCE_AUDIT_VERSION
    assert summary["has_text_layer"] is True
    assert summary["status_counts"]["source_text_match"] == 5
    assert summary["status_counts"]["source_text_empty"] == 1

    source = tmp_path / "source.pdf"
    second_provider = NativeProvider(_verify_payload())
    second = _preparer(tmp_path, second_provider).prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_format="pdf",
        parsed=_parsed(table),
    )
    assert second_provider.verify_calls == []
    assert second_provider.cell_calls == []
    assert second.cache_hits == 1
    assert second.publishable is True
    # 缓存命中也不能跳过原件 guard。
    assert _source_audit_stub == [1, 1]
    assert second.native_pages[1]["cache_hit"] is True
    assert second.page_evidence[1]["native_source_audit_sha256"] == (
        second.native_pages[1]["source_audit_sha256"]
    )


def test_source_clip_mismatch_forces_blind_crop_even_when_model_claims_clean(
    tmp_path, monkeypatch
):
    table = _native_table()
    table.rows[1].cells[1].text = "84"
    table.rows[1].text = "测试项 | 84"
    mismatch_cell = (TABLE_ID, 1, 1)

    def fake_audit(source_path, *, page_no, tables, source_sha256):
        return _source_audit_result(
            source_path,
            page_no=page_no,
            tables=list(tables),
            source_sha256=source_sha256,
            mismatch_cell=mismatch_cell,
            mismatch_clip="48",
        )

    monkeypatch.setattr(
        "campusguide.ingest.visual.audit_pdf_grid_cells", fake_audit
    )
    bbox = _crop_bbox([90, 40, 160, 70])
    provider = NativeProvider(
        _verify_payload(),
        {bbox: {"observed_text": "48", "uncertain": False}},
    )
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert len(provider.verify_calls) == 1
    assert provider.transcribe_calls == []
    # 主模型谎报 verified/0 correction 也必须对 guard 标记格真实裁剪复识。
    assert len(provider.cell_calls) == 1
    assert provider.cell_calls[0]["bbox"] == bbox
    assert result.ok is True
    assert result.publishable is False
    assert any(
        item["kind"] == "native_source_clip_mismatch"
        and item["table_id"] == TABLE_ID
        and item["row"] == 1
        and item["col"] == 1
        and item["clip_text"] == "48"
        for item in result.unresolved_conflicts
    )

    sent = provider.verify_calls[0]["context"]["native_context"]
    serialized = json.dumps(sent, ensure_ascii=False, sort_keys=True)
    assert "clip_text" not in serialized
    assert all(
        cell["text"] != "48"
        for table_entry in sent["tables"]
        for row in table_entry["rows"]
        for cell in row["cells"]
    )
    assert sent["tables"][0]["needs_review_cells"] == [[1, 1]]
    assert native.model_native_context(sent)["tables"][0]["needs_review"] == [
        [1, 1]
    ]

    decision = result.native_pages[1]["decision"]
    assert decision["publishable"] is False
    assert any(
        item["reason"] == "crop_third_value"
        for item in decision["unresolved"]
    )
    summary = result.native_pages[1]["source_audit"]
    assert summary["status_counts"]["source_clip_mismatch"] == 1
    assert summary["mismatches"][0]["clip_text"] == "48"
    assert result.page_evidence[1]["native_source_audit_sha256"] == (
        result.native_pages[1]["source_audit_sha256"]
    )

    parsed = _parsed(table)
    apply_vision_result(parsed, result)
    assert _cell_by_coords(parsed.structure.tables[0], 1, 1).text == "84"


def test_source_clip_mismatch_blocks_publication_even_when_crop_confirms_native(
    tmp_path, monkeypatch
):
    table = _native_table()
    table.rows[1].cells[1].text = "84"
    table.rows[1].text = "测试项 | 84"
    mismatch_cell = (TABLE_ID, 1, 1)

    def fake_audit(source_path, *, page_no, tables, source_sha256):
        return _source_audit_result(
            source_path,
            page_no=page_no,
            tables=list(tables),
            source_sha256=source_sha256,
            mismatch_cell=mismatch_cell,
            mismatch_clip="48",
        )

    monkeypatch.setattr(
        "campusguide.ingest.visual.audit_pdf_grid_cells", fake_audit
    )
    bbox = _crop_bbox([90, 40, 160, 70])
    provider = NativeProvider(
        _verify_payload(),
        {bbox: {"observed_text": "84", "uncertain": False}},
    )
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert len(provider.cell_calls) == 1
    decision = result.native_pages[1]["decision"]
    # 独立裁剪确认 native 84 也不足以发布：原件 clip 与 native 不一致本身必须阻断。
    assert decision["publishable"] is True
    assert result.ok is True
    assert result.publishable is False
    conflicts = [
        item
        for item in result.unresolved_conflicts
        if item["kind"] == "native_source_clip_mismatch"
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["table_id"] == TABLE_ID
    assert conflicts[0]["row"] == 1
    assert conflicts[0]["col"] == 1
    assert conflicts[0]["native_text"] == "84"
    assert conflicts[0]["clip_text"] == "48"

    parsed = _parsed(table)
    apply_vision_result(parsed, result)
    assert _cell_by_coords(parsed.structure.tables[0], 1, 1).text == "84"


def test_missing_source_text_layer_blocks_publication_after_crops(
    tmp_path, monkeypatch
):
    table = _native_table()

    def fake_audit(source_path, *, page_no, tables, source_sha256):
        return _source_audit_result(
            source_path,
            page_no=page_no,
            tables=list(tables),
            source_sha256=source_sha256,
            has_text_layer=False,
        )

    monkeypatch.setattr(
        "campusguide.ingest.visual.audit_pdf_grid_cells", fake_audit
    )
    cell_payloads = {
        _crop_bbox(cell.bbox): {
            "observed_text": cell.text,
            "uncertain": False,
        }
        for row in table.rows
        for cell in row.cells
    }
    provider = NativeProvider(_verify_payload(), cell_payloads)
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert len(cell_payloads) == 6
    assert len(provider.cell_calls) == 6
    assert provider.transcribe_calls == []
    # 逐格裁剪都与 native 一致，但源页无文本层仍是显式阻断，不得算成功。
    assert result.native_pages[1]["decision"]["publishable"] is True
    assert result.ok is True
    assert result.publishable is False
    kinds = [item["kind"] for item in result.unresolved_conflicts]
    assert "native_source_text_layer_missing" in kinds
    assert kinds.count("native_source_cell_unresolved") == 6
    summary = result.native_pages[1]["source_audit"]
    assert summary["has_text_layer"] is False
    assert summary["status_counts"]["unresolved_no_text_layer"] == 6


def test_no_differences_uses_one_image_and_audits_additional_content(tmp_path):
    from campusguide.ingest.visual_verify import build_native_grid_context

    table = _native_table()
    provider = NativeProvider(
        _verify_payload(
            additional=[{"kind": "paragraph", "text": "备注：本表为示例"}]
        )
    )
    _, result, source_sha = _prepare(tmp_path, provider, _parsed(table))

    assert result.ok is True
    assert result.publishable is True
    assert result.mode == "native_visual_verified"
    assert result.images_sent == 1
    assert result.cache_hits == 0
    assert len(provider.verify_calls) == 1
    assert provider.cell_calls == []
    assert provider.verify_calls[0]["image_sha256"] == (
        result.page_evidence[1]["image_sha256"]
    )
    audit = result.native_verifications[0]
    assert audit["audit_mode"] == "native_visual_verified"
    assert audit["source_sha256"] == source_sha
    assert audit["image_sha256"] == result.page_evidence[1]["image_sha256"]
    assert audit["decision"]["publishable"] is True
    assert audit["tables"][TABLE_ID]["checked_rows"] == [0, 1, 2]
    assert result.pages[1].tables == []
    assert result.page_proofs[1]["stage"] == "native_grid_verify"
    assert not any(
        item["kind"] == "native_table_unseen"
        for item in result.unresolved_conflicts
    )

    full_context = build_native_grid_context(
        [table],
        page_no=1,
        source_format="pdf",
        page_size=(PAGE_SIZE, PAGE_SIZE),
    )
    sent_context = provider.verify_calls[0]["context"]["native_context"]
    assert sent_context["native_context_sha256"] == (
        full_context["native_context_sha256"]
    )
    sent_cell = sent_context["tables"][0]["rows"][1]["cells"][1]
    assert sent_cell["cell_id"] == f"{TABLE_ID}#r1c1"
    assert sent_cell["bbox"] == [90.0, 40.0, 160.0, 70.0]
    compact_sha = native.payload_digest(native.model_native_context(full_context))
    assert result.native_pages[1]["model_context_sha256"] == compact_sha
    assert result.page_evidence[1]["native_model_context_sha256"] == compact_sha
    cache_files = list((tmp_path / "cache").rglob("native_grid_verify-*.json"))
    assert len(cache_files) == 1
    record = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert record["native_context_sha256"] == full_context["native_context_sha256"]
    assert record["model_context_sha256"] == compact_sha

    parsed = _parsed(table)
    apply_vision_result(parsed, result)
    assert any(
        block.text == "备注：本表为示例" for block in parsed.structure.blocks
    )
    assert "备注：本表为示例" in parsed.text
    assert parsed.structure.tables[0].rows[1].text == "测试项 | 18000"


def test_correction_requires_opt_in_and_real_crop(tmp_path):
    table = _native_table()
    bbox = _crop_bbox([90, 40, 160, 70])
    provider = NativeProvider(
        _verify_payload(
            corrections=[
                {"row": 1, "col": 1, "observed_text": "1800", "reason": "金额少一位"}
            ]
        ),
        {bbox: {"observed_text": "1800", "uncertain": False}},
    )
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert result.ok is True
    assert result.publishable is False
    assert len(provider.cell_calls) == 1
    call = provider.cell_calls[0]
    assert call["bbox"] == bbox
    assert "budget" in call["context"]
    visible_prompt = GeminiVisionProvider._cell_prompt(call["page_no"], call["context"])
    assert "18000" not in visible_prompt
    assert "测试项" not in visible_prompt
    assert "native_context" not in call["context"]
    assert "native_context" not in visible_prompt
    assert not (set(call["context"]) & {"neighbors", "proposal", "pending_cells"})
    visible_context = {
        key: value for key, value in call["context"].items() if key != "budget"
    }
    serialized = json.dumps(visible_context, ensure_ascii=False)
    assert "18000" not in serialized
    assert "测试项" not in serialized

    audit = result.native_verifications[0]
    observation = audit["observations"][0]
    assert observation["crop_bbox"] == list(bbox)
    assert observation["native_bbox"] == [90.0, 40.0, 160.0, 70.0]
    assert observation["parent_image_sha256"] == (
        result.page_evidence[1]["image_sha256"]
    )
    assert any(
        item["kind"] == "native_grid_change_requires_opt_in"
        for item in result.unresolved_conflicts
    )

    crop_path = tmp_path / "cache" / observation["crop_relpath"]
    native.verify_native_crop(
        Path(result.page_evidence[1]["image_path"]), crop_path, bbox
    )
    with Image.open(crop_path) as crop:
        assert crop.size == (bbox[2] - bbox[0], bbox[3] - bbox[1])

    parsed = _parsed(table)
    apply_vision_result(parsed, result)
    cell = _cell_by_coords(parsed.structure.tables[0], 1, 1)
    assert cell.text == "18000"
    assert cell.text_source == "native"
    assert cell.vision_text is None
    assert cell.native_text is None


def test_opt_in_applies_only_cropped_confirmed_cells(tmp_path):
    table = _native_table(review_needs_review=True)
    correction_bbox = _crop_bbox([90, 40, 160, 70])
    review_bbox = _crop_bbox([10, 70, 90, 100])
    provider = NativeProvider(
        _verify_payload(
            corrections=[
                {"row": 1, "col": 1, "observed_text": "1800", "reason": "金额少一位"}
            ]
        ),
        {
            correction_bbox: {"observed_text": "1800", "uncertain": False},
            review_bbox: {"observed_text": "空值", "uncertain": False},
        },
    )
    config = _config(tmp_path, allow_confirmed_native_corrections=True)
    _, result, _ = _prepare(tmp_path, provider, _parsed(table), config)

    assert result.publishable is True
    assert len(provider.cell_calls) == 2
    assert {call["bbox"] for call in provider.cell_calls} == {
        correction_bbox,
        review_bbox,
    }

    parsed = _parsed(table)
    apply_vision_result(parsed, result)
    target = parsed.structure.tables[0]
    corrected = _cell_by_coords(target, 1, 1)
    assert corrected.text == "1800"
    assert corrected.native_text == "18000"
    assert corrected.vision_text == "1800"
    assert corrected.text_source == "vision+native"
    assert corrected.needs_review is False
    assert _cell_by_coords(target, 1, 0).text_source == "native"
    assert _cell_by_coords(target, 2, 0).needs_review is False
    assert _cell_by_coords(target, 2, 1).text == ""
    assert target.rows[1].text == "测试项 | 1800"
    block = next(
        item
        for item in parsed.structure.blocks
        if item.block_id == target.block_id
    )
    assert "测试项 | 1800" in block.text
    assert target.header_paths == [["科目"], ["金额"]]
    coverage = parsed.structure.coverage["vision_native"]
    assert coverage[0]["approved_cells"] == 1
    assert coverage[0]["cleared_needs_review"] == 1


def test_blank_native_invented_code_rejected_by_blank_crop(tmp_path):
    table = _native_table(blank_needs_review=True)
    bbox = _crop_bbox([90, 70, 160, 100])
    provider = NativeProvider(
        _verify_payload(
            corrections=[
                {"row": 2, "col": 1, "observed_text": "CS101", "reason": "看到课程码"}
            ]
        ),
        {bbox: {"observed_text": "", "uncertain": False}},
    )
    config = _config(tmp_path, allow_confirmed_native_corrections=True)
    _, result, _ = _prepare(tmp_path, provider, _parsed(table), config)

    assert result.publishable is True
    decision = result.native_pages[1]["decision"]
    assert decision["approved_corrections"] == []
    assert decision["rejected_proposals"][0]["reason"] == "crop_matches_native"

    parsed = _parsed(table)
    apply_vision_result(parsed, result)
    target = parsed.structure.tables[0]
    blank = _cell_by_coords(target, 2, 1)
    assert blank.text == ""
    assert blank.vision_text is None
    assert blank.needs_review is False
    assert "CS101" not in target.rows[2].text
    assert "CS101" not in parsed.text


def test_unreliable_bbox_blocks_without_crop(tmp_path):
    table = _native_table(blank_needs_review=True, blank_bbox_missing=True)
    provider = NativeProvider(_verify_payload())
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert provider.cell_calls == []
    assert any(
        item["kind"] == "native_cell_bbox_unreliable"
        for item in result.unresolved_conflicts
    )
    assert result.publishable is False
    reasons = {
        item["reason"] for item in result.native_pages[1]["decision"]["unresolved"]
    }
    assert "bbox_unreliable" in reasons


def test_missing_rows_fail_closed_without_transcription_fallback(tmp_path):
    table = _native_table()
    provider = NativeProvider(_verify_payload(checked_rows=(0, 1)))
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert provider.transcribe_calls == []
    assert result.pages == {}
    assert result.status == "failed"
    assert result.publishable is False
    assert any(
        failure["stage"] == "native_grid_verify"
        and failure["code"] == "native_grid_parse"
        for failure in result.failures
    )


def test_rate_limit_aborts_without_transcription_fallback(tmp_path):
    table = _native_table()
    provider = NativeProvider(
        _verify_payload(),
        verify_error=VisionRateLimited(
            "429", http_status=429, retry_after_seconds=180.0
        ),
    )
    _, result, _ = _prepare(tmp_path, provider, _parsed(table))

    assert len(provider.verify_calls) == 1
    assert provider.transcribe_calls == []
    assert result.pages == {}
    assert result.publishable is False
    assert any(
        failure["stage"] == "ratelimit" for failure in result.failures
    )
    assert result.images_sent == 1


def test_cache_reuse_revalidates_and_native_hash_change_invalidates(tmp_path):
    table = _native_table()
    bbox = _crop_bbox([90, 40, 160, 70])
    payload = _verify_payload(
        corrections=[
            {"row": 1, "col": 1, "observed_text": "1800", "reason": "金额少一位"}
        ]
    )
    cells = {bbox: {"observed_text": "1800", "uncertain": False}}
    first_provider = NativeProvider(payload, cells)
    _, first, _ = _prepare(tmp_path, first_provider, _parsed(table))
    assert len(first_provider.verify_calls) == 1
    first_digest = first.native_pages[1]["decision_audit_sha256"]

    second_provider = NativeProvider(payload, cells)
    source = tmp_path / "source.pdf"
    second = _preparer(tmp_path, second_provider).prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_format="pdf",
        parsed=_parsed(table),
    )
    assert second_provider.verify_calls == []
    assert second_provider.cell_calls == []
    assert second.cache_hits == 1
    assert second.images_sent == 0
    assert second.native_pages[1]["cache_hit"] is True
    assert second.native_pages[1]["decision_audit_sha256"] == first_digest
    assert second.native_pages[1]["native_context_sha256"] == (
        first.native_pages[1]["native_context_sha256"]
    )

    changed = _native_table()
    changed.rows[1].cells[1].text = "19000"
    changed.rows[1].text = "测试项 | 19000"
    third_provider = NativeProvider(payload, cells)
    third = _preparer(tmp_path, third_provider).prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_format="pdf",
        parsed=_parsed(changed),
    )
    assert len(third_provider.verify_calls) == 1
    assert third.native_pages[1]["cache_hit"] is False
    assert third.native_pages[1]["native_context_sha256"] != (
        first.native_pages[1]["native_context_sha256"]
    )


def test_tampered_crop_invalidates_cache_and_is_rebuilt(tmp_path):
    table = _native_table()
    bbox = _crop_bbox([90, 40, 160, 70])
    payload = _verify_payload(
        corrections=[
            {"row": 1, "col": 1, "observed_text": "1800", "reason": "金额少一位"}
        ]
    )
    cells = {bbox: {"observed_text": "1800", "uncertain": False}}
    _, first, _ = _prepare(
        tmp_path, NativeProvider(payload, cells), _parsed(table)
    )
    observation = first.native_verifications[0]["observations"][0]
    crop_path = tmp_path / "cache" / observation["crop_relpath"]
    original_sha = observation["crop_image_sha256"]
    crop_path.write_bytes(b"not the real crop")

    provider = NativeProvider(payload, cells)
    source = tmp_path / "source.pdf"
    second = _preparer(tmp_path, provider).prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_format="pdf",
        parsed=_parsed(table),
    )
    assert len(provider.verify_calls) == 1
    assert second.cache_invalidations
    assert second.cache_hits == 0
    rebuilt = second.native_verifications[0]["observations"][0]
    assert rebuilt["crop_image_sha256"] == original_sha
    native.verify_native_crop(
        Path(second.page_evidence[1]["image_path"]),
        tmp_path / "cache" / rebuilt["crop_relpath"],
        bbox,
    )


def test_provider_without_native_methods_keeps_legacy_path(tmp_path):
    provider = LegacyProvider()
    _, result, _ = _prepare(tmp_path, provider, None)

    assert provider.transcribe_calls
    assert result.ok is True
    assert result.publishable is True
    assert result.mode == "vision_page"
    assert result.native_verifications == []


def test_cell_prompt_whitelists_only_local_hints():
    prompt = GeminiVisionProvider._cell_prompt(
        3,
        {
            "grid_id": TABLE_ID,
            "image_width": 40,
            "image_height": 20,
            "native_text": "18000",
            "neighbors": ["测试项"],
            "proposal": "1800",
        },
    )
    assert "18000" not in prompt
    assert "测试项" not in prompt
    assert "neighbors" not in prompt
    assert TABLE_ID in prompt
    assert '"page_no": 3' in prompt


def _cell_by_coords(table: TableStructure, row: int, col: int) -> TableCell:
    for table_row in table.rows:
        for cell in table_row.cells:
            if (
                cell.row == row
                and cell.col == col
                and not cell.covered
            ):
                return cell
    raise AssertionError(f"missing cell {row},{col}")
