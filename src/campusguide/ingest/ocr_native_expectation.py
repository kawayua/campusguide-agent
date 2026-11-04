"""Source-native table expectations for region cross-checks.

Builds a ``RegionExpectation`` exclusively from parsed PyMuPDF tables and an
independent re-read of the original PDF. There is no OCR/gold input, no model,
no network and no write. The audit payload is diagnostic evidence, not truth;
native agreement can only trigger conflict review, never certify accuracy.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from campusguide.ingest.native_source import NativeSourceAudit, audit_pdf_grid_cells
from campusguide.ingest.ocr_layout import RegionCrop
from campusguide.ingest.ocr_router import NumericExpectation, RegionExpectation
from campusguide.ingest.parse import PARSE_SCHEMA_VERSION, ParsedDocument
from campusguide.ingest.structure import TableCell, TableRow, TableStructure

MIN_TABLE_IOU = 0.85
ASPECT_TOLERANCE = 0.01
_ALLOWED_TEXT_SOURCES = frozenset({None, "native", "clip_text"})
_CHECKABLE_MARKERS = frozenset({"M", "H", "L"})
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_CHECKED_STATUSES = frozenset({"source_text_match", "source_text_empty"})
_BBox = tuple[float, float, float, float]


class NativeExpectationValidationError(ValueError):
    """输入身份、schema 或页面绑定不一致；在产生任何期望前失败关闭。"""


class NativeExpectationAudit(BaseModel):
    """源原生期望审计；``audit`` 仅为诊断信息，不是事实或准确性证明。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unavailable", "needs_review"]
    expectation: RegionExpectation | None = None
    needs_review: bool
    reasons: list[str] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reasons", mode="before")
    @classmethod
    def _reasons(cls, value: object) -> list[str]:
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise ValueError("reasons 必须是字符串列表")
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def _consistent(self) -> NativeExpectationAudit:
        if self.needs_review != (self.status == "needs_review"):
            raise ValueError("needs_review 与 status 不一致")
        if (self.status == "ready") != (self.expectation is not None):
            raise ValueError("只有 ready 可以携带 expectation")
        if (
            self.audit.get("diagnostic_only") is not True
            or self.audit.get("not_truth") is not True
        ):
            raise ValueError("audit 必须显式标记 diagnostic_only/not_truth")
        return self


def _strict_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _strict_bool(value: object) -> bool:
    return isinstance(value, bool)


def _finite_bbox(value: object) -> _BBox | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    box = tuple(numbers)
    if box[0] < 0.0 or box[1] < 0.0 or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box  # type: ignore[return-value]


def _sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise NativeExpectationValidationError(f"{name} 必须是 64 位十六进制 SHA-256")
    return value.strip().lower()


def _checked_inputs(
    crop: object, source_path: object, parsed: object
) -> tuple[RegionCrop, ParsedDocument]:
    if not isinstance(crop, RegionCrop):
        raise NativeExpectationValidationError("crop 必须是 RegionCrop")
    if not isinstance(source_path, Path):
        raise NativeExpectationValidationError("source_path 必须是 Path")
    if not isinstance(parsed, ParsedDocument):
        raise NativeExpectationValidationError("parsed 必须是 ParsedDocument")
    try:
        checked = RegionCrop.model_validate(crop.model_dump(mode="python"))
    except ValidationError as exc:
        raise NativeExpectationValidationError(f"crop 严格复核失败: {exc}") from exc
    if parsed.schema_version != PARSE_SCHEMA_VERSION:
        raise NativeExpectationValidationError("parsed.schema_version 不受支持")
    if parsed.format not in {"pdf", "html"}:
        raise NativeExpectationValidationError("parsed.format 不受支持")
    if parsed.format != checked.page.source_format:
        raise NativeExpectationValidationError("parsed 与页面来源格式不一致")
    if (
        parsed.doc_id != checked.page.doc_id
        or parsed.version_id != checked.page.version_id
    ):
        raise NativeExpectationValidationError("parsed 与页面文档/版本绑定不一致")
    if _sha256(parsed.source_sha256, name="parsed.source_sha256") != (
        checked.page.source_sha256
    ):
        raise NativeExpectationValidationError("parsed 与页面来源 SHA-256 不一致")
    if not _strict_bool(parsed.ocr_used):
        raise NativeExpectationValidationError("parsed.ocr_used 必须是布尔值")
    if parsed.page_count is not None:
        if not _strict_int(parsed.page_count, minimum=1):
            raise NativeExpectationValidationError("parsed.page_count 必须是正整数")
        if checked.page.page > parsed.page_count:
            raise NativeExpectationValidationError("页面超出 parsed.page_count")
    if parsed.structure is not None and parsed.structure.format != parsed.format:
        raise NativeExpectationValidationError("parsed.structure 来源格式不一致")
    return checked, parsed


