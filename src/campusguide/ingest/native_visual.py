"""PDF 原生网格视觉核验的运行时辅助。

只负责把 native 单元格 bbox 变成真实像素裁剪、构造独立观察、生成确定性
决策审计并校验可复用缓存。模型调用与结果应用由 ``visual.py`` 负责。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from campusguide.ingest.visual_verify import (
    NATIVE_CELL_CROP_PROMPT_SHA256,
    NATIVE_VERIFICATION_PROMPT,
    NativeCellObservation,
    NativeGridCheck,
    NativeGridDecision,
    PendingCell,
    expected_native_crop_bbox,
    parse_native_grid_check,
    resolve_native_grid_check,
)

NATIVE_GRID_MODE = "native_grid_verify"
NATIVE_GRID_AUDIT_MODE = "native_visual_verified"
NATIVE_GRID_CACHE_SCHEMA_VERSION = "native-grid-cache-2.0"
NATIVE_GRID_INSET_POINTS = 1.0
NATIVE_MODEL_CONTEXT_VERSION = "native-model-context-1.0"
NATIVE_SOURCE_AUDIT_VERSION = "native-source-audit-1.0"
NATIVE_HTML_GEOMETRY_VERSION = "html-grid-geometry-1.0"
NATIVE_VERIFY_USER_PREFIX = (
    "请核验下面这页实际渲染图像。输入是压缩后的 native 网格候选"
    f"（{NATIVE_MODEL_CONTEXT_VERSION}）："
    "tables[].rows[].cells 为 [col,text] 对，covered 位置为空占位，"
    "由 spans 的 row,col,rowspan,colspan 标明；missing_cols/error 表示"
    "原行不完整，不得当作空文本补猜。上下文只是可能出错的候选，"
    "空单元格不得猜测；needs_review 坐标必须优先逐格看原图，"
    "不得因为候选已有文本就判 verified；checked_rows 仍须显式覆盖"
    "所有真实 native 行。只输出协议 JSON："
)
_NATIVE_ADDITIONAL_KINDS = {"paragraph", "heading", "list_item", "formula"}
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_PENDING_PRIORITY = {"correction": 0, "uncertain": 1, "needs_review": 2}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", str(value or ""))
    cleaned = cleaned.strip("._")[:120]
    return cleaned or fallback


def _require_sha(value: Any, name: str) -> str:
    text = str(value or "").lower()
    if not _HEX64_RE.fullmatch(text):
        raise ValueError(f"{name} 必须是 64 位十六进制 SHA-256")
    return text


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    return value


def _require_bbox_list(value: Any, name: str, *, integer: bool) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise ValueError(f"{name} 必须是四元组")
    numbers: list[Any] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{name} 不接受 bool")
        if integer:
            if not isinstance(item, int):
                raise ValueError(f"{name} 必须是整数像素坐标")
        elif not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError(f"{name} 必须是有限数字")
        numbers.append(item)
    return tuple(numbers)


def native_verification_prompt_sha256() -> str:
    return _sha256_text(
        NATIVE_MODEL_CONTEXT_VERSION
        + "\n"
        + NATIVE_VERIFICATION_PROMPT
        + "\n"
        + NATIVE_VERIFY_USER_PREFIX
    )


def _verify_context_hash(context: Mapping[str, Any]) -> None:
    provided = context.get("native_context_sha256")
    if provided is None:
        return
    body = {key: value for key, value in context.items() if key != "native_context_sha256"}
    if str(provided) != _sha256_text(_canonical_json(body)):
        raise ValueError("native_context_sha256 重算不匹配，拒绝压缩被修改的上下文")


def _compact_header_paths(value: Any, table_id: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise ValueError(f"{table_id}.header_paths 必须是列表")
    paths: list[list[str]] = []
    for path in value:
        if isinstance(path, (str, bytes)) or not isinstance(path, Sequence):
            raise ValueError(f"{table_id}.header_paths 项必须是列表")
        paths.append([str(part) for part in path])
    return paths


def _compact_table(table: Mapping[str, Any]) -> dict[str, Any]:
    table_id = table.get("table_id")
    if not isinstance(table_id, str) or not table_id:
        raise ValueError("native context table_id 缺失")
    column_count = _require_int(table.get("column_count"), f"{table_id}.column_count")
    if column_count < 0:
        raise ValueError(f"{table_id}.column_count 不能为负")
    raw_header_rows = table.get("header_row_indices", [])
    if not isinstance(raw_header_rows, list):
        raise ValueError(f"{table_id}.header_row_indices 必须是列表")
    header_rows = sorted(
        {_require_int(item, f"{table_id}.header_row") for item in raw_header_rows}
    )
    header_paths = _compact_header_paths(table.get("header_paths", []), table_id)
    raw_rows = table.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError(f"{table_id}.rows 必须是列表")

    cells_by_row: dict[int, list[list[Any]]] = {}
    spans: list[list[int]] = []
    covered_origins: dict[tuple[int, int], tuple[int, int]] = {}
    derived_needs_review: set[tuple[int, int]] = set()
    seen_positions: set[tuple[int, int]] = set()
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"{table_id}.rows 项必须是对象")
        row_index = _require_int(raw_row.get("row"), f"{table_id}.row")
        if row_index in cells_by_row:
            raise ValueError(f"{table_id} 行号重复：{row_index}")
        raw_cells = raw_row.get("cells")
        if not isinstance(raw_cells, list):
            raise ValueError(f"{table_id}.cells 必须是列表")
        row_cells: list[list[Any]] = []
        for raw_cell in raw_cells:
            if not isinstance(raw_cell, Mapping):
                raise ValueError(f"{table_id}.cells 项必须是对象")
            cell_row = _require_int(raw_cell.get("row"), f"{table_id}.cell.row")
            col = _require_int(raw_cell.get("col"), f"{table_id}.cell.col")
            if cell_row != row_index:
                raise ValueError(f"{table_id} 单元格行号与行不一致")
            if not 0 <= col < column_count:
                raise ValueError(f"{table_id} 单元格列 {col} 超出 column_count")
            if (row_index, col) in seen_positions:
                raise ValueError(f"{table_id} 单元格坐标重复：{row_index},{col}")
            seen_positions.add((row_index, col))
            covered = raw_cell.get("covered", False)
            if not isinstance(covered, bool):
                raise ValueError(f"{table_id}.covered 必须是 bool")
            text = raw_cell.get("text", "")
            if not isinstance(text, str):
                raise ValueError(f"{table_id}.cell.text 必须是字符串")
            rowspan = _require_int(raw_cell.get("rowspan", 1), f"{table_id}.rowspan")
            colspan = _require_int(raw_cell.get("colspan", 1), f"{table_id}.colspan")
            if rowspan < 1 or colspan < 1:
                raise ValueError(f"{table_id} span 必须 >= 1")
            origin_row = _require_int(
                raw_cell.get("origin_row", row_index), f"{table_id}.origin_row"
            )
            origin_col = _require_int(
                raw_cell.get("origin_col", col), f"{table_id}.origin_col"
            )
            if not covered and (origin_row, origin_col) != (row_index, col):
                raise ValueError(f"{table_id} 非 covered 格的 origin 必须指向自身")
            needs_review = raw_cell.get("needs_review", False)
            if not isinstance(needs_review, bool):
                raise ValueError(f"{table_id}.needs_review 必须是 bool")
            if covered:
                text = ""
                covered_origins[(row_index, col)] = (origin_row, origin_col)
            elif rowspan > 1 or colspan > 1:
                spans.append([row_index, col, rowspan, colspan])
            if needs_review and not covered:
                derived_needs_review.add((row_index, col))
            row_cells.append([col, text])
        row_cells.sort(key=lambda item: item[0])
        cells_by_row[row_index] = row_cells

    spans.sort(key=lambda item: (item[0], item[1]))
    span_origins = {(item[0], item[1]): item for item in spans}
    if len(span_origins) != len(spans):
        raise ValueError(f"{table_id} span 原点重复")
    for (row_index, col), (origin_row, origin_col) in covered_origins.items():
        span = span_origins.get((origin_row, origin_col))
        if span is None or not (
            origin_row <= row_index < origin_row + span[2]
            and origin_col <= col < origin_col + span[3]
        ):
            raise ValueError(f"{table_id} covered 格 {row_index},{col} 缺少 span 来源")

    raw_row_indices = table.get("row_indices")
    if raw_row_indices is None:
        row_indices = set(cells_by_row)
    else:
        if not isinstance(raw_row_indices, list):
            raise ValueError(f"{table_id}.row_indices 必须是列表")
        row_indices = {
            _require_int(item, f"{table_id}.row_indices") for item in raw_row_indices
        }
        if len(row_indices) != len(raw_row_indices):
            raise ValueError(f"{table_id}.row_indices 重复")
        if row_indices != set(cells_by_row):
            raise ValueError(f"{table_id}.row_indices 与 rows 不一致")
    if not set(header_rows).issubset(row_indices):
        raise ValueError(f"{table_id} header_rows 不在原生行内")

    occupied = {
        row_index: {cell[0] for cell in cells}
        for row_index, cells in cells_by_row.items()
    }
    for origin_row, origin_col, rowspan, colspan in spans:
        for row_index in row_indices:
            if not origin_row <= row_index < origin_row + rowspan:
                continue
            for col in range(origin_col, min(origin_col + colspan, column_count)):
                occupied[row_index].add(col)
    compact_rows: list[dict[str, Any]] = []
    for row_index in sorted(row_indices):
        row_entry: dict[str, Any] = {
            "row": row_index,
            "cells": [list(cell) for cell in cells_by_row[row_index]],
        }
        missing = sorted(set(range(column_count)) - occupied[row_index])
        if missing:
            row_entry["missing_cols"] = missing
            row_entry["error"] = "row_missing_columns"
        compact_rows.append(row_entry)

    raw_needs_review = table.get("needs_review_cells")
    if raw_needs_review is None:
        review_cells = derived_needs_review
    else:
        if not isinstance(raw_needs_review, list):
            raise ValueError(f"{table_id}.needs_review_cells 必须是列表")
        review_cells: set[tuple[int, int]] = set()
        for item in raw_needs_review:
            if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
                raise ValueError(f"{table_id}.needs_review_cells 项必须是二元组")
            row = _require_int(item[0], f"{table_id}.needs_review.row")
            col = _require_int(item[1], f"{table_id}.needs_review.col")
            if row not in row_indices or not 0 <= col < column_count:
                raise ValueError(f"{table_id}.needs_review 坐标越界")
            review_cells.add((row, col))
        if review_cells != derived_needs_review:
            raise ValueError(f"{table_id}.needs_review_cells 与单元格标记不一致")
    return {
        "table_id": table_id,
        "column_count": column_count,
        "header_rows": header_rows,
        "header_paths": header_paths,
        "rows": compact_rows,
        "spans": spans,
        "needs_review": [[row, col] for row, col in sorted(review_cells)],
    }


def model_native_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """完整 canonical context → 仅含定位/候选文本的压缩模型输入。"""
    if not isinstance(context, Mapping):
        raise ValueError("native context 必须是对象")
    _verify_context_hash(context)
    page_no = _require_int(context.get("page_no"), "page_no")
    if page_no < 1:
        raise ValueError("page_no 必须 >= 1")
    source_format = context.get("source_format")
    if source_format not in {"pdf", "html"}:
        raise ValueError("source_format 必须是 pdf 或 html")
    raw_tables = context.get("tables")
    if not isinstance(raw_tables, list):
        raise ValueError("native context tables 必须是列表")
    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_table in raw_tables:
        if not isinstance(raw_table, Mapping):
            raise ValueError("native context tables 项必须是对象")
        table = _compact_table(raw_table)
        table_id = table["table_id"]
        if table_id in seen:
            raise ValueError(f"native context table_id 重复：{table_id}")
        seen.add(table_id)
        tables.append(table)
    return {
        "page_no": page_no,
        "source_format": source_format,
        "tables": tables,
    }


def native_cell_prompt_sha256() -> str:
    return NATIVE_CELL_CROP_PROMPT_SHA256


def payload_digest(payload: Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json(dict(payload)))


def _digest_value(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限正数")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} 必须是有限正数")
    return number


def source_audit_summary(audit: Any) -> dict[str, Any]:
    """原件 guard 的可复算摘要；clip 文本只留审计，不进模型上下文。"""
    checked_cells = [dict(item) for item in getattr(audit, "checked_cells", ()) or ()]
    status_counts = Counter(str(item.get("status") or "") for item in checked_cells)
    unresolved = [
        dict(item)
        for item in checked_cells
        if str(item.get("status") or "").startswith("unresolved_")
    ]
    mismatches = [dict(item) for item in getattr(audit, "mismatches", ()) or ()]
    warnings = [dict(item) for item in getattr(audit, "warnings", ()) or ()]
    page_no = _require_int(getattr(audit, "page_no", None), "source_audit.page_no")
    if page_no < 1:
        raise ValueError("source_audit.page_no 必须 >= 1")
    summary = {
        "version": NATIVE_SOURCE_AUDIT_VERSION,
        "source_sha256": _require_sha(
            getattr(audit, "source_sha256", None), "source_audit.source_sha256"
        ),
        "page_no": page_no,
        "page_width": _positive_number(
            getattr(audit, "page_width", None), "source_audit.page_width"
        ),
        "page_height": _positive_number(
            getattr(audit, "page_height", None), "source_audit.page_height"
        ),
        "has_text_layer": bool(getattr(audit, "has_text_layer", False)),
        "checked_cell_count": len(checked_cells),
        "status_counts": dict(sorted(status_counts.items())),
        "checked_cells_sha256": _digest_value(checked_cells),
        "unresolved": unresolved,
        "mismatches": mismatches,
        "warnings": warnings,
    }
    return summary


def source_audit_digest(summary: Mapping[str, Any]) -> str:
    return payload_digest(summary)


def _geometry_rows(mapping: Any, name: str) -> dict[str, list[int]]:
    if not isinstance(mapping, Mapping):
        raise ValueError(f"{name} 必须是映射")
    result: dict[str, list[int]] = {}
    for table_id, rows in mapping.items():
        if not isinstance(table_id, str) or not table_id:
            raise ValueError(f"{name} table_id 非法")
        if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
            raise ValueError(f"{name}.{table_id} 必须是行号集合")
        result[table_id] = sorted({_require_int(row, f"{name}.{table_id}") for row in rows})
    return result


def html_geometry_summary(
    geometry: Any, *, render_pdf_sha256: str = ""
) -> dict[str, Any]:
    """HTML 原生行→渲染 PDF 页/bbox 映射的可复算摘要。"""
    page_tables = getattr(geometry, "page_tables", None)
    if not isinstance(page_tables, Mapping):
        raise ValueError("html geometry.page_tables 必须是映射")
    pages: list[dict[str, Any]] = []
    for raw_page, tables in sorted(
        page_tables.items(), key=lambda item: _require_int(item[0], "geometry.page")
    ):
        page_no = _require_int(raw_page, "geometry.page")
        if page_no < 1:
            raise ValueError("geometry.page 必须 >= 1")
        if isinstance(tables, (str, bytes)) or not isinstance(tables, Sequence):
            raise ValueError(f"geometry.page_tables[{page_no}] 必须是列表")
        page_entries: list[dict[str, Any]] = []
        for table in tables:
            table_id = str(getattr(table, "table_id", "") or "")
            if not table_id:
                raise ValueError("geometry page table 缺少 table_id")
            row_indices = sorted(
                {
                    _require_int(row.row_index, f"{table_id}.row_index")
                    for row in getattr(table, "rows", ()) or ()
                }
            )
            page_entries.append(
                {"table_id": table_id, "row_indices": row_indices}
            )
        pages.append({"page": page_no, "tables": page_entries})

    unresolved = [
        dict(item) for item in getattr(geometry, "unresolved", ()) or ()
    ]
    render_pdf_sha = str(render_pdf_sha256 or "")
    if render_pdf_sha:
        render_pdf_sha = _require_sha(render_pdf_sha, "render_pdf_sha256")
    return {
        "version": NATIVE_HTML_GEOMETRY_VERSION,
        "render_pdf_sha256": render_pdf_sha,
        "pages": pages,
        "expected_rows": _geometry_rows(
            getattr(geometry, "expected_rows", None), "geometry.expected_rows"
        ),
        "mapped_rows": _geometry_rows(
            getattr(geometry, "mapped_rows", None), "geometry.mapped_rows"
        ),
        "unresolved": unresolved,
        "row_mapping_sha256": _digest_value(
            list(getattr(geometry, "row_mapping", ()) or ())
        ),
        "cell_mapping_sha256": _digest_value(
            list(getattr(geometry, "cell_mapping", ()) or ())
        ),
    }


def html_geometry_digest(summary: Mapping[str, Any]) -> str:
    return payload_digest(summary)


def native_grid_cache_key(
    *,
    source_sha256: str,
    image_sha256: str,
    render_sha256: str,
    renderer_version: str,
    provider_version: str,
    model: str,
    prompt_sha256: str,
    cell_prompt_sha256: str,
    native_context_sha256: str,
    page_no: int,
    dpi: int,
    source_audit_sha256: str = "",
    geometry_sha256: str = "",
) -> str:
    """核验缓存与转写缓存完全分离：mode/native/guard/geometry 都进入键。"""
    values = [
        NATIVE_GRID_MODE,
        _require_sha(source_sha256, "source_sha256"),
        _require_sha(image_sha256, "image_sha256"),
        _require_sha(render_sha256, "render_sha256"),
        str(renderer_version),
        str(provider_version),
        str(model),
        _require_sha(prompt_sha256, "prompt_sha256"),
        _require_sha(cell_prompt_sha256, "cell_prompt_sha256"),
        _require_sha(native_context_sha256, "native_context_sha256"),
        str(_require_int(page_no, "page_no")),
        str(_require_int(dpi, "dpi")),
    ]
    if source_audit_sha256:
        values.append(
            "audit:" + _require_sha(source_audit_sha256, "source_audit_sha256")
        )
    if geometry_sha256:
        values.append("geometry:" + _require_sha(geometry_sha256, "geometry_sha256"))
    digest = hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()
    return f"{NATIVE_GRID_MODE}-{digest}"


def native_crop_bbox(
    bbox: Sequence[float],
    *,
    page_width: float,
    page_height: float,
    render_width: int,
    render_height: int,
    inset_points: float = NATIVE_GRID_INSET_POINTS,
) -> tuple[int, int, int, int]:
    """PDF 点坐标→本页实际像素裁剪框；直接复用 resolver 的几何实现。"""
    if float(inset_points) != NATIVE_GRID_INSET_POINTS:
        raise ValueError(
            "native_crop_bbox 只支持 resolver 约定的 1pt 内缩，"
            "自定义 inset 会导致裁剪与 resolver 校验不一致"
        )
    return expected_native_crop_bbox(
        bbox,
        page_width=page_width,
        page_height=page_height,
        render_width=render_width,
        render_height=render_height,
    )


def native_crop_path(
    cache_root: Path,
    *,
    doc_id: str,
    version_id: str,
    image_sha256: str,
    table_id: str,
    row: int,
    col: int,
    crop_bbox: tuple[int, int, int, int],
) -> Path:
    image_sha = _require_sha(image_sha256, "image_sha256")
    slug = hashlib.sha256(str(table_id).encode("utf-8")).hexdigest()[:12]
    x0, y0, x1, y1 = crop_bbox
    return (
        Path(cache_root)
        / _cache_component(doc_id, "doc")
        / _cache_component(version_id, "version")
        / "native_crops"
        / image_sha
        / f"{slug}-r{row}c{col}-{x0}_{y0}_{x1}_{y1}.png"
    )


def _crop_from_parent(parent_path: Path, crop_bbox: tuple[int, int, int, int]) -> Any:
    from PIL import Image

    with Image.open(parent_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        x0, y0, x1, y1 = crop_bbox
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("裁剪框与实际父图像尺寸不一致")
        return image.crop((x0, y0, x1, y1))


def write_native_crop(
    parent_path: Path,
    crop_bbox: tuple[int, int, int, int],
    output_path: Path,
) -> dict[str, Any]:
    """在真实 page.png 上原样裁剪（不放大），并复核裁剪像素。"""
    crop = _crop_from_parent(parent_path, crop_bbox)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    buffer_path = output_path.with_suffix(f".{os.getpid()}.tmp")
    try:
        crop.save(buffer_path, format="PNG")
        buffer_path.replace(output_path)
    finally:
        if buffer_path.exists():
            buffer_path.unlink(missing_ok=True)
    png = output_path.read_bytes()
    return {
        "png": png,
        "crop_image_sha256": hashlib.sha256(png).hexdigest(),
        "crop_width": crop.width,
        "crop_height": crop.height,
    }


def verify_native_crop(
    parent_path: Path,
    crop_path: Path,
    crop_bbox: tuple[int, int, int, int],
) -> None:
    """编码字节可以不同，但尺寸与像素必须逐字节等于父图实际裁剪。"""
    expected = _crop_from_parent(parent_path, crop_bbox)
    from PIL import Image

    with Image.open(crop_path) as image:
        actual = image.convert("RGB")
        if actual.size != expected.size:
            raise ValueError("crop 图像尺寸与父图裁剪不一致")
        if actual.tobytes() != expected.tobytes():
            raise ValueError("crop 图像像素与父图实际裁剪不一致")


def unique_pending_cells(check: NativeGridCheck) -> list[PendingCell]:
    """同一格可能同时是 correction 与 native needs_review，只裁剪一次。"""
    selected: dict[tuple[str, int, int], PendingCell] = {}
    for cell in check.pending_cells:
        key = (cell.table_id, cell.row, cell.col)
        prior = selected.get(key)
        if prior is None or _PENDING_PRIORITY.get(
            cell.reason, 9
        ) < _PENDING_PRIORITY.get(prior.reason, 9):
            selected[key] = cell
    return [selected[key] for key in sorted(selected)]


def context_cells(context: Mapping[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    index: dict[tuple[str, int, int], dict[str, Any]] = {}
    for table in context.get("tables", []):
        if not isinstance(table, Mapping):
            continue
        table_id = str(table.get("table_id") or "")
        for row in table.get("rows", []):
            if not isinstance(row, Mapping):
                continue
            for cell in row.get("cells", []):
                if not isinstance(cell, Mapping):
                    continue
                if cell.get("covered"):
                    continue
                index[(table_id, int(cell["row"]), int(cell["col"]))] = dict(cell)
    return index


def build_observation(
    *,
    cell: PendingCell,
    source_sha256: str,
    parent_image_sha256: str,
    crop_image_sha256: str,
    parent_image_path: Path,
    crop_image_path: Path,
    crop_bbox: tuple[int, int, int, int],
    native_bbox: Sequence[float],
    observed_text: str,
    uncertain: bool,
    model: str,
    page_no: int,
    native_context_sha256: str,
) -> NativeCellObservation:
    return NativeCellObservation(
        table_id=cell.table_id,
        row=cell.row,
        col=cell.col,
        source_sha256=_require_sha(source_sha256, "source_sha256"),
        parent_image_sha256=_require_sha(parent_image_sha256, "parent_image_sha256"),
        crop_image_sha256=_require_sha(crop_image_sha256, "crop_image_sha256"),
        parent_image_path=str(Path(parent_image_path).resolve()),
        crop_image_path=str(Path(crop_image_path).resolve()),
        crop_bbox=crop_bbox,
        native_bbox=tuple(float(value) for value in native_bbox),  # type: ignore[arg-type]
        observed_text=str(observed_text),
        uncertain=bool(uncertain),
        model=str(model),
        prompt_sha256=native_cell_prompt_sha256(),
        page_no=page_no,
        native_context_sha256=_require_sha(native_context_sha256, "native_context_sha256"),
    )


def observation_record(
    observation: NativeCellObservation,
    *,
    cache_root: Path,
    response_sha256: str = "",
) -> dict[str, Any]:
    crop_path = Path(observation.crop_image_path)
    try:
        crop_relpath = crop_path.resolve().relative_to(Path(cache_root).resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("native crop 不在缓存目录内") from exc
    return {
        "table_id": observation.table_id,
        "row": observation.row,
        "col": observation.col,
        "source_sha256": observation.source_sha256,
        "parent_image_sha256": observation.parent_image_sha256,
        "crop_image_sha256": observation.crop_image_sha256,
        "crop_relpath": crop_relpath,
        "crop_bbox": list(observation.crop_bbox),
        "native_bbox": list(observation.native_bbox),
        "observed_text": observation.observed_text,
        "uncertain": observation.uncertain,
        "model": observation.model,
        "prompt_sha256": observation.prompt_sha256,
        "page_no": observation.page_no,
        "native_context_sha256": observation.native_context_sha256,
        "response_sha256": str(response_sha256 or ""),
    }


def normalize_additional_content(
    items: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """只接受可见文本块；坐标、候选、证明字段一律丢弃。"""
    accepted: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            warnings.append(f"native additional_content 第 {index} 项不是对象，已忽略")
            continue
        text = item.get("text")
        kind = item.get("kind", "paragraph")
        if set(item) - {"text", "kind"}:
            warnings.append(
                f"native additional_content 第 {index} 项含未允许字段，已忽略"
            )
            continue
        if not isinstance(text, str) or not text.strip():
            warnings.append(f"native additional_content 第 {index} 项没有可见文本，已忽略")
            continue
        if not isinstance(kind, str) or kind not in _NATIVE_ADDITIONAL_KINDS:
            warnings.append(f"native additional_content 第 {index} 项类型非法，已忽略")
            continue
        accepted.append({"kind": kind, "text": text})
    return accepted, warnings


def parse_cell_payload(payload: Any) -> tuple[str, bool]:
    """单格模型只能返回 observed_text/uncertain；任何证明字段都拒绝。"""
    if not isinstance(payload, Mapping):
        raise ValueError("单格识别响应必须是 JSON 对象")
    if set(payload) != {"observed_text", "uncertain"}:
        raise ValueError("单格识别响应只能包含 observed_text/uncertain")
    observed_text = payload.get("observed_text")
    uncertain = payload.get("uncertain")
    if not isinstance(observed_text, str):
        raise ValueError("observed_text 必须是字符串")
    if not isinstance(uncertain, bool):
        raise ValueError("uncertain 必须是布尔值")
    return observed_text, uncertain


def _correction_audit(items: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {
            "table_id": item.table_id,
            "row": item.row,
            "col": item.col,
            "observed_text": item.observed_text,
            "reason": item.reason,
        }
        for item in items
    ]


def decision_audit(
    decision: NativeGridDecision,
    *,
    additional_content: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """确定性审计：不含文件路径，便于缓存重算后逐字节比较。"""
    check = decision.check
    observations = [
        {
            "table_id": item.table_id,
            "row": item.row,
            "col": item.col,
            "crop_image_sha256": item.crop_image_sha256,
            "parent_image_sha256": item.parent_image_sha256,
            "crop_bbox": list(item.crop_bbox),
            "native_bbox": list(item.native_bbox),
            "observed_text": item.observed_text,
            "uncertain": item.uncertain,
            "model": item.model,
            "prompt_sha256": item.prompt_sha256,
        }
        for item in sorted(
            decision.observations, key=lambda item: (item.table_id, item.row, item.col)
        )
    ]
    return {
        "page_no": check.page_no,
        "source_format": check.source_format,
        "source_sha256": check.source_sha256,
        "page_image_sha256": check.page_image_sha256,
        "native_context_sha256": check.native_context_sha256,
        "table_checks": [
            {
                "table_id": item.table_id,
                "checked_rows": list(item.checked_rows),
                "verified": item.verified,
                "corrections": _correction_audit(item.corrections),
                "uncertain_cells": [list(cell) for cell in item.uncertain_cells],
                "missing_rows": item.missing_rows,
                "missing_columns": item.missing_columns,
            }
            for item in check.table_checks
        ],
        "parser_unresolved": [
            {
                "table_id": item.table_id,
                "row": item.row,
                "col": item.col,
                "reason": item.reason,
            }
            for item in check.unresolved
        ],
        "pending_cells": [
            {
                "table_id": item.table_id,
                "row": item.row,
                "col": item.col,
                "reason": item.reason,
            }
            for item in check.pending_cells
        ],
        "additional_content": [dict(item) for item in additional_content],
        "warnings": list(check.warnings),
        "approved_corrections": _correction_audit(decision.approved_corrections),
        "rejected_proposals": [
            {
                "table_id": item.table_id,
                "row": item.row,
                "col": item.col,
                "native_text": item.native_text,
                "proposed_text": item.proposed_text,
                "reason": item.reason,
            }
            for item in decision.rejected_proposals
        ],
        "unresolved": [
            {
                "table_id": item.table_id,
                "row": item.row,
                "col": item.col,
                "reason": item.reason,
            }
            for item in decision.unresolved
        ],
        "confirmed_cells": [
            {
                "table_id": item.table_id,
                "row": item.row,
                "col": item.col,
                "native_text": item.native_text,
                "observed_text": item.observed_text,
                "clear_needs_review": item.clear_needs_review,
            }
            for item in decision.confirmed_cells
        ],
        "observations": observations,
        "released_not_verified": list(decision.released_not_verified),
        "allow_confirmed_corrections": bool(decision.allow_confirmed_corrections),
        "publishable": bool(decision.publishable),
    }


def decision_digest(audit: Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json(dict(audit)))


def _observations_from_record(
    record: Any,
    *,
    cache_root: Path,
    parent_path: Path,
    parent_image_sha256: str,
    source_sha256: str,
    page_no: int,
    native_context_sha256: str,
) -> tuple[list[NativeCellObservation], list[dict[str, Any]]]:
    if not isinstance(record, list):
        raise ValueError("缓存 observations 必须是列表")
    observations: list[NativeCellObservation] = []
    audit_records: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for raw in record:
        if not isinstance(raw, Mapping):
            raise ValueError("缓存 observation 项必须是对象")
        table_id = raw.get("table_id")
        if not isinstance(table_id, str) or not table_id:
            raise ValueError("缓存 observation 缺少 table_id")
        row = _require_int(raw.get("row"), "observation.row")
        col = _require_int(raw.get("col"), "observation.col")
        key = (table_id, row, col)
        if key in seen:
            raise ValueError(f"缓存 observation 坐标重复：{key}")
        seen.add(key)
        if _require_sha(raw.get("source_sha256"), "observation.source_sha256") != source_sha256:
            raise ValueError("缓存 observation 来自其他源文件")
        if (
            _require_sha(raw.get("parent_image_sha256"), "observation.parent_image_sha256")
            != parent_image_sha256
        ):
            raise ValueError("缓存 observation 父图 SHA 与当前页面图不一致")
        crop_sha = _require_sha(raw.get("crop_image_sha256"), "observation.crop_image_sha256")
        if raw.get("native_context_sha256") != native_context_sha256:
            raise ValueError("缓存 observation native context 不一致")
        if _require_int(raw.get("page_no"), "observation.page_no") != page_no:
            raise ValueError("缓存 observation 页码不一致")
        if raw.get("prompt_sha256") != native_cell_prompt_sha256():
            raise ValueError("缓存 observation 单格 prompt 不匹配")
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("缓存 observation 缺少模型")
        observed_text = raw.get("observed_text")
        uncertain = raw.get("uncertain")
        if not isinstance(observed_text, str) or not isinstance(uncertain, bool):
            raise ValueError("缓存 observation 缺少 observed_text/uncertain")
        crop_relpath = raw.get("crop_relpath")
        if not isinstance(crop_relpath, str) or not crop_relpath:
            raise ValueError("缓存 observation 缺少 crop_relpath")
        crop_path = (Path(cache_root) / crop_relpath).resolve()
        try:
            crop_path.relative_to(Path(cache_root).resolve())
        except ValueError as exc:
            raise ValueError("缓存 crop 路径越界") from exc
        if not crop_path.is_file() or _sha256_path(crop_path) != crop_sha:
            raise ValueError("缓存 crop 文件缺失或 SHA 不匹配")
        crop_bbox = _require_bbox_list(raw.get("crop_bbox"), "crop_bbox", integer=True)
        native_bbox = _require_bbox_list(raw.get("native_bbox"), "native_bbox", integer=False)
        verify_native_crop(parent_path, crop_path, crop_bbox)  # type: ignore[arg-type]
        observation = NativeCellObservation(
            table_id=table_id,
            row=row,
            col=col,
            source_sha256=source_sha256,
            parent_image_sha256=parent_image_sha256,
            crop_image_sha256=crop_sha,
            parent_image_path=str(parent_path.resolve()),
            crop_image_path=str(crop_path),
            crop_bbox=crop_bbox,  # type: ignore[arg-type]
            native_bbox=tuple(float(value) for value in native_bbox),
            observed_text=observed_text,
            uncertain=uncertain,
            model=model,
            prompt_sha256=native_cell_prompt_sha256(),
            page_no=page_no,
            native_context_sha256=native_context_sha256,
        )
        observations.append(observation)
        audit_records.append(dict(raw))
    return observations, audit_records


def load_native_grid_record(
    record: Any,
    *,
    expected: Mapping[str, Any],
    context: Mapping[str, Any],
    cache_root: Path,
    parent_path: Path,
    allow_confirmed_corrections: bool,
) -> tuple[NativeGridCheck, NativeGridDecision, list[dict[str, Any]], str]:
    """缓存复用前重验原生 hash、父图/crop 像素并重跑 resolver。"""
    if not isinstance(record, Mapping):
        raise ValueError("native 缓存记录不是对象")
    if record.get("schema_version") != NATIVE_GRID_CACHE_SCHEMA_VERSION:
        raise ValueError("native 缓存 schema 不匹配")
    if record.get("mode") != NATIVE_GRID_MODE:
        raise ValueError("native 缓存 mode 不匹配")
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"native 缓存字段 {key} 不匹配")
    source_audit = record.get("source_audit")
    expected_audit_sha = str(expected.get("source_audit_sha256") or "")
    if expected_audit_sha:
        if not isinstance(source_audit, Mapping):
            raise ValueError("native 缓存缺少 source audit 摘要")
        if source_audit_digest(source_audit) != expected_audit_sha:
            raise ValueError("native 缓存 source audit 摘要不一致")
    elif source_audit not in (None, {}):
        raise ValueError("native 缓存不应包含 source audit 摘要")
    source_sha256 = _require_sha(expected["source_sha256"], "expected.source_sha256")
    page_image_sha256 = _require_sha(expected["image_sha256"], "expected.image_sha256")
    native_context_sha256 = _require_sha(
        expected["native_context_sha256"], "expected.native_context_sha256"
    )
    if context.get("native_context_sha256") != native_context_sha256:
        raise ValueError("native 缓存 context hash 与当前原生网格不一致")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("native 缓存缺少原始核验 payload")
    if record.get("payload_sha256") != _sha256_text(_canonical_json(dict(payload))):
        raise ValueError("native 缓存 payload SHA 不匹配")
    check = parse_native_grid_check(
        dict(payload),
        context=dict(context),
        source_sha256=source_sha256,
        page_image_sha256=page_image_sha256,
    )
    if check.native_context_sha256 != native_context_sha256:
        raise ValueError("native 缓存 check 与 context hash 不一致")
    if _sha256_path(parent_path) != page_image_sha256:
        raise ValueError("native 缓存父图 SHA 与当前页面图不一致")
    observations, audit_records = _observations_from_record(
        record.get("observations"),
        cache_root=cache_root,
        parent_path=parent_path,
        parent_image_sha256=page_image_sha256,
        source_sha256=source_sha256,
        page_no=check.page_no,
        native_context_sha256=native_context_sha256,
    )
    additional, _warnings = normalize_additional_content(check.additional_content)
    decision = resolve_native_grid_check(
        check,
        tuple(observations),
        context=dict(context),
        allow_confirmed_corrections=allow_confirmed_corrections,
    )
    audit = decision_audit(decision, additional_content=additional)
    if record.get("decision_audit_sha256") != decision_digest(audit):
        raise ValueError("native 缓存 decision 重算不一致")
    verify_response = record.get("verify_response_sha256")
    if verify_response is not None and not isinstance(verify_response, str):
        raise ValueError("native 缓存 verify_response_sha256 非法")
    return check, decision, audit_records, str(verify_response or "")


__all__ = [
    "NATIVE_GRID_AUDIT_MODE",
    "NATIVE_GRID_CACHE_SCHEMA_VERSION",
    "NATIVE_GRID_INSET_POINTS",
    "NATIVE_GRID_MODE",
    "NATIVE_HTML_GEOMETRY_VERSION",
    "NATIVE_MODEL_CONTEXT_VERSION",
    "NATIVE_SOURCE_AUDIT_VERSION",
    "NATIVE_VERIFY_USER_PREFIX",
    "build_observation",
    "context_cells",
    "decision_audit",
    "decision_digest",
    "html_geometry_digest",
    "html_geometry_summary",
    "load_native_grid_record",
    "model_native_context",
    "native_cell_prompt_sha256",
    "native_crop_bbox",
    "native_crop_path",
    "native_grid_cache_key",
    "native_verification_prompt_sha256",
    "normalize_additional_content",
    "observation_record",
    "parse_cell_payload",
    "payload_digest",
    "source_audit_digest",
    "source_audit_summary",
    "unique_pending_cells",
    "verify_native_crop",
    "write_native_crop",
]
