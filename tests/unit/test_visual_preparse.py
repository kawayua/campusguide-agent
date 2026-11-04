"""Tests for vision-first preprocessing.

Covers the shared validator, prepare provenance/caching/region refinement,
provider request accounting, HTML renderer integration, independent HTML asset
recognition, 429 handling and apply-time materialization.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import campusguide.ingest.html_visual as html_visual
import campusguide.ingest.visual as visual
from campusguide.ingest.parse import ParseOutput
from campusguide.ingest.structure import (
    AssetResource,
    PageStructure,
    ParsedStructure,
    SourceLocator,
    StructureBlock,
    TableCell,
    TableRow,
    TableStructure,
)
from campusguide.ingest.visual import (
    VISION_CACHE_SCHEMA_VERSION,
    VISION_PROMPT_VERSION,
    EdgeHtmlRenderer,
    GeminiVisionProvider,
    RenderedPage,
    VisionBlock,
    VisionBudgetExceeded,
    VisionConfig,
    VisionImage,
    VisionPage,
    VisionRateLimited,
    VisionRequestBudget,
    VisionResponseError,
    VisionResult,
    VisionTable,
    VisionTableCell,
    VisionUnavailable,
    VisionValidationError,
    VisualPreparser,
    apply_vision_result,
    parse_vision_json,
    validate_vision_page,
)


def _page_payload(**overrides):
    payload = {
        "page": 1,
        "blocks": [{"id": "b1", "kind": "paragraph", "text": "正文"}],
    }
    payload.update(overrides)
    return payload


def _blank_evidence() -> dict:
    return {
        "image_sha256": "a" * 64,
        "image_path": "C:/cache/pages/a.png",
        "width": 100,
        "height": 100,
        "pixel_blank": True,
        "ink_ratio": 0.0,
        "native_content": False,
    }


def _source(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\nvision phase1 fixture\n")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path, **overrides) -> VisionConfig:
    values = {
        "cache_dir": tmp_path / "cache",
        "dpi": 144,
        "max_requests": 10,
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "max_tables_per_page": 8,
    }
    values.update(overrides)
    return VisionConfig(**values)


class FixtureRenderer:
    version = "fixture-renderer/1"

    def __init__(self, pages: int = 1, ink: bool = False) -> None:
        self.pages = pages
        self.ink = ink

    def render(
        self, source_path: Path, *, source_format: str, dpi: int
    ) -> list[RenderedPage]:
        rendered: list[RenderedPage] = []
        for index in range(self.pages):
            image = Image.new("RGB", (100, 100), "white")
            if index or self.ink:
                image.paste("black", (0, 0, 10, 10))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
            rendered.append(
                RenderedPage(
                    page_no=index + 1,
                    image_sha256=hashlib.sha256(png).hexdigest(),
                    width=100,
                    height=100,
                    dpi=dpi,
                    png_base64=base64.b64encode(png).decode("ascii"),
                )
            )
        return rendered


class FixtureProvider:
    version = "fixture-provider/1"
    model = "fixture-gemini"

    def __init__(self, payload, refine_payload=None, asset_payload=None) -> None:
        self.payload = payload
        self.refine_payload = refine_payload
        self.asset_payload = asset_payload
        self.calls: list[dict] = []
        self.asset_calls: list[dict] = []
        self.last_asset_audit: dict = {}

    def recognize_page(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        stage = str(context.get("stage") or "full_page")
        self.calls.append(
            {
                "page_no": page_no,
                "stage": stage,
                "image_sha256": hashlib.sha256(image_png).hexdigest(),
                "context": dict(context),
            }
        )
        payload = (
            self.refine_payload
            if stage != "full_page" and self.refine_payload is not None
            else self.payload
        )
        if callable(payload):
            return payload(page_no)
        return json.loads(json.dumps(payload))

    def recognize_asset(
        self,
        image_bytes: bytes,
        *,
        asset_id: str,
        mime_type: str,
        context: dict,
    ) -> dict:
        self.asset_calls.append(
            {
                "asset_id": asset_id,
                "mime_type": mime_type,
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "context": dict(context),
            }
        )
        payload = self.asset_payload
        if callable(payload):
            payload = payload(asset_id)
        payload = json.loads(json.dumps(payload or {}))
        self.last_asset_audit = {
            "response_sha256": hashlib.sha256(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "model": self.model,
            "prompt_sha256": "c" * 64,
            "mime_type": mime_type,
        }
        return payload


def _prepare(
    tmp_path: Path,
    provider,
    *,
    renderer=None,
    config: VisionConfig | None = None,
    source_sha256: str | None = None,
    parsed=None,
):
    source, sha = _source(tmp_path)
    preparser = VisualPreparser(
        config or _config(tmp_path),
        renderer=renderer or FixtureRenderer(),
        provider=provider,
    )
    result = preparser.prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=source_sha256 or sha,
        source_format="pdf",
        parsed=parsed,
    )
    return preparser, result


def _prepare_again(
    preparser: VisualPreparser, tmp_path: Path, *, parsed=None
) -> VisionResult:
    source = tmp_path / "source.pdf"
    return preparser.prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_format="pdf",
        parsed=parsed,
    )


class FixtureHtmlRenderer(FixtureRenderer):
    version = "fixture-html/1"

    def __init__(self, metadata: dict) -> None:
        super().__init__(pages=1)
        self.last_render_metadata = metadata


def _png_bytes(seed: str = "resource") -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    color = (digest[0], digest[1], digest[2])
    image = Image.new("RGB", (24, 24), "white")
    image.paste(color, (0, 0, 8, 8))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _html_resource(tmp_path: Path, asset_id: str, dom_path: str) -> dict:
    data = _png_bytes(asset_id)
    path = tmp_path / f"resource-{asset_id.replace('#', '-')}.png"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return {
        "asset_id": asset_id,
        "dom_path": dom_path,
        "ref": "src",
        "original_ref": "https://www.scau.edu.cn/demo.png",
        "url": "https://www.scau.edu.cn/demo.png",
        "final_url": "https://www.scau.edu.cn/demo.png",
        "status": "embedded",
        "http_status": 200,
        "bytes": len(data),
        "sha256": digest,
        "content_type": "image/png",
        "image_format": "png",
        "local_path": str(path),
        "cache_path": str(path),
        "source_manifest": None,
        "attempts": 1,
        "cooldown_seconds": 0.0,
        "error": None,
    }


def _html_asset(asset_id: str, dom_path: str, *, factual: bool = True) -> AssetResource:
    return AssetResource(
        asset_id=asset_id,
        kind="image",
        src="https://www.scau.edu.cn/demo.png",
        locator=SourceLocator(format="html", dom_path=dom_path),
        factual=factual,
    )


def _html_parsed(*assets: AssetResource) -> ParseOutput:
    structure = ParsedStructure(
        format="html",
        pages=[PageStructure(page=1, width=100, height=100, has_text_layer=True)],
        assets=list(assets),
    )
    return ParseOutput(
        format="html",
        structure=structure,
        text="学院简介",
        title="学院简介",
        parser_version="test",
    )


def _prepare_html(
    tmp_path: Path,
    provider,
    *,
    parsed: ParseOutput,
    metadata: dict,
    config: VisionConfig | None = None,
):
    source = tmp_path / "source.html"
    source.write_text("<html><body>学院简介</body></html>", encoding="utf-8")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    preparser = VisualPreparser(
        config or _config(tmp_path),
        renderer=FixtureHtmlRenderer(metadata),
        provider=provider,
    )
    result = preparser.prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=sha,
        source_format="html",
        parsed=parsed,
    )
    return preparser, result


def _html_page_payload(images: list[dict] | None = None) -> dict:
    return {
        "page": 1,
        "blocks": [{"id": "b1", "kind": "paragraph", "text": "学院简介"}],
        "images": images or [],
    }


def _cache_record(tmp_path: Path) -> tuple[Path, dict]:
    cache_file = next((tmp_path / "cache").rglob("*.json"))
    return cache_file, json.loads(cache_file.read_text(encoding="utf-8"))


def _page_cache_record(tmp_path: Path) -> tuple[Path, dict]:
    files = [
        path
        for path in (tmp_path / "cache").rglob("*.json")
        if path.parent.name != "regions"
    ]
    assert len(files) == 1, files
    cache_file = files[0]
    return cache_file, json.loads(cache_file.read_text(encoding="utf-8"))


def _native_parsed(amount: str = "18000") -> ParseOutput:
    header = ["科目", "金额"]
    values = ["测试项", amount]
    rows: list[TableRow] = []
    for row_index, row_values in enumerate((header, values)):
        cells = [
            TableCell(
                cell_id=f"native-table#r{row_index}c{col}",
                row=row_index,
                col=col,
                origin_row=row_index,
                origin_col=col,
                text=value,
                is_header=row_index == 0,
            )
            for col, value in enumerate(row_values)
        ]
        rows.append(
            TableRow(
                row_id=f"native-table#row{row_index}",
                row_index=row_index,
                is_header=row_index == 0,
                cells=cells,
                text=" | ".join(row_values),
                locator=SourceLocator(format="pdf", page=1),
            )
        )
    table = TableStructure(
        table_id="native-table-1",
        block_id="native-block-1",
        table_index=0,
        page=1,
        bbox=[10, 10, 90, 60],
        rows=rows,
        header_row_indices=[0],
        column_count=2,
        row_count=2,
        column_headers=header,
        header_paths=[["科目"], ["金额"]],
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=1, bbox=[10, 10, 90, 60]),
    )
    structure = ParsedStructure(
        format="pdf",
        pages=[
            PageStructure(
                page=1,
                width=100,
                height=100,
                text_chars=20,
                has_text_layer=True,
            )
        ],
        tables=[table],
        blocks=[
            StructureBlock(
                block_id=table.block_id,
                order=0,
                kind="table",
                text=f"科目 | 金额\n测试项 | {amount}",
                locator=SourceLocator(
                    format="pdf", page=1, bbox=[10, 10, 90, 60]
                ),
                table_id=table.table_id,
            )
        ],
        text=f"科目 金额 测试项 {amount}",
    )
    return ParseOutput(
        format="pdf",
        structure=structure,
        text=structure.blocks[0].text,
        title="费用明细",
        parser_version="test",
    )


def _vision_table_payload(amount: str, *, bbox: list[float] | None = None) -> dict:
    table: dict = {
        "id": "t1",
        "column_count": 2,
        "header_rows": [0],
        "rows": [["科目", "金额"], ["测试项", amount]],
    }
    if bbox is not None:
        table["bbox"] = bbox
    return {"page": 1, "tables": [table]}


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


def test_parse_rejects_ragged_rows_without_padding():
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "column_count": 3,
                "header_rows": [0],
                "rows": [["甲", "乙", "丙"], ["值1", "值2"]],
            }
        ],
    }
    with pytest.raises(VisionValidationError) as excinfo:
        parse_vision_json(json.dumps(payload, ensure_ascii=False), page_no=1)
    assert excinfo.value.code == "ragged_rows"
    assert "补空" in str(excinfo.value)


@pytest.mark.parametrize(
    ("table", "code"),
    [
        ({"rows": [["甲", "乙"]], "column_count": 3}, "column_count_mismatch"),
        ({"rows": [["甲", "乙"], ["1", "2"]], "row_count": 3}, "row_count_mismatch"),
        ({"rows": []}, "empty_table"),
        (
            {"rows": [["甲", "乙"], ["1", "2"]], "header_rows": [2]},
            "header_index_out_of_range",
        ),
        (
            {"rows": [["甲", "乙"], ["1", "2"]], "header_rows": [0, 0]},
            "header_index_duplicate",
        ),
        (
            {"rows": [["甲", "乙"], ["1", "2"]], "header_paths": [["甲"]]},
            "header_paths_length",
        ),
        (
            {
                "rows": [["甲", "乙"], ["1", "2"]],
                "header_paths": [["甲"], ["丙"]],
            },
            "header_paths_mismatch",
        ),
        (
            {
                "rows": [["甲", "乙"], ["1", "2"]],
                "header_paths": [["甲", "乙"], ["乙"]],
            },
            "header_paths_depth",
        ),
    ],
)
def test_table_structure_failures_are_explicit(table, code):
    payload = {"page": 1, "tables": [{"id": "t1", **table}]}
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(payload, page_no=1)
    assert excinfo.value.code == code
    assert excinfo.value.page_no == 1


@pytest.mark.parametrize(
    ("cells", "rows", "code"),
    [
        (
            [
                {"row": 0, "col": 0, "rowspan": 2, "colspan": 2, "text": "甲"},
                {"row": 1, "col": 1, "text": "乙"},
            ],
            [["甲", ""], ["", ""]],
            "span_overlap",
        ),
        (
            [
                {"row": 0, "col": 0, "rowspan": 1, "colspan": 2, "text": "甲"},
                {
                    "row": 0,
                    "col": 1,
                    "origin_row": 0,
                    "origin_col": 0,
                    "text": "",
                },
            ],
            [["甲", ""]],
            "cell_covered_invalid",
        ),
        (
            [
                {"row": 0, "col": 0, "text": "甲"},
                {
                    "row": 0,
                    "col": 1,
                    "covered": True,
                    "origin_row": 5,
                    "origin_col": 5,
                },
            ],
            [["甲", ""]],
            "cell_origin_missing",
        ),
        (
            [{"row": 0, "col": 0, "rowspan": 3, "colspan": 1, "text": "甲"}],
            [["甲"], ["1"]],
            "span_out_of_bounds",
        ),
    ],
)
def test_merged_cell_geometry_is_validated(cells, rows, code):
    payload = {
        "page": 1,
        "tables": [{"id": "t1", "rows": rows, "cells": cells}],
    }
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(payload, page_no=1)
    assert excinfo.value.code == code


def test_sparse_merged_origin_materializes_from_rectangular_rows():
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "header_rows": [0],
                "rows": [["甲", "乙"], ["1", "2"]],
                "cells": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 1, "text": "甲"}],
            }
        ],
    }
    page = validate_vision_page(payload, page_no=1)
    table = page.tables[0]
    assert table.effective_rows() == [["甲", "乙"], ["1", "2"]]
    # 稀疏 cells 只保留合并原点，其余格由完整 rows 物化，不要求逐格重复。
    assert len(table.cells or []) == 1
    assert table.cells[0].row == 0 and table.cells[0].col == 0


def test_single_level_header_paths_cannot_be_swapped():
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "header_rows": [0],
                "column_count": 2,
                "rows": [["课程", "学时"], ["测试课", "32"]],
                "header_paths": [["学时"], ["课程"]],
            }
        ],
    }
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(payload, page_no=1)
    assert excinfo.value.code == "header_paths_mismatch"
    assert excinfo.value.table_id == "t1"


def test_merged_cells_derive_rectangular_rows_and_header_paths():
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "column_count": 2,
                "header_rows": [0],
                "rows": [["甲", "乙"], ["", ""]],
                "cells": [
                    {
                        "row": 0,
                        "col": 0,
                        "rowspan": 2,
                        "colspan": 1,
                        "text": "甲",
                    },
                    {
                        "row": 0,
                        "col": 1,
                        "rowspan": 2,
                        "colspan": 1,
                        "text": "乙",
                    },
                ],
            }
        ],
    }
    page = validate_vision_page(payload, page_no=1)
    table = page.tables[0]
    assert table.effective_rows() == [["甲", "乙"], ["", ""]]
    assert table.effective_column_count() == 2
    assert table.effective_header_paths() == [["甲"], ["乙"]]
    assert table.row_count == 2

    again = validate_vision_page(page, page_no=1)
    assert again.tables[0].effective_rows() == table.effective_rows()


def test_cells_only_table_derives_rows_grid():
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "cells": [
                    {"row": 0, "col": 0, "text": "科目"},
                    {"row": 0, "col": 1, "text": "金额"},
                    {"row": 1, "col": 0, "text": "测试项"},
                    {"row": 1, "col": 1, "text": "18000"},
                ],
            }
        ],
    }
    page = validate_vision_page(payload, page_no=1)
    assert page.tables[0].rows == [["科目", "金额"], ["测试项", "18000"]]
    assert page.tables[0].column_count == 2
    assert page.tables[0].header_paths == [["科目"], ["金额"]]


@pytest.mark.parametrize(
    ("unit", "bbox", "code"),
    [
        ("pixel", [0, 0, 150, 10], "bbox_out_of_bounds"),
        ("normalized", [0, 0, 1.2, 1], "bbox_out_of_bounds"),
        ("pixel", [10, 0, 5, 10], "bbox_invalid"),
        ("cm", [0, 0, 10, 10], "coordinate_unit_invalid"),
    ],
)
def test_bbox_units_and_bounds_are_checked(unit, bbox, code):
    payload = {
        "page": 1,
        "coordinate_unit": unit,
        "page_width": 100,
        "page_height": 100,
        "blocks": [
            {"id": "b1", "kind": "paragraph", "text": "正文", "bbox": bbox}
        ],
    }
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(payload, page_no=1, page_size=(100, 100))
    assert excinfo.value.code == code


def test_cell_bbox_must_stay_inside_table():
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "bbox": [0, 0, 100, 100],
                "cells": [
                    {
                        "row": 0,
                        "col": 0,
                        "text": "甲",
                        "bbox": [0, 0, 120, 20],
                    }
                ],
            }
        ],
    }
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(payload, page_no=1, page_size=(200, 200))
    assert excinfo.value.code == "cell_bbox_outside_table"


def test_declared_yxyx_order_is_converted_once_to_xyxy():
    payload = {
        "page": 1,
        "coordinate_unit": "normalized",
        "coordinate_order": "yxyx",
        "page_width": 1000,
        "page_height": 500,
        "blocks": [
            {"id": "b1", "kind": "paragraph", "text": "Example", "bbox": [0.2, 0.1, 0.4, 0.9]}
        ],
    }
    page = validate_vision_page(payload, page_no=1, page_size=(1000, 500))
    assert page.blocks[0].bbox == [0.1, 0.2, 0.9, 0.4]
    assert page.coordinate_order == "yxyx"
    assert page.coordinate_order_applied is True

    again = validate_vision_page(page, page_no=1, page_size=(1000, 500))
    assert again.blocks[0].bbox == [0.1, 0.2, 0.9, 0.4]


def test_missing_coordinate_order_keeps_legacy_xyxy():
    payload = {
        "page": 1,
        "coordinate_unit": "normalized",
        "page_width": 1000,
        "page_height": 500,
        "blocks": [
            {"id": "b1", "kind": "paragraph", "text": "Example", "bbox": [0.1, 0.2, 0.9, 0.4]}
        ],
    }
    page = validate_vision_page(payload, page_no=1, page_size=(1000, 500))
    assert page.blocks[0].bbox == [0.1, 0.2, 0.9, 0.4]
    assert page.coordinate_order is None
    again = validate_vision_page(page, page_no=1, page_size=(1000, 500))
    assert again.blocks[0].bbox == [0.1, 0.2, 0.9, 0.4]


def test_unknown_coordinate_order_is_rejected():
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(
            {
                "page": 1,
                "coordinate_order": "unknown",
                "blocks": [{"id": "b1", "kind": "paragraph", "text": "正文"}],
            },
            page_no=1,
        )
    assert excinfo.value.code == "coordinate_order_invalid"


def test_literal_unicode_escapes_decode_without_breaking_latex():
    payload = {
        "page": 1,
        "blocks": [
            {
                "id": "b1",
                "kind": "paragraph",
                "text": r"值=\frac{\u5b66\u5206}{2}\times3",
            }
        ],
    }
    page = validate_vision_page(payload, page_no=1)
    assert page.blocks[0].text == r"值=\frac{学分}{2}\times3"


def test_formula_block_kind_is_accepted():
    payload = {
        "page": 1,
        "blocks": [{"id": "b1", "kind": "formula", "text": "E=mc^2"}],
    }
    page = validate_vision_page(payload, page_no=1)
    assert page.blocks[0].kind == "formula"


def test_page_number_errors_are_rejected():
    with pytest.raises(VisionValidationError) as mismatch:
        validate_vision_page(_page_payload(page=2), page_no=1)
    assert mismatch.value.code == "page_mismatch"

    with pytest.raises(VisionValidationError) as invalid:
        validate_vision_page(_page_payload(), page_no=0)
    assert invalid.value.code == "page_number_invalid"


def test_duplicate_ids_are_rejected():
    payload = {
        "page": 1,
        "blocks": [{"id": "x", "kind": "paragraph", "text": "正文"}],
        "tables": [{"id": "x", "rows": [["甲"]]}],
    }
    with pytest.raises(VisionValidationError) as excinfo:
        validate_vision_page(payload, page_no=1)
    assert excinfo.value.code == "duplicate_id"


def test_empty_page_needs_explicit_blank_and_render_evidence():
    with pytest.raises(VisionValidationError) as empty:
        validate_vision_page({"page": 1}, page_no=1)
    assert empty.value.code == "empty_page"

    with pytest.raises(VisionValidationError) as no_evidence:
        validate_vision_page({"page": 1, "blank": True}, page_no=1, allow_blank=True)
    assert no_evidence.value.code == "blank_without_render_evidence"

    with pytest.raises(VisionValidationError) as not_allowed:
        validate_vision_page(
            {"page": 1, "blank": True},
            page_no=1,
            blank_evidence=_blank_evidence(),
        )
    assert not_allowed.value.code == "blank_not_allowed"

    with pytest.raises(VisionValidationError) as with_content:
        validate_vision_page(
            {
                "page": 1,
                "blank": True,
                "blocks": [{"id": "b1", "kind": "paragraph", "text": "正文"}],
            },
            page_no=1,
            allow_blank=True,
            blank_evidence=_blank_evidence(),
        )
    assert with_content.value.code == "blank_with_content"

    page = validate_vision_page(
        {"page": 1, "blank": True},
        page_no=1,
        allow_blank=True,
        blank_evidence=_blank_evidence(),
    )
    assert page.blank is True
    assert any("空白页" in warning for warning in page.warnings)


def test_blank_claim_requires_blank_pixels_and_no_native_content():
    with pytest.raises(VisionValidationError) as not_blank:
        validate_vision_page(
            {"page": 1, "blank": True},
            page_no=1,
            allow_blank=True,
            blank_evidence={
                **_blank_evidence(),
                "pixel_blank": False,
                "ink_ratio": 0.2,
            },
        )
    assert not_blank.value.code == "blank_image_not_blank"

    with pytest.raises(VisionValidationError) as native_content:
        validate_vision_page(
            {"page": 1, "blank": True},
            page_no=1,
            allow_blank=True,
            blank_evidence={**_blank_evidence(), "native_content": True},
        )
    assert native_content.value.code == "blank_with_native_content"


def test_missing_ids_and_unknown_kinds_are_normalized_with_warnings():
    payload = {
        "page": 1,
        "blocks": [{"kind": "caption", "text": "正文"}],
    }
    page = validate_vision_page(payload, page_no=1)
    assert page.blocks[0].id == "b1"
    assert page.blocks[0].kind == "other"
    assert any("缺少 id" in warning for warning in page.warnings)
    assert any("kind 无法识别" in warning for warning in page.warnings)


def test_legacy_rectangular_table_interface_remains_supported():
    table = VisionTable(
        id="t1",
        column_count=2,
        rows=[["科目", "金额"], ["测试项", "18000"]],
    )
    assert table.effective_rows() == [["科目", "金额"], ["测试项", "18000"]]
    assert table.effective_column_count() == 2
    assert table.effective_header_paths() == [["科目"], ["金额"]]
    assert table.cells is None
    assert table.bbox is None


# ---------------------------------------------------------------------------
# prepare: source provenance, blank/empty results, cache
# ---------------------------------------------------------------------------


def test_prepare_verifies_source_sha256_before_any_request(tmp_path):
    provider = FixtureProvider(_page_payload())
    with pytest.raises(VisionUnavailable) as excinfo:
        _prepare(tmp_path, provider, source_sha256="0" * 64)
    assert provider.calls == []
    assert "SHA-256" in str(excinfo.value)


def test_prepare_rejects_missing_source_file(tmp_path):
    preparser = VisualPreparser(
        _config(tmp_path),
        renderer=FixtureRenderer(),
        provider=FixtureProvider(_page_payload()),
    )
    with pytest.raises(VisionUnavailable):
        preparser.prepare(
            source_path=tmp_path / "missing.pdf",
            doc_id="doc",
            version_id="ver",
            source_sha256="0" * 64,
            source_format="pdf",
        )


def test_prepare_rejects_empty_provider_result_after_one_region_retry(tmp_path):
    provider = FixtureProvider({"page": 1})
    _, result = _prepare(tmp_path, provider)
    assert result.ok is False
    assert result.pages_processed == 0
    assert result.publishable is False
    assert result.images_sent == 2
    assert [request["stage"] for request in result.requests] == [
        "full_page",
        "full_page_refine",
    ]
    assert result.requests[0]["attempt"] == 1
    assert any(failure["code"] == "empty_page" for failure in result.failures)
    assert any(
        conflict["kind"] == "refinement_invalid"
        for conflict in result.unresolved_conflicts
    )
    assert not list((tmp_path / "cache").rglob("*.json"))


def test_prepare_accepts_blank_page_with_render_evidence(tmp_path):
    provider = FixtureProvider({"page": 1, "blank": True})
    _, result = _prepare(tmp_path, provider)
    assert result.ok is True
    assert result.publishable is True
    assert result.pages_processed == 1
    assert result.pages[1].blank is True
    assert result.budget is not None
    assert set(result.page_renders) == {1}
    assert result.provider is provider
    assert result.page_proofs[1]["validated"] is True
    assert result.page_proofs[1]["cache_hit"] is False
    summary = result.summary()
    assert summary["publishable"] is True
    assert summary["page_proofs"][0]["page"] == 1
    assert summary["source_sha256"]
    assert summary["page_status"]["1"]["blank"] is True
    assert "budget" not in summary
    assert "page_renders" not in summary
    evidence = summary["page_evidence"]["1"]
    image_path = Path(evidence["image_path"])
    assert image_path.is_file()
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == evidence[
        "image_sha256"
    ]
    assert summary["images_sent"] == 1


def test_prepare_cache_hit_revalidates_and_reuses_page(tmp_path):
    provider = FixtureProvider(
        {
            "page": 1,
            "tables": [
                {
                    "id": "t1",
                    "column_count": 2,
                    "rows": [["科目", "金额"], ["测试项", "18000"]],
                }
            ],
        }
    )
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True
    assert len(provider.calls) == 1

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 1
    assert second.images_sent == 0
    assert len(provider.calls) == 1

    cache_file, record = _cache_record(tmp_path)
    assert cache_file.is_file()
    assert record["schema_version"] == VISION_CACHE_SCHEMA_VERSION
    assert record["prompt_version"] == VISION_PROMPT_VERSION
    for key in (
        "source_sha256",
        "image_sha256",
        "render_sha256",
        "prompt_sha256",
        "provider_version",
        "renderer_version",
        "model",
        "dpi",
        "image_path",
        "image_relpath",
    ):
        assert record.get(key)
    assert record["page"]["tables"][0]["rows"][1][1] == "18000"


def test_cached_yxyx_page_is_not_swapped_twice(tmp_path):
    payload = {
        "page": 1,
        "coordinate_unit": "normalized",
        "coordinate_order": "yxyx",
        "page_width": 100,
        "page_height": 100,
        "blocks": [
            {"id": "b1", "kind": "paragraph", "text": "正文", "bbox": [0.1, 0.2, 0.4, 0.9]}
        ],
    }
    provider = FixtureProvider(payload)
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True
    assert first.pages[1].blocks[0].bbox == [0.2, 0.1, 0.9, 0.4]

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 1
    assert second.pages[1].blocks[0].bbox == [0.2, 0.1, 0.9, 0.4]

    _, record = _cache_record(tmp_path)
    assert record["page"]["coordinate_order"] == "yxyx"
    assert record["page"]["coordinate_order_applied"] is True


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "prompt_sha256",
        "source_sha256",
        "image_sha256",
        "render_sha256",
        "provider_version",
        "dpi",
    ],
)
def test_cache_metadata_mismatch_forces_revalidation(tmp_path, field):
    provider = FixtureProvider(_page_payload())
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True

    cache_file, record = _cache_record(tmp_path)
    tampered = 96 if field == "dpi" else "mismatch"
    record[field] = tampered
    cache_file.write_text(json.dumps(record), encoding="utf-8")

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 0
    assert second.images_sent == 1
    assert len(provider.calls) == 2

    repaired = json.loads(cache_file.read_text(encoding="utf-8"))
    assert repaired[field] != tampered


def test_tampered_cache_page_is_not_accepted(tmp_path):
    provider = FixtureProvider(
        {
            "page": 1,
            "tables": [
                {
                    "id": "t1",
                    "rows": [["科目", "金额"], ["测试项", "18000"]],
                }
            ],
        }
    )
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True

    cache_file, record = _cache_record(tmp_path)
    record["page"]["tables"][0]["rows"][1] = ["18000"]
    cache_file.write_text(json.dumps(record), encoding="utf-8")

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 0
    assert len(provider.calls) == 2
    repaired = json.loads(cache_file.read_text(encoding="utf-8"))
    assert repaired["page"]["tables"][0]["rows"][1] == ["测试项", "18000"]


def test_invalid_cache_and_empty_response_cannot_succeed(tmp_path):
    provider = FixtureProvider(_page_payload())
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True

    cache_file, record = _cache_record(tmp_path)
    record["page"] = {"page": 1}
    cache_file.write_text(json.dumps(record), encoding="utf-8")
    provider.payload = {"page": 1}

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is False
    assert second.pages_processed == 0
    assert {failure["stage"] for failure in second.failures} == {
        "cache",
        "recognize",
    }
    assert all(
        failure["code"] == "empty_page" for failure in second.failures
    )
    assert second.cache_invalidations
    assert second.cache_invalidations[0]["code"] == "empty_page"
    assert second.cache_invalidations[0]["reason"]
    assert second.cache_invalidations[0]["cache_path"]


def test_cache_hit_repairs_tampered_page_image(tmp_path):
    provider = FixtureProvider(_page_payload())
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True

    _, record = _cache_record(tmp_path)
    image_path = Path(record["image_path"])
    image_path.write_bytes(b"not-a-png")

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 1
    assert len(provider.calls) == 1
    assert (
        hashlib.sha256(image_path.read_bytes()).hexdigest()
        == record["image_sha256"]
    )


def test_cached_page_revalidates_header_geometry(tmp_path):
    payload = {
        "page": 1,
        "tables": [
            {
                "id": "t1",
                "header_rows": [0],
                "column_count": 2,
                "rows": [["课程", "学时"], ["测试课", "32"]],
            }
        ],
    }
    provider = FixtureProvider(payload)
    preparser, first = _prepare(tmp_path, provider)
    assert first.ok is True

    cache_file, record = _cache_record(tmp_path)
    record["page"]["tables"][0]["header_paths"] = [["学时"], ["课程"]]
    cache_file.write_text(json.dumps(record), encoding="utf-8")

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 0
    assert len(provider.calls) == 2
    assert second.cache_invalidations[0]["code"] == "header_paths_mismatch"

    repaired = json.loads(cache_file.read_text(encoding="utf-8"))
    assert repaired["page"]["tables"][0]["header_paths"] == [["课程"], ["学时"]]


def test_cached_blank_page_still_requires_blank_pixels(tmp_path):
    provider = FixtureProvider(_page_payload())
    preparser, first = _prepare(
        tmp_path, provider, renderer=FixtureRenderer(ink=True)
    )
    assert first.ok is True

    cache_file, record = _cache_record(tmp_path)
    record["page"] = {"page": 1, "blank": True}
    cache_file.write_text(json.dumps(record), encoding="utf-8")

    second = _prepare_again(preparser, tmp_path)
    assert second.ok is True
    assert second.cache_hits == 0
    assert len(provider.calls) == 2
    assert second.cache_invalidations[0]["code"] == "blank_image_not_blank"
    repaired = json.loads(cache_file.read_text(encoding="utf-8"))
    assert repaired["page"]["blank"] is False


def test_provider_blank_claim_cannot_hide_ink_on_page(tmp_path):
    provider = FixtureProvider({"page": 1, "blank": True})
    _, result = _prepare(
        tmp_path, provider, renderer=FixtureRenderer(ink=True)
    )
    assert result.ok is False
    assert result.publishable is False
    assert any(
        failure["code"] == "blank_image_not_blank"
        for failure in result.failures
    )
    assert not list((tmp_path / "cache").rglob("*.json"))


# ---------------------------------------------------------------------------
# Gemini provider: retry accounting, prompt boundary, region context
# ---------------------------------------------------------------------------


class StubResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class StubClient:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _completion(payload: dict) -> StubResponse:
    return StubResponse(
        200,
        {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
    )


@pytest.fixture()
def vision_env(monkeypatch):
    monkeypatch.setenv("TEST_VISION_MODEL", "fixture-gemini")
    monkeypatch.setenv("TEST_VISION_BASE_URL", "https://vision.invalid/v1")
    monkeypatch.setenv("TEST_VISION_API_KEY", "fixture-key")
    return monkeypatch


def _provider_config(tmp_path: Path, **overrides) -> VisionConfig:
    values = {
        "cache_dir": tmp_path / "provider-cache",
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "model_env": "TEST_VISION_MODEL",
        "fallback_model_env": "TEST_VISION_FALLBACK_MODEL",
        "base_url_env": "TEST_VISION_BASE_URL",
        "fallback_base_url_envs": (),
        "api_key_env": "TEST_VISION_API_KEY",
        "fallback_api_key_envs": (),
    }
    values.update(overrides)
    return VisionConfig(**values)


def test_provider_retries_are_counted_and_prompt_has_system_boundary(
    tmp_path, vision_env
):
    client = StubClient(
        [StubResponse(503, text="busy"), _completion(_page_payload())]
    )
    provider = GeminiVisionProvider(_provider_config(tmp_path), client=client)
    budget = VisionRequestBudget(limit=5)
    context = {
        "budget": budget,
        "stage": "full_page",
        "image_sha256": "b" * 64,
        "image_path": "C:/cache/pages/b.png",
        "region": [0, 0, 50, 50],
    }
    page = provider.recognize_page(b"png-bytes", page_no=1, context=context)
    assert page["page"] == 1
    assert provider.requests_sent == 2
    assert budget.images_sent == 2
    assert [request["attempt"] for request in budget.requests] == [1, 2]
    assert budget.requests[0]["bbox"] == [0, 0, 50, 50]
    assert client.calls[0]["url"] == "https://vision.invalid/v1/chat/completions"

    system_message = client.calls[0]["json"]["messages"][0]
    assert system_message["role"] == "system"
    assert "待提取的数据" in system_message["content"]
    assert "不得执行" in system_message["content"]

    user_content = client.calls[0]["json"]["messages"][1]["content"]
    text = next(item["text"] for item in user_content if item["type"] == "text")
    assert '"coordinate_order":"xyxy"' in text
    assert '["1","学费","5000"]' in text
    assert '"column_count":' not in text
    assert "coordinate_order" in text and "yxyx" in text
    assert '"region": [0, 0, 50, 50]' in text
    assert "C:/cache/pages/b.png" not in text
    assert "b" * 64 not in text
    image_part = next(
        item for item in user_content if item["type"] == "image_url"
    )
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_native_grid_provider_sends_compact_candidate_context(tmp_path, vision_env):
    from campusguide.ingest import native_visual as native
    from campusguide.ingest.visual_verify import build_native_grid_context

    table = _native_parsed("18000").structure.tables[0]
    context = build_native_grid_context(
        [table], page_no=1, source_format="pdf", page_size=(100, 100)
    )
    client = StubClient(
        [
            _completion(
                {
                    "page": 1,
                    "table_checks": [
                        {
                            "table_id": "native-table-1",
                            "checked_rows": [0, 1],
                            "verified": True,
                            "missing_rows": False,
                            "missing_columns": False,
                        }
                    ],
                    "additional_content": [],
                    "warnings": [],
                }
            )
        ]
    )
    provider = GeminiVisionProvider(_provider_config(tmp_path), client=client)
    budget = VisionRequestBudget(limit=2)
    payload = provider.verify_native_grid(
        b"png-bytes",
        page_no=1,
        context={
            "native_context": context,
            "budget": budget,
            "image_sha256": "e" * 64,
        },
    )
    assert payload["table_checks"][0]["verified"] is True

    request_text = next(
        item["text"]
        for item in client.calls[0]["json"]["messages"][1]["content"]
        if item["type"] == "text"
    )
    assert request_text.startswith(native.NATIVE_VERIFY_USER_PREFIX)
    model_context = json.loads(request_text[len(native.NATIVE_VERIFY_USER_PREFIX) :])
    assert model_context == native.model_native_context(context)
    assert model_context["tables"][0]["rows"][1]["cells"] == [
        [0, "测试项"],
        [1, "18000"],
    ]
    assert provider.last_native_audit["model_context_sha256"] == (
        native.payload_digest(model_context)
    )
    assert budget.images_sent == 1

    serialized = json.dumps(client.calls[0]["json"], ensure_ascii=False)
    for forbidden in (
        "cell_id",
        "bbox",
        "row_indices",
        "header_row_indices",
        "column_headers",
        "native_context_sha256",
        "text_source",
    ):
        assert forbidden not in serialized
    assert "测试项 | 18000" not in serialized
    assert "data:image/png;base64," in serialized


def test_provider_retry_cannot_exceed_request_budget(tmp_path, vision_env):
    client = StubClient(
        [StubResponse(500, text="busy"), _completion(_page_payload())]
    )
    provider = GeminiVisionProvider(_provider_config(tmp_path), client=client)
    budget = VisionRequestBudget(limit=1)
    with pytest.raises(VisionBudgetExceeded):
        provider.recognize_page(
            b"png-bytes",
            page_no=1,
            context={"budget": budget, "image_sha256": "c" * 64},
        )
    assert provider.requests_sent == 1
    assert budget.images_sent == 1
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# apply compatibility for the new models
# ---------------------------------------------------------------------------


def _blank_parsed() -> ParseOutput:
    return ParseOutput(
        format="pdf",
        structure=ParsedStructure(format="pdf"),
        text="",
        title="扫描文档",
        parser_version="test",
    )


def test_apply_vision_result_adds_table_to_blank_pdf():
    parsed = _blank_parsed()
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                column_count=2,
                rows=[["科目", "金额"], ["测试项", "18000"]],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    apply_vision_result(parsed, result)
    table = parsed.structure.tables[0]
    assert table.rows[1].cells[1].text == "18000"
    assert table.column_count == 2
    assert table.vision["source"] == "vision_only"
    assert table.vision["needs_review"] is True
    assert table.vision["bbox_unit"] == "pixel"
    assert table.source == "vision"
    assert any("需复核" in warning for warning in table.warnings)
    assert parsed.vision["ok"] is True


def test_apply_vision_result_accepts_cells_only_table():
    parsed = _blank_parsed()
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                cells=[
                    VisionTableCell(row=0, col=0, text="科目"),
                    VisionTableCell(row=0, col=1, text="金额"),
                    VisionTableCell(row=1, col=0, text="测试项"),
                    VisionTableCell(row=1, col=1, text="18000"),
                ],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    apply_vision_result(parsed, result)
    table = parsed.structure.tables[0]
    assert table.rows[1].cells[1].text == "18000"
    assert table.column_headers == ["科目", "金额"]


# ---------------------------------------------------------------------------
# Phase 2: region refinement, conflict publication and scanned materialization
# ---------------------------------------------------------------------------


class FailingProvider:
    version = "failing-provider/1"
    model = "fixture-gemini"

    def __init__(self) -> None:
        self.calls = 0

    def recognize_page(self, image_png, *, page_no: int, context: dict) -> dict:
        self.calls += 1
        raise VisionResponseError("供应商不可用")


def test_region_refinement_uses_real_crop_and_records_provenance(tmp_path):
    provider = FixtureProvider(
        _vision_table_payload("1800", bbox=[10, 10, 90, 60]),
        refine_payload=_vision_table_payload("18000"),
    )
    parsed = _native_parsed("18000")
    preparser, result = _prepare(tmp_path, provider, parsed=parsed)

    assert result.ok is True
    assert result.publishable is True
    assert [call["stage"] for call in provider.calls] == [
        "full_page",
        "region_refine",
    ]
    assert provider.calls[0]["image_sha256"] != provider.calls[1]["image_sha256"]
    assert result.images_sent == 2
    assert result.refinement_requests == 1
    assert result.refinement_cache_hits == 0
    assert result.page_proofs[1]["stage"] == "full_page_refined"
    assert result.page_proofs[1]["refined"] is True
    assert result.pages[1].tables[0].effective_rows()[1][1] == "18000"

    record = result.refinements[0]
    assert record["stage"] == "region_refine"
    assert record["reason"] == "numeric_conflict"
    assert record["table_id"] == "t1"
    assert record["parent_image_sha256"] == result.page_evidence[1]["image_sha256"]
    assert record["crop_image_sha256"] == provider.calls[1]["image_sha256"]
    assert record["base_bbox"] == [10.0, 10.0, 90.0, 60.0]
    assert record["crop_bbox"] == [7, 7, 93, 63]
    assert record["validated"] is True
    assert record["cache_hit"] is False
    crop_path = tmp_path / "cache" / record["crop_relpath"]
    assert crop_path.is_file()
    assert (
        hashlib.sha256(crop_path.read_bytes()).hexdigest()
        == record["crop_image_sha256"]
    )
    assert not result.unresolved_conflicts

    cache_file, cached = _page_cache_record(tmp_path)
    cached["page"] = _vision_table_payload("1800", bbox=[10, 10, 90, 60])
    cache_file.write_text(json.dumps(cached), encoding="utf-8")

    third = _prepare_again(preparser, tmp_path, parsed=parsed)
    assert third.ok is True
    assert third.publishable is True
    assert third.cache_hits == 1
    assert third.refinement_cache_hits == 1
    assert len(provider.calls) == 2
    assert third.refinements[-1]["cache_hit"] is True
    repaired = json.loads(cache_file.read_text(encoding="utf-8"))
    assert repaired["page"]["tables"][0]["rows"][1][1] == "18000"


def test_region_refinement_stops_when_request_budget_is_spent(tmp_path):
    provider = FixtureProvider(
        _vision_table_payload("1800", bbox=[10, 10, 90, 60]),
        refine_payload=_vision_table_payload("18000"),
    )
    parsed = _native_parsed("18000")
    _, result = _prepare(
        tmp_path,
        provider,
        parsed=parsed,
        config=_config(tmp_path, max_requests=1),
    )

    assert result.ok is True
    assert result.publishable is False
    assert result.images_sent == 1
    assert result.refinement_requests == 0
    assert len(provider.calls) == 1
    kinds = {conflict["kind"] for conflict in result.unresolved_conflicts}
    assert "numeric_conflict" in kinds
    assert "refinement_budget_exhausted" in kinds


def test_failed_generic_provider_attempt_counts_toward_budget(tmp_path):
    provider = FailingProvider()
    _, result = _prepare(
        tmp_path, provider, config=_config(tmp_path, max_requests=1)
    )

    assert result.ok is False
    assert result.publishable is False
    assert result.images_sent == 1
    assert provider.calls == 1
    assert result.requests[0]["stage"] == "full_page"
    assert result.failures[0]["code"] == "recognize_failed"


def test_validation_failure_keeps_code_page_and_table_id(tmp_path):
    provider = FixtureProvider(
        {
            "page": 1,
            "tables": [
                {
                    "id": "t1",
                    "column_count": 3,
                    "rows": [["甲", "乙", "丙"], ["值1", "值2"]],
                }
            ],
        }
    )
    _, result = _prepare(tmp_path, provider)

    assert result.ok is False
    assert result.images_sent == 2
    failure = next(
        item for item in result.failures if item["stage"] == "recognize"
    )
    assert failure["code"] == "ragged_rows"
    assert failure["page"] == 1
    assert failure["table_id"] == "t1"
    conflict = next(
        item
        for item in result.unresolved_conflicts
        if item["kind"] == "refinement_invalid"
    )
    assert conflict["table_id"] == "t1"
    assert conflict["code"] == "ragged_rows"


def test_numeric_conflict_blocks_publication_and_keeps_native_value(tmp_path):
    provider = FixtureProvider(
        _vision_table_payload("1800", bbox=[10, 10, 90, 60])
    )
    parsed = _native_parsed("18000")
    _, result = _prepare(
        tmp_path,
        provider,
        parsed=parsed,
        config=_config(tmp_path, max_region_refinements_per_page=0),
    )

    assert result.ok is True
    assert result.publishable is False
    conflict = next(
        item
        for item in result.unresolved_conflicts
        if item["kind"] == "numeric_conflict"
    )
    assert conflict["native_text"] == "18000"
    assert conflict["vision_text"] == "1800"

    apply_vision_result(parsed, result)
    table = parsed.structure.tables[0]
    cell = table.rows[1].cells[1]
    assert cell.text == "18000"
    assert cell.native_text == "18000"
    assert cell.vision_text == "1800"
    assert cell.text_source is None
    assert cell.needs_review is True
    assert table.vision["source"] == "vision_cross_check"
    assert table.vision["needs_review"] is True
    assert parsed.vision["publishable"] is False
    assert any("禁止发布" in warning for warning in parsed.structure.warnings)


def test_high_confidence_omission_of_native_table_is_not_publishable(tmp_path):
    provider = FixtureProvider(
        {
            "page": 1,
            "confidence": 0.99,
            "blocks": [{"id": "b1", "kind": "heading", "text": "费用明细"}],
        }
    )
    parsed = _native_parsed("18000")
    _, result = _prepare(tmp_path, provider, parsed=parsed)

    assert result.ok is True
    assert result.publishable is False
    assert any(
        item["kind"] == "native_table_unseen"
        for item in result.unresolved_conflicts
    )
    assert result.pages[1].tables == []


def _crop_evidence(*, refined_rows: list[list[str]] | None = None) -> dict:
    record = {
        "page": 1,
        "stage": "region_refine",
        "reason": "numeric_conflict",
        "table_id": "t1",
        "parent_image_sha256": "b" * 64,
        "crop_image_sha256": "c" * 64,
        "crop_bbox": [0.0, 0.0, 100.0, 100.0],
        "base_bbox": [10.0, 10.0, 90.0, 60.0],
        "validated": True,
        "cache_hit": False,
    }
    if refined_rows is not None:
        record["refined_tables"] = [
            {"id": "t1", "header_rows": [0], "rows": refined_rows, "bbox": None}
        ]
    return record


@pytest.mark.parametrize(
    ("native_value", "vision_value"), [("12", "21"), ("18000", "10800")]
)
@pytest.mark.parametrize("with_evidence", [False, True])
def test_plain_numeric_permutation_keeps_native_and_conflicts(
    native_value, vision_value, with_evidence
):
    parsed = _native_parsed(native_value)
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                header_rows=[0],
                rows=[["科目", "金额"], ["测试项", vision_value]],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    if with_evidence:
        result.refinements.append(_crop_evidence())
    apply_vision_result(parsed, result)

    cell = parsed.structure.tables[0].rows[1].cells[1]
    assert cell.native_text == native_value
    assert cell.vision_text == vision_value
    assert cell.text == native_value
    assert cell.text_source is None
    assert cell.needs_review is True
    assert any(
        item["kind"] == "numeric_conflict"
        for item in result.unresolved_conflicts
    )


def test_native_newline_extraction_order_is_adopted_only_with_refined_rows():
    parsed = _native_parsed("12\n34")
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                header_rows=[0],
                rows=[["科目", "金额"], ["测试项", "3412"]],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    result.refinements.append(
        _crop_evidence(refined_rows=[["科目", "金额"], ["测试项", "3412"]])
    )
    apply_vision_result(parsed, result)

    cell = parsed.structure.tables[0].rows[1].cells[1]
    assert cell.native_text == "12\n34"
    assert cell.vision_text == "3412"
    assert cell.text == "3412"
    assert cell.text_source == "vision"
    assert cell.needs_review is False
    assert not any(
        item["kind"] == "numeric_conflict"
        for item in result.unresolved_conflicts
    )


def test_text_mismatch_keeps_native_and_blocks_publication():
    parsed = _native_parsed("测试课")
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                header_rows=[0],
                rows=[["科目", "金额"], ["测试项", "测试课程"]],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    result.refinements.append(_crop_evidence())
    apply_vision_result(parsed, result)

    cell = parsed.structure.tables[0].rows[1].cells[1]
    assert cell.text == "测试课"
    assert cell.text_source is None
    assert cell.needs_review is True
    assert any(
        item["kind"] == "text_conflict" for item in result.unresolved_conflicts
    )


@pytest.mark.parametrize("with_refined_rows", [False, True])
def test_empty_native_value_adopted_only_with_refined_rows(with_refined_rows):
    parsed = _native_parsed("")
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                header_rows=[0],
                rows=[["科目", "金额"], ["测试项", "3"]],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    result.refinements.append(
        _crop_evidence(
            refined_rows=(
                [["科目", "金额"], ["测试项", "3"]]
                if with_refined_rows
                else None
            )
        )
    )
    apply_vision_result(parsed, result)

    cell = parsed.structure.tables[0].rows[1].cells[1]
    if with_refined_rows:
        assert cell.text == "3"
        assert cell.text_source == "vision"
        assert cell.needs_review is False
        assert not any(
            item["kind"] == "empty_native_value_unconfirmed"
            for item in result.unresolved_conflicts
        )
    else:
        assert cell.text == ""
        assert cell.text_source is None
        assert cell.needs_review is True
        assert any(
            item["kind"] == "empty_native_value_unconfirmed"
            for item in result.unresolved_conflicts
        )


def test_apply_materializes_scanned_body_and_table_for_segmentation():
    parsed = _blank_parsed()
    page = VisionPage(
        page=1,
        reading_order=["b1", "t1"],
        blocks=[
            VisionBlock(
                id="b1",
                kind="heading",
                text="费用明细",
                bbox=[10, 10, 90, 30],
            )
        ],
        tables=[
            VisionTable(
                id="t1",
                column_count=2,
                header_rows=[0],
                rows=[["科目", "金额"], ["测试项", "18000"]],
                bbox=[10, 40, 90, 90],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    apply_vision_result(parsed, result)

    structure = parsed.structure
    heading = next(
        block for block in structure.blocks if block.kind == "heading"
    )
    table_block = next(
        block for block in structure.blocks if block.kind == "table"
    )
    table = structure.tables[0]
    assert heading.text in parsed.text
    assert table.source == "vision"
    assert [cell.col for cell in table.rows[0].cells] == [0, 1]
    assert [cell.col for cell in table.rows[1].cells] == [0, 1]
    assert table.rows[0].cells[0].is_header is True
    assert table.rows[1].cells[1].is_header is False
    assert table.rows[1].cells[1].text == "18000"
    assert table.rows[1].cells[1].text_source == "vision"
    assert table_block.table_id == table.table_id
    assert table_block.locator.char_start is not None
    assert (
        parsed.text[
            table_block.locator.char_start : table_block.locator.char_end
        ]
        == table_block.text
    )
    assert structure.reading_order == [
        heading.block_id,
        table_block.block_id,
    ]


def test_vision_only_table_clears_review_only_with_page_proofs():
    parsed = _blank_parsed()
    page = VisionPage(
        page=1,
        tables=[
            VisionTable(
                id="t1",
                header_rows=[0],
                rows=[["科目", "金额"], ["测试项", "18000"]],
            )
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
        source_sha256="a" * 64,
    )
    image_sha = "d" * 64
    result.page_evidence[1] = {"image_sha256": image_sha}
    result.record_page_proof(
        1,
        image_sha256=image_sha,
        cache_hit=False,
        evidence={"render_sha256": "e" * 64},
    )
    apply_vision_result(parsed, result)

    table = parsed.structure.tables[0]
    assert table.vision["trusted"] is True
    assert table.vision["needs_review"] is False
    assert all(
        cell.needs_review is False
        for row in table.rows
        for cell in row.cells
    )


def test_formula_block_is_materialized_as_searchable_body():
    parsed = _blank_parsed()
    page = VisionPage(
        page=1,
        blocks=[VisionBlock(id="b1", kind="formula", text="E=mc^2")],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    apply_vision_result(parsed, result)

    block = next(
        item for item in parsed.structure.blocks if item.text == "E=mc^2"
    )
    assert block.kind == "paragraph"
    assert "E=mc^2" in parsed.text


def test_vision_asset_description_requires_real_binding():
    parsed = _blank_parsed()
    parsed.structure.assets = [
        AssetResource(
            asset_id="asset-1",
            kind="image",
            src="images/logo.png",
            locator=SourceLocator(format="pdf", page=1),
        )
    ]
    page = VisionPage(
        page=1,
        images=[
            VisionImage(
                id="i1",
                kind="formula",
                description="E=mc^2",
                asset_id="asset-1",
            ),
            VisionImage(
                id="i2",
                kind="image",
                description="校徽",
                asset_id="missing-asset",
            ),
        ],
    )
    result = VisionResult(
        ok=True,
        status="success",
        pages={1: page},
        pages_processed=1,
        pages_expected=1,
        response_validated=True,
    )
    apply_vision_result(parsed, result)

    blocks = {block.element_id: block for block in parsed.structure.blocks}
    assert "asset-1" in blocks
    assert blocks["asset-1"].kind == "paragraph"
    assert parsed.structure.assets[0].recognized is True
    assert any(
        item["kind"] == "vision_asset_unbound"
        for item in result.unresolved_conflicts
    )
    assert parsed.structure.coverage["vision_assets"][0]["asset_id"] == "asset-1"


# ---------------------------------------------------------------------------
# HTML renderer integration, independent asset recognition and 429 handling
# ---------------------------------------------------------------------------


def test_edge_html_renderer_localizes_once_and_reuses_bundle(tmp_path, monkeypatch):
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"stub-edge")
    bundle_pdf = tmp_path / "rendered.pdf"
    bundle_pdf.write_bytes(b"%PDF-1.4\nfixture\n")
    manifest = tmp_path / "resources_manifest.json"
    asset = _html_asset("doc@ver#image1", "html:nth-child(1)")
    resource_record = {
        "asset_id": asset.asset_id,
        "dom_path": "html:nth-child(1)",
        "status": "embedded",
        "sha256": "d" * 64,
        "url": "https://www.scau.edu.cn/demo.png",
    }
    prepare_calls: list[dict] = []

    def fake_prepare(source_path, options, *, doc_id="", version_id=""):
        prepare_calls.append(
            {
                "source": Path(source_path),
                "options": options,
                "doc_id": doc_id,
                "version_id": version_id,
            }
        )
        return SimpleNamespace(
            pdf_path=bundle_pdf,
            resources=[SimpleNamespace(as_dict=lambda: dict(resource_record))],
            manifest_path=manifest,
            warnings=["资源 1 使用本地缓存"],
            assets=[asset],
            clean_html_sha256="e" * 64,
        )

    monkeypatch.setattr(html_visual, "prepare_html_render", fake_prepare)
    rendered_sources: list[Path] = []

    def fake_render(self, source_path, *, source_format, dpi):
        rendered_sources.append(Path(source_path))
        return FixtureRenderer().render(
            source_path, source_format=source_format, dpi=dpi
        )

    monkeypatch.setattr(visual.PdfPageRenderer, "render", fake_render)
    config = _config(
        tmp_path,
        html_render_cache_dir=tmp_path / "render-cache",
        html_render_timeout_seconds=45.0,
        html_resource_cache_dir=tmp_path / "resource-cache",
        html_resource_manifest=manifest,
        html_resource_max_bytes=123456,
        html_resource_timeout_seconds=7.5,
        html_resource_max_redirects=2,
        html_allowed_hosts=("scau.edu.cn",),
    )
    renderer = EdgeHtmlRenderer(
        str(edge), config=config, doc_id="doc", version_id="ver"
    )
    html = tmp_path / "source.html"
    html.write_text("<html><body>正文</body></html>", encoding="utf-8")

    pages = renderer.render(html, source_format="html", dpi=144)
    again = renderer.render(html, source_format="html", dpi=144)

    assert len(pages) == 1 and len(again) == 1
    assert rendered_sources == [bundle_pdf, bundle_pdf]
    assert len(prepare_calls) == 1
    call = prepare_calls[0]
    assert call["doc_id"] == "doc" and call["version_id"] == "ver"
    options = call["options"]
    assert options.cache_dir == tmp_path / "render-cache"
    assert options.resource_cache_dir == tmp_path / "resource-cache"
    assert options.resource_manifest == manifest
    assert options.max_bytes == 123456
    assert options.request_timeout == 7.5
    assert options.redirect_limit == 2
    assert options.render_timeout == 45.0
    assert options.allowed_hosts == ("scau.edu.cn",)
    metadata = renderer.last_render_metadata
    assert metadata["resources"][0]["asset_id"] == asset.asset_id
    assert metadata["manifest_path"] == str(manifest)
    assert metadata["warnings"] == ["资源 1 使用本地缓存"]
    assert metadata["assets"][0]["asset_id"] == asset.asset_id
    assert metadata["clean_html_sha256"] == "e" * 64


def test_html_asset_recognition_materializes_formula_and_non_text(tmp_path):
    resources = [
        _html_resource(tmp_path, "formula-1", "html:nth-child(2)"),
        _html_resource(tmp_path, "logo-1", "html:nth-child(3)"),
        _html_resource(tmp_path, "photo-1", "html:nth-child(4)"),
    ]
    parsed = _html_parsed(
        _html_asset("formula-1", "html:nth-child(2)"),
        _html_asset("logo-1", "html:nth-child(3)"),
        _html_asset("photo-1", "html:nth-child(4)"),
    )
    payload = _html_page_payload(
        [
            {
                "id": "i1",
                "kind": "image",
                "description": "学院标识",
                "asset_id": "logo-1",
                "dom_path": "html:nth-child(3)",
            }
        ]
    )

    def asset_payload(asset_id: str) -> dict:
        if asset_id == "formula-1":
            return {
                "has_text": True,
                "blocks": [{"kind": "formula", "text": "E=mc^2"}],
            }
        return {"has_text": False, "blocks": [], "warnings": ["人物照片"]}

    provider = FixtureProvider(payload, asset_payload=asset_payload)
    metadata = {
        "resources": resources,
        "manifest_path": str(tmp_path / "manifest.json"),
        "warnings": [],
        "clean_html_sha256": "f" * 64,
        "assets": [asset.model_dump(mode="json") for asset in parsed.structure.assets],
    }
    _, result = _prepare_html(
        tmp_path, provider, parsed=parsed, metadata=metadata
    )

    assert result.ok is True
    assert result.publishable is True
    # 即使整页视觉已经描述了 logo-1，也必须对该资源图独立识别并留下像素证明。
    assert [call["asset_id"] for call in provider.asset_calls] == [
        "formula-1",
        "logo-1",
        "photo-1",
    ]
    assert result.images_sent == 4
    assert [request["stage"] for request in result.requests] == [
        "full_page",
        "asset",
        "asset",
        "asset",
    ]
    statuses = {
        record["asset_id"]: record["status"]
        for record in result.asset_recognitions
    }
    assert statuses == {
        "formula-1": "recognized",
        "logo-1": "non_text",
        "photo-1": "non_text",
    }
    logo_record = next(
        record
        for record in result.asset_recognitions
        if record["asset_id"] == "logo-1"
    )
    assert logo_record["image_sha256"] == resources[1]["sha256"]
    assert logo_record["resource_sha256"] == resources[1]["sha256"]
    formula_record = next(
        record
        for record in result.asset_recognitions
        if record["asset_id"] == "formula-1"
    )
    assert formula_record["image_sha256"] == resources[0]["sha256"]
    assert formula_record["resource_sha256"] == resources[0]["sha256"]
    assert formula_record["response_sha256"]
    assert formula_record["prompt_version"] == VISION_PROMPT_VERSION
    assert provider.asset_calls[0]["image_sha256"] == resources[0]["sha256"]
    assert provider.asset_calls[0]["context"]["stage"] == "asset"
    assert provider.asset_calls[0]["context"]["source_url"].startswith(
        "https://www.scau.edu.cn/"
    )
    photo_record = next(
        record
        for record in result.asset_recognitions
        if record["asset_id"] == "photo-1"
    )
    assert photo_record["blocks"] == []

    apply_vision_result(parsed, result)
    formula_block = next(
        block for block in parsed.structure.blocks if block.element_id == "formula-1"
    )
    assert formula_block.kind == "paragraph"
    assert formula_block.text == "E=mc^2"
    assert "E=mc^2" in parsed.text
    assets = {asset.asset_id: asset for asset in parsed.structure.assets}
    assert assets["formula-1"].recognized is True
    assert assets["logo-1"].recognized is True
    assert assets["photo-1"].recognized is False
    assert "非文字" in (assets["photo-1"].note or "")
    coverage = parsed.structure.coverage["vision_assets"]
    assert any(
        item["source"] == "asset_recognition"
        and item["asset_id"] == "formula-1"
        and item["status"] == "recognized"
        for item in coverage
    )
    assert any(
        item["source"] == "page_vision"
        and item["asset_id"] == "logo-1"
        for item in coverage
    )


def test_html_asset_cache_avoids_second_provider_call(tmp_path):
    resources = [
        _html_resource(tmp_path, "formula-1", "html:nth-child(2)"),
        _html_resource(tmp_path, "photo-1", "html:nth-child(3)"),
    ]
    assets = [
        _html_asset("formula-1", "html:nth-child(2)"),
        _html_asset("photo-1", "html:nth-child(3)"),
    ]
    metadata = {
        "resources": resources,
        "manifest_path": str(tmp_path / "manifest.json"),
        "warnings": [],
        "clean_html_sha256": "f" * 64,
        "assets": [asset.model_dump(mode="json") for asset in assets],
    }

    def asset_payload(asset_id: str) -> dict:
        if asset_id == "formula-1":
            return {"has_text": True, "blocks": [{"kind": "formula", "text": "a+b"}]}
        return {"has_text": False, "blocks": []}

    first_provider = FixtureProvider(
        _html_page_payload(), asset_payload=asset_payload
    )
    _, first = _prepare_html(
        tmp_path,
        first_provider,
        parsed=_html_parsed(*assets),
        metadata=metadata,
    )
    assert first.ok is True and first.asset_cache_hits == 0
    assert len(first_provider.asset_calls) == 2

    second_provider = FixtureProvider(
        _html_page_payload(), asset_payload=asset_payload
    )
    _, second = _prepare_html(
        tmp_path,
        second_provider,
        parsed=_html_parsed(*assets),
        metadata=metadata,
    )
    assert second.ok is True
    assert second.cache_hits == 1
    assert second.images_sent == 0
    assert second.asset_cache_hits == 2
    assert second_provider.asset_calls == []
    statuses = {
        record["asset_id"]: record["status"]
        for record in second.asset_recognitions
    }
    assert statuses == {"formula-1": "recognized", "photo-1": "non_text"}
    assert all(record["cache_hit"] is True for record in second.asset_recognitions)


def test_html_asset_page_description_does_not_skip_independent_recognition(tmp_path):
    resources = [_html_resource(tmp_path, "logo-1", "html:nth-child(3)")]
    parsed = _html_parsed(_html_asset("logo-1", "html:nth-child(3)"))
    payload = _html_page_payload(
        [
            {
                "id": "i1",
                "kind": "image",
                "description": "学院标识",
                "asset_id": "logo-1",
                "dom_path": "html:nth-child(3)",
            }
        ]
    )
    provider = FixtureProvider(
        payload, asset_payload={"has_text": False, "blocks": []}
    )
    metadata = {
        "resources": resources,
        "manifest_path": str(tmp_path / "manifest.json"),
        "warnings": [],
        "clean_html_sha256": "f" * 64,
        "assets": [asset.model_dump(mode="json") for asset in parsed.structure.assets],
    }
    _, result = _prepare_html(
        tmp_path, provider, parsed=parsed, metadata=metadata
    )

    assert result.ok is True
    assert [call["asset_id"] for call in provider.asset_calls] == ["logo-1"]
    assert result.images_sent == 2
    record = result.asset_recognitions[0]
    assert record["asset_id"] == "logo-1"
    assert record["status"] == "non_text"
    assert record["image_sha256"] == resources[0]["sha256"]
    assert record["resource_sha256"] == resources[0]["sha256"]


def test_html_missing_asset_resource_blocks_publication(tmp_path):
    parsed = _html_parsed(_html_asset("missing-1", "html:nth-child(2)"))
    provider = FixtureProvider(_html_page_payload())
    metadata = {
        "resources": [],
        "manifest_path": str(tmp_path / "manifest.json"),
        "warnings": [],
        "clean_html_sha256": "f" * 64,
        "assets": [asset.model_dump(mode="json") for asset in parsed.structure.assets],
    }
    _, result = _prepare_html(
        tmp_path, provider, parsed=parsed, metadata=metadata
    )

    assert result.ok is True
    assert result.publishable is False
    assert provider.asset_calls == []
    assert any(
        item["kind"] == "vision_asset_resource_missing"
        for item in result.unresolved_conflicts
    )
    assert result.asset_recognitions[0]["status"] == "resource_missing"
    apply_vision_result(parsed, result)
    coverage = parsed.structure.coverage["vision_assets"]
    assert coverage[-1]["status"] == "resource_missing"
    assert parsed.structure.assets[0].recognized is False


def test_html_resource_sha_mismatch_is_not_sent(tmp_path):
    resource = _html_resource(tmp_path, "formula-1", "html:nth-child(2)")
    resource["sha256"] = "0" * 64
    parsed = _html_parsed(_html_asset("formula-1", "html:nth-child(2)"))
    provider = FixtureProvider(
        _html_page_payload(),
        asset_payload={"has_text": True, "blocks": [{"kind": "formula", "text": "x"}]},
    )
    metadata = {
        "resources": [resource],
        "manifest_path": str(tmp_path / "manifest.json"),
        "warnings": [],
        "clean_html_sha256": "f" * 64,
        "assets": [asset.model_dump(mode="json") for asset in parsed.structure.assets],
    }
    _, result = _prepare_html(
        tmp_path, provider, parsed=parsed, metadata=metadata
    )

    assert result.publishable is False
    assert provider.asset_calls == []
    assert any(
        item["kind"] == "vision_asset_resource_invalid"
        and item["code"] == "asset_resource_sha_mismatch"
        for item in result.unresolved_conflicts
    )


def test_html_asset_without_text_evidence_is_rejected(tmp_path):
    resource = _html_resource(tmp_path, "formula-1", "html:nth-child(2)")
    parsed = _html_parsed(_html_asset("formula-1", "html:nth-child(2)"))
    provider = FixtureProvider(
        _html_page_payload(),
        asset_payload={"blocks": []},
    )
    metadata = {
        "resources": [resource],
        "manifest_path": str(tmp_path / "manifest.json"),
        "warnings": [],
        "clean_html_sha256": "f" * 64,
        "assets": [asset.model_dump(mode="json") for asset in parsed.structure.assets],
    }
    _, result = _prepare_html(
        tmp_path, provider, parsed=parsed, metadata=metadata
    )

    assert result.publishable is False
    assert result.asset_recognitions[0]["status"] == "failed"
    conflict = next(
        item
        for item in result.unresolved_conflicts
        if item["kind"] == "vision_asset_recognition_failed"
    )
    assert conflict["code"] == "asset_text_evidence_missing"
    assert parsed.structure.assets[0].recognized is False


def test_provider_429_waits_retry_after_in_observable_segments(
    tmp_path, vision_env, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(visual.time, "sleep", sleeps.append)
    client = StubClient(
        [
            StubResponse(429, text="slow", headers={"Retry-After": "300"}),
            _completion(_page_payload()),
        ]
    )
    provider = GeminiVisionProvider(_provider_config(tmp_path), client=client)
    budget = VisionRequestBudget(limit=5)
    page = provider.recognize_page(
        b"png-bytes",
        page_no=1,
        context={
            "budget": budget,
            "stage": "full_page",
            "image_sha256": "b" * 64,
        },
    )

    assert page["page"] == 1
    assert sleeps == [30.0] * 10
    assert [request["attempt"] for request in budget.requests] == [1, 2]
    assert len(provider.rate_limit_events) == 1
    event = provider.rate_limit_events[0]
    assert event["http_status"] == 429
    assert event["retry_after_seconds"] == 300.0
    assert event["wait_seconds"] == 300.0
    assert "retry-after" not in json.dumps(event).lower()


def test_provider_429_enforces_minimum_wait_without_header(
    tmp_path, vision_env, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(visual.time, "sleep", sleeps.append)
    client = StubClient(
        [StubResponse(429, text="slow"), _completion(_page_payload())]
    )
    provider = GeminiVisionProvider(_provider_config(tmp_path), client=client)
    provider.recognize_page(
        b"png-bytes",
        page_no=1,
        context={"budget": VisionRequestBudget(limit=5), "image_sha256": "b" * 64},
    )

    assert sleeps == [30.0] * 6
    assert provider.rate_limit_events[0]["retry_after_seconds"] == 0.0
    assert provider.rate_limit_events[0]["wait_seconds"] == 180.0


def test_response_header_lookup_is_case_insensitive_for_plain_mappings():
    response = SimpleNamespace(headers={"Retry-After": "300"})
    assert visual._response_header(response, "retry-after") == "300"
    response = SimpleNamespace(headers={"rEtRy-AfTeR": "7"})
    assert visual._response_header(response, "Retry-After") == "7"
    assert visual._response_header(SimpleNamespace(headers={}), "Retry-After") is None
    assert visual._response_header(SimpleNamespace(), "Retry-After") is None


def test_retry_after_parses_http_date_and_rejects_non_finite_values():
    future = format_datetime(
        datetime.now(UTC) + timedelta(seconds=240), usegmt=True
    )
    seconds = visual._retry_after_seconds(future)
    assert 200 <= seconds <= 240
    for value in ("inf", "-inf", "nan", "Infinity"):
        assert visual._retry_after_seconds(value) == 0.0


def test_provider_persistent_429_raises_after_one_limited_retry(
    tmp_path, vision_env, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(visual.time, "sleep", sleeps.append)
    client = StubClient(
        [
            StubResponse(429, text="slow", headers={"Retry-After": "180"}),
            StubResponse(429, text="slow", headers={"Retry-After": "180"}),
        ]
    )
    provider = GeminiVisionProvider(_provider_config(tmp_path), client=client)
    budget = VisionRequestBudget(limit=5)
    with pytest.raises(VisionRateLimited) as excinfo:
        provider.recognize_page(
            b"png-bytes",
            page_no=1,
            context={
                "budget": budget,
                "stage": "full_page",
                "image_sha256": "b" * 64,
            },
        )

    assert excinfo.value.http_status == 429
    assert excinfo.value.wait_seconds == 180.0
    assert sleeps == [30.0] * 6
    assert len(client.calls) == 2
    assert budget.images_sent == 2
    assert len(provider.rate_limit_events) == 2


def test_rate_limited_page_aborts_without_region_retry(
    tmp_path, vision_env, monkeypatch
):
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: None)
    client = StubClient(
        [
            StubResponse(429, text="slow", headers={"Retry-After": "180"}),
            StubResponse(429, text="slow", headers={"Retry-After": "180"}),
        ]
    )
    config = _provider_config(tmp_path, max_requests=10)
    provider = GeminiVisionProvider(config, client=client)
    _, result = _prepare(tmp_path, provider, config=config)

    assert result.ok is False
    assert result.publishable is False
    assert result.pages_processed == 0
    assert result.images_sent == 2
    assert result.refinement_requests == 0
    assert len(client.calls) == 2
    assert len(result.rate_limits) == 2
    failure = next(
        item for item in result.failures if item["stage"] == "ratelimit"
    )
    assert failure["code"] == "rate_limited"
    assert failure["http_status"] == 429
    assert failure["wait_seconds"] == 180.0


def test_region_rate_limit_is_not_downgraded_to_refinement_failure(
    tmp_path, vision_env, monkeypatch
):
    monkeypatch.setattr(visual.time, "sleep", lambda _seconds: None)
    client = StubClient(
        [
            _completion(_vision_table_payload("1800", bbox=[10, 10, 90, 60])),
            StubResponse(429, text="slow", headers={"Retry-After": "180"}),
            StubResponse(429, text="slow", headers={"Retry-After": "180"}),
        ]
    )
    config = _provider_config(
        tmp_path, max_requests=10, native_grid_verification=False
    )
    provider = GeminiVisionProvider(config, client=client)
    _, result = _prepare(
        tmp_path, provider, config=config, parsed=_native_parsed("18000")
    )

    assert result.ok is False
    assert result.publishable is False
    assert result.pages_processed == 0
    assert result.images_sent == 3
    assert result.refinement_requests == 2
    assert result.refinements == []
    assert len(client.calls) == 3
    assert len(result.rate_limits) == 2
    failure = next(
        item for item in result.failures if item["stage"] == "ratelimit"
    )
    assert failure["code"] == "rate_limited"
    assert not any(
        item["kind"] == "refinement_failed"
        for item in result.unresolved_conflicts
    )