def _native_tables(parsed: ParsedDocument, page: int) -> list[TableStructure]:
    if parsed.structure is None:
        return []
    result: list[TableStructure] = []
    for table in parsed.structure.tables:
        if (
            not _strict_int(table.page, minimum=1)
            or table.page != page
            or table.kind != "data"
            or table.source != "pymupdf"
            or table.vision is not None
        ):
            continue
        if any(
            cell.text_source not in _ALLOWED_TEXT_SOURCES
            for row in table.rows
            for cell in row.cells
        ):
            continue
        result.append(table)
    return result


def _grid_ok(table: TableStructure) -> bool:
    rows, columns = table.row_count, table.column_count
    if not _strict_int(rows, minimum=1) or not _strict_int(columns, minimum=1):
        return False
    if len(table.rows) != rows:
        return False
    headers = table.header_row_indices
    if not isinstance(headers, list) or any(
        not _strict_int(index, minimum=0) or index >= rows for index in headers
    ):
        return False
    slots: list[list[TableCell | None]] = [[None] * columns for _ in range(rows)]
    origins: dict[tuple[int, int], TableCell] = {}
    seen_ids: set[str] = set()
    for index, row in enumerate(table.rows):
        if not _strict_int(row.row_index, minimum=0) or row.row_index != index:
            return False
        if not _strict_bool(row.is_header):
            return False
        for cell in row.cells:
            if (
                not isinstance(cell.cell_id, str)
                or not cell.cell_id.strip()
                or cell.cell_id in seen_ids
            ):
                return False
            seen_ids.add(cell.cell_id)
            if (
                not _strict_int(cell.row, minimum=0)
                or not _strict_int(cell.col, minimum=0)
                or not _strict_int(cell.rowspan, minimum=1)
                or not _strict_int(cell.colspan, minimum=1)
                or not _strict_int(cell.origin_row, minimum=0)
                or not _strict_int(cell.origin_col, minimum=0)
                or not _strict_bool(cell.covered)
                or cell.row != index
                or cell.col >= columns
                or slots[index][cell.col] is not None
            ):
                return False
            slots[index][cell.col] = cell
            if cell.covered:
                continue
            if cell.origin_row != cell.row or cell.origin_col != cell.col:
                return False
            if cell.row + cell.rowspan > rows or cell.col + cell.colspan > columns:
                return False
            origin_key = (cell.row, cell.col)
            if origin_key in origins:
                return False
            origins[origin_key] = cell
    if not origins or any(cell is None for line in slots for cell in line):
        return False
    cover = [[0] * columns for _ in range(rows)]
    for cell in origins.values():
        for row_index in range(cell.row, cell.row + cell.rowspan):
            for col_index in range(cell.col, cell.col + cell.colspan):
                cover[row_index][col_index] += 1
    if any(count != 1 for line in cover for count in line):
        return False
    for row in table.rows:
        for cell in row.cells:
            if not cell.covered:
                continue
            origin = origins.get((cell.origin_row, cell.origin_col))
            if origin is None or not (
                origin.row <= cell.row < origin.row + origin.rowspan
                and origin.col <= cell.col < origin.col + origin.colspan
            ):
                return False
    return True


def _map_bbox(
    box: _BBox,
    *,
    page_width: float,
    page_height: float,
    image_width: int,
    image_height: int,
) -> _BBox:
    scale_x = image_width / page_width
    scale_y = image_height / page_height
    return (
        box[0] * scale_x,
        box[1] * scale_y,
        box[2] * scale_x,
        box[3] * scale_y,
    )


def _iou(left: _BBox, right: Sequence[float]) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = (
        (left[2] - left[0]) * (left[3] - left[1])
        + (right[2] - right[0]) * (right[3] - right[1])
        - intersection
    )
    return intersection / union if union > 0.0 else 0.0


