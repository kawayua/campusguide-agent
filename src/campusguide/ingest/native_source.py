"""Independent original-PDF text audit for native table grid cells.

The audit re-reads the original PDF, recomputes its file SHA-256, and compares
each reliable native origin cell with the text clipped at the cell bbox. It is
an audit-only source signal: no model is called, inputs are not mutated, and
clip text is never adopted as a correction.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from campusguide.ingest.structure import TableCell, TableStructure

EXTRACTOR_METHOD = "pymupdf.get_text:text:clip:sort"

_WHITESPACE_RE = re.compile(r"\s+")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


@dataclass(frozen=True, slots=True)
class NativeSourceAudit:
    source_path: Path
    source_sha256: str
    page_no: int
    page_width: float
    page_height: float
    has_text_layer: bool
    tables: list[TableStructure]
    checked_cells: list[dict[str, Any]]
    mismatches: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    @property
    def page_size(self) -> tuple[float, float]:
        return (self.page_width, self.page_height)


def _normalized(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def _require_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("source_sha256 必须是 64 位十六进制字符串")
    return value.lower()


def _require_page_no(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_no 必须是正整数")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_bbox(
    value: Any, page_width: float, page_height: float
) -> tuple[list[float] | None, str]:
    if value is None:
        return None, "cell_bbox_missing"
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        return None, "cell_bbox_invalid"
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None, "cell_bbox_invalid"
        number = float(item)
        if not math.isfinite(number):
            return None, "cell_bbox_invalid"
        numbers.append(number)
    x0, y0, x1, y1 = numbers
    if not (0.0 <= x0 < x1 <= page_width and 0.0 <= y0 < y1 <= page_height):
        return None, "cell_bbox_invalid"
    return numbers, ""


def _cell_entry(
    table: TableStructure,
    cell: TableCell,
    *,
    page_no: int,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "table_id": table.table_id,
        "id": cell.cell_id,
        "row": cell.row,
        "col": cell.col,
        "page": page_no,
        "covered": bool(cell.covered),
        "native_text": str(cell.text),
        "clip_text": None,
        "bbox": None,
        "status": "",
        "source_sha256": source_sha256,
        "extractor": EXTRACTOR_METHOD,
    }


def _cell_warning(
    reason: str,
    table: TableStructure,
    cell: TableCell,
    *,
    page_no: int,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "reason": reason,
        "table_id": table.table_id,
        "id": cell.cell_id,
        "row": cell.row,
        "col": cell.col,
        "page": page_no,
        "source_sha256": source_sha256,
    }


def audit_pdf_grid_cells(
    source_path: Path,
    *,
    page_no: int,
    tables: Sequence[TableStructure],
    source_sha256: str,
) -> NativeSourceAudit:
    if not isinstance(source_path, Path):
        raise TypeError("source_path 必须是 Path")
    if not source_path.is_file():
        raise FileNotFoundError(f"PDF 源文件不存在：{source_path}")
    expected_sha = _require_sha256(source_sha256)
    page_number = _require_page_no(page_no)
    if isinstance(tables, (str, bytes)) or not isinstance(tables, Sequence):
        raise ValueError("tables 必须是 TableStructure 序列")
    for table in tables:
        if not isinstance(table, TableStructure):
            raise ValueError("tables 必须只包含 TableStructure")

    actual_sha = _file_sha256(source_path)
    if actual_sha != expected_sha:
        raise ValueError("PDF 源文件 SHA-256 与传入值不一致")

    selected = [
        table.model_copy(deep=True)
        for table in tables
        if table.kind != "layout" and table.page == page_number
    ]

    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF 未安装，无法读取原 PDF") from exc

    try:
        document = pymupdf.open(source_path)
    except Exception as exc:
        raise ValueError(f"无法打开 PDF 源文件：{source_path}") from exc

    try:
        if not document.is_pdf:
            raise ValueError("源文件不是 PDF")
        if page_number > document.page_count:
            raise ValueError(f"page_no 超出 PDF 页数：{page_number} > {document.page_count}")
        page = document[page_number - 1]
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        if not (
            math.isfinite(page_width)
            and math.isfinite(page_height)
            and page_width > 0
            and page_height > 0
        ):
            raise ValueError("PDF 页尺寸无效")

        has_text_layer = bool(str(page.get_text("text") or "").strip())
        checked_cells: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if not has_text_layer:
            warnings.append(
                {
                    "reason": "page_text_layer_missing",
                    "page": page_number,
                    "source_sha256": actual_sha,
                    "message": "源 PDF 该页没有文本层，单元格需要真实图片复核",
                }
            )

        for table in selected:
            for row in table.rows:
                for cell in row.cells:
                    entry = _cell_entry(
                        table, cell, page_no=page_number, source_sha256=actual_sha
                    )
                    checked_cells.append(entry)
                    if cell.covered:
                        entry["status"] = "skipped_covered"
                        continue
                    if cell.origin_row != cell.row or cell.origin_col != cell.col:
                        entry["status"] = "unresolved_origin_mismatch"
                        cell.needs_review = True
                        warnings.append(
                            _cell_warning(
                                "cell_origin_mismatch",
                                table,
                                cell,
                                page_no=page_number,
                                source_sha256=actual_sha,
                            )
                        )
                        continue
                    native = _normalized(str(cell.text))
                    bbox, bbox_reason = _valid_bbox(cell.bbox, page_width, page_height)
                    if bbox is None:
                        if native:
                            entry["status"] = "unresolved_bbox"
                            cell.needs_review = True
                            warnings.append(
                                _cell_warning(
                                    bbox_reason,
                                    table,
                                    cell,
                                    page_no=page_number,
                                    source_sha256=actual_sha,
                                )
                            )
                        else:
                            entry["status"] = "skipped_no_bbox"
                        continue
                    entry["bbox"] = bbox
                    if not has_text_layer:
                        entry["status"] = "unresolved_no_text_layer"
                        cell.needs_review = True
                        continue
                    try:
                        raw_clip = page.get_text(
                            "text", clip=pymupdf.Rect(bbox), sort=True
                        )
                    except Exception:
                        entry["status"] = "unresolved_extract_failed"
                        cell.needs_review = True
                        warnings.append(
                            _cell_warning(
                                "cell_clip_extract_failed",
                                table,
                                cell,
                                page_no=page_number,
                                source_sha256=actual_sha,
                            )
                        )
                        continue
                    clip_text = str(raw_clip or "")
                    entry["clip_text"] = clip_text
                    if _normalized(clip_text) == native:
                        entry["status"] = (
                            "source_text_match" if native else "source_text_empty"
                        )
                        continue
                    entry["status"] = "source_clip_mismatch"
                    cell.needs_review = True
                    mismatches.append(
                        {
                            "reason": "source_clip_mismatch",
                            "id": cell.cell_id,
                            "table_id": table.table_id,
                            "row": cell.row,
                            "col": cell.col,
                            "native_text": str(cell.text),
                            "clip_text": clip_text,
                            "bbox": bbox,
                            "source_sha256": actual_sha,
                            "page": page_number,
                            "extractor": EXTRACTOR_METHOD,
                        }
                    )

        return NativeSourceAudit(
            source_path=source_path,
            source_sha256=actual_sha,
            page_no=page_number,
            page_width=page_width,
            page_height=page_height,
            has_text_layer=has_text_layer,
            tables=selected,
            checked_cells=checked_cells,
            mismatches=mismatches,
            warnings=warnings,
        )
    finally:
        document.close()


__all__ = ["EXTRACTOR_METHOD", "NativeSourceAudit", "audit_pdf_grid_cells"]