def _pdf_shape_reasons(
    source_path: Path,
    page_no: int,
    *,
    page_width: float,
    page_height: float,
    image_width: int,
    image_height: int,
) -> list[str]:
    try:
        import pymupdf
    except ImportError as exc:
        raise NativeExpectationValidationError("PyMuPDF 未安装") from exc
    try:
        document = pymupdf.open(source_path)
    except Exception as exc:
        raise NativeExpectationValidationError(f"无法打开原始 PDF: {exc}") from exc
    try:
        if not document.is_pdf or page_no > document.page_count:
            raise NativeExpectationValidationError("原始 PDF 页面无效")
        page = document[page_no - 1]
        if page.rotation != 0:
            return ["unsupported_pdf_rotation"]
        cropbox = page.cropbox
        if abs(float(cropbox.x0)) > 0.5 or abs(float(cropbox.y0)) > 0.5:
            return ["unsupported_pdf_cropbox_origin"]
        rect = page.rect
        width, height = float(rect.width), float(rect.height)
        if (
            not math.isfinite(width)
            or not math.isfinite(height)
            or width <= 0.0
            or height <= 0.0
            or abs(width - page_width) > 0.01
            or abs(height - page_height) > 0.01
        ):
            raise NativeExpectationValidationError("PDF 页尺寸与审计结果不一致")
        if (
            abs((image_width / image_height) - (width / height))
            >= (width / height) * ASPECT_TOLERANCE
        ):
            return ["parent_page_aspect_mismatch"]
        return []
    finally:
        document.close()


def _audit_base(
    crop: RegionCrop,
    page: int,
    source_audit: NativeSourceAudit | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "diagnostic_only": True,
        "not_truth": True,
        "native_agreement_is_not_accuracy_guarantee": True,
        "units": {
            "table_bbox": "pdf_points",
            "region_bbox": "parent_pixels",
            "cell_bbox": "pdf_points",
        },
        "source_sha256": crop.page.source_sha256,
        "parent_image_sha256": crop.page.image_sha256,
        "crop_sha256": crop.image_sha256,
        "region_id": crop.region.region_id,
        "page": page,
    }
    if source_audit is not None:
        payload.update(
            {
                "source_sha256": source_audit.source_sha256,
                "native_page_width_pt": source_audit.page_width,
                "native_page_height_pt": source_audit.page_height,
                "has_text_layer": source_audit.has_text_layer,
            }
        )
    payload.update(extra)
    return payload


def _result(
    status: Literal["ready", "unavailable", "needs_review"],
    *,
    reasons: list[str],
    audit: dict[str, Any],
    expectation: RegionExpectation | None = None,
) -> NativeExpectationAudit:
    payload = dict(audit)
    payload["reason_codes"] = list(reasons)
    return NativeExpectationAudit(
        status=status,
        expectation=expectation,
        needs_review=status == "needs_review",
        reasons=reasons,
        audit=payload,
    )


def _checked_entries(
    source_audit: NativeSourceAudit,
) -> dict[tuple[object, object], dict[str, Any]]:
    return {
        (entry.get("table_id"), entry.get("id")): entry
        for entry in source_audit.checked_cells
        if isinstance(entry, dict)
    }


def _reliable(entry: dict[str, Any] | None, cell: TableCell) -> bool:
    if entry is None or entry.get("covered") is not False:
        return False
    if entry.get("status") not in _CHECKED_STATUSES:
        return False
    entry_id = entry.get("id")
    entry_row = entry.get("row")
    entry_col = entry.get("col")
    entry_text = entry.get("native_text")
    if (
        not isinstance(entry_id, str)
        or entry_id != cell.cell_id
        or not _strict_int(entry_row, minimum=0)
        or entry_row != cell.row
        or not _strict_int(entry_col, minimum=0)
        or entry_col != cell.col
        or not isinstance(entry_text, str)
        or entry_text != cell.text
    ):
        return False
    cell_box, entry_box = cell.bbox, entry.get("bbox")
    if (
        not isinstance(cell_box, (list, tuple))
        or len(cell_box) != 4
        or not isinstance(entry_box, (list, tuple))
        or len(entry_box) != 4
    ):
        return False
    return all(
        not isinstance(target, bool)
        and not isinstance(audited, bool)
        and isinstance(target, (int, float))
        and isinstance(audited, (int, float))
        and math.isfinite(float(target))
        and math.isfinite(float(audited))
        and float(target) == float(audited)
        for target, audited in zip(cell_box, entry_box, strict=True)
    )


def _checkable(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped in _CHECKABLE_MARKERS:
        return True
    try:
        return Decimal(stripped).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _scalar_cells(rows: list[TableRow]) -> dict[int, dict[int, TableCell]]:
    return {
        row.row_index: {
            cell.col: cell
            for cell in row.cells
            if not cell.covered
            and cell.rowspan == 1
            and cell.colspan == 1
            and cell.origin_row == cell.row
            and cell.origin_col == cell.col
        }
        for row in rows
    }


def build_native_expectation(
    crop: RegionCrop,
    *,
    source_path: Path,
    parsed: ParsedDocument,
) -> NativeExpectationAudit:
    """从原生 PDF 表格派生源绑定数值/标记/空位期望；不读 OCR 或 gold。"""

    checked_crop, checked_parsed = _checked_inputs(crop, source_path, parsed)
    page = checked_crop.page.page
    if checked_crop.page.source_format != "pdf" or checked_parsed.format == "html":
        return _result(
            "unavailable",
            reasons=["unsupported_source_format"],
            audit=_audit_base(checked_crop, page),
        )
    checked_crop.page.verify_current()
    if checked_crop.region.kind != "table":
        return _result(
            "unavailable",
            reasons=["unsupported_region_kind"],
            audit=_audit_base(checked_crop, page),
        )
    candidates = _native_tables(checked_parsed, page)
    try:
        source_audit = audit_pdf_grid_cells(
            source_path,
            page_no=page,
            tables=candidates,
            source_sha256=checked_crop.page.source_sha256,
        )
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise NativeExpectationValidationError(f"原始 PDF 审计失败: {exc}") from exc
    if not source_audit.has_text_layer:
        return _result(
            "unavailable",
            reasons=["page_text_layer_missing"],
            audit=_audit_base(checked_crop, page, source_audit),
        )
    page_width, page_height = source_audit.page_width, source_audit.page_height
    if not (
        math.isfinite(page_width)
        and math.isfinite(page_height)
        and page_width > 0.0
        and page_height > 0.0
    ):
        raise NativeExpectationValidationError("原始 PDF 页尺寸无效")
    shape_reasons = _pdf_shape_reasons(
        source_path,
        page,
        page_width=page_width,
        page_height=page_height,
        image_width=checked_crop.page.width,
        image_height=checked_crop.page.height,
    )
    if shape_reasons:
        return _result(
            "unavailable",
            reasons=shape_reasons,
            audit=_audit_base(
                checked_crop, page, source_audit, native_table_count=len(candidates)
            ),
        )
    malformed: list[str] = []
    matches: list[tuple[TableStructure, _BBox, _BBox, float]] = []
    for table in candidates:
        box = _finite_bbox(table.bbox)
        if (
            not _grid_ok(table)
            or box is None
            or box[2] > page_width + 1e-6
            or box[3] > page_height + 1e-6
        ):
            malformed.append(table.table_id)
            continue
        mapped = _map_bbox(
            box,
            page_width=page_width,
            page_height=page_height,
            image_width=checked_crop.page.width,
            image_height=checked_crop.page.height,
        )
        iou = _iou(mapped, checked_crop.region.bbox_px)
        if iou >= MIN_TABLE_IOU:
            matches.append((table, box, mapped, iou))
    table_matches = [
        {"table_id": table.table_id, "iou": round(iou, 6)}
        for table, _box, _mapped, iou in matches
    ]
    base_extra = {
        "native_table_count": len(candidates),
        "malformed_table_ids": malformed,
        "table_matches": table_matches,
    }
    if not candidates:
        return _result(
            "unavailable",
            reasons=["no_native_data_tables"],
            audit=_audit_base(
                checked_crop, page, source_audit, **base_extra
            ),
        )
    if not matches:
        reason = (
            "native_table_geometry_malformed" if malformed else "no_native_table_match"
        )
        return _result(
            "unavailable",
            reasons=[reason],
            audit=_audit_base(checked_crop, page, source_audit, **base_extra),
        )
    if len(matches) > 1:
        return _result(
            "needs_review",
            reasons=["ambiguous_native_table_match"],
            audit=_audit_base(checked_crop, page, source_audit, **base_extra),
        )
    table, table_box, mapped, iou = matches[0]
    header_indices = {row.row_index for row in table.rows if row.is_header} | set(
        table.header_row_indices
    )
    body_rows = [
        row for row in table.rows if row.row_index not in header_indices
    ]
    row_cells = _scalar_cells(table.rows)
    checked = _checked_entries(source_audit)
    key_col: int | None = None
    key_count = 0
    for col in range(table.column_count):
        keys: list[str] = []
        for row in body_rows:
            cell = row_cells[row.row_index].get(col)
            if cell is None or not _reliable(
                checked.get((table.table_id, cell.cell_id)), cell
            ):
                continue
            key = cell.text.strip()
            if key:
                keys.append(key)
        if keys and len(set(keys)) == len(keys) and len(keys) > key_count:
            key_col, key_count = col, len(keys)
    if key_col is None:
        return _result(
            "unavailable",
            reasons=["no_reliable_key_column"],
            audit=_audit_base(
                checked_crop,
                page,
                source_audit,
                native_table_count=len(candidates),
                malformed_table_ids=malformed,
                table_matches=table_matches,
                table_id=table.table_id,
                iou=round(iou, 6),
            ),
        )
    checks: list[NumericExpectation] = []
    checked_coords: list[dict[str, Any]] = []
    skipped_reasons: dict[str, int] = {}
    skipped_count = 0
    skipped_rows = 0
    for row in body_rows:
        key_cell = row_cells[row.row_index].get(key_col)
        key_entry = checked.get((table.table_id, key_cell.cell_id)) if key_cell else None
        if (
            key_cell is None
            or not _reliable(key_entry, key_cell)
            or not key_cell.text.strip()
        ):
            skipped_rows += 1
            continue
        row_key = key_cell.text.strip()
        for col, cell in row_cells[row.row_index].items():
            if col == key_col:
                continue
            entry = checked.get((table.table_id, cell.cell_id))
            if not _reliable(entry, cell):
                skipped_count += 1
                reason = (
                    entry.get("status") if entry is not None else "missing_audit"
                )
                skipped_reasons[str(reason)] = skipped_reasons.get(str(reason), 0) + 1
                continue
            if not _checkable(cell.text):
                skipped_count += 1
                skipped_reasons["non_numeric_text"] = (
                    skipped_reasons.get("non_numeric_text", 0) + 1
                )
                continue
            checks.append(
                NumericExpectation(
                    row_key=row_key,
                    key_col=key_col,
                    col=col,
                    value=cell.text,
                )
            )
            checked_coords.append(
                {
                    "row": row.row_index,
                    "col": col,
                    "status": entry["status"],
                    "pdf_bbox": list(entry["bbox"]),
                }
            )
    counts = {
        "table_id": table.table_id,
        "iou": round(iou, 6),
        "key_col": key_col,
        "key_rows": key_count,
        "checked_count": len(checks),
        "skipped_count": skipped_count,
        "skipped_rows": skipped_rows,
        "skipped_reasons": skipped_reasons,
        "checked_cells": checked_coords,
    }
    if not checks:
        return _result(
            "unavailable",
            reasons=["no_numeric_or_marker_checks"],
            audit=_audit_base(
                checked_crop,
                page,
                source_audit,
                native_table_count=len(candidates),
                malformed_table_ids=malformed,
                table_matches=table_matches,
                **counts,
            ),
        )
    expectation = RegionExpectation(
        source_sha256=checked_crop.page.source_sha256,
        page_image_sha256=checked_crop.page.image_sha256,
        page=page,
        region_id=checked_crop.region.region_id,
        crop_sha256=checked_crop.image_sha256,
        expected_table_column_count=table.column_count,
        min_table_rows=table.row_count,
        numeric_checks=checks,
    )
    expectation_sha256 = hashlib.sha256(
        expectation.model_dump_json().encode("utf-8")
    ).hexdigest()
    audit = _audit_base(
        checked_crop,
        page,
        source_audit,
        native_table_count=len(candidates),
        malformed_table_ids=malformed,
        table_matches=table_matches,
        table_bbox_pdf_points=list(table_box),
        table_bbox_parent_pixels=list(mapped),
        expectation_sha256=expectation_sha256,
        **counts,
    )
    return _result("ready", reasons=[], audit=audit, expectation=expectation)


__all__ = [
    "NativeExpectationAudit",
    "NativeExpectationValidationError",
    "build_native_expectation",
]
