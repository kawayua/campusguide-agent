"""Map native HTML tables onto real rendered-PDF page geometry.

Native HTML values, IDs, spans, and row indices stay authoritative. A locally
rendered PDF supplies only page assignment and cell bboxes; this module never
mutates either input structure and never invents missing geometry.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from campusguide.ingest.structure import ParsedStructure, TableCell, TableRow, TableStructure

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class HtmlGridGeometry:
    page_tables: dict[int, list[TableStructure]]
    expected_rows: dict[str, set[int]]
    mapped_rows: dict[str, set[int]]
    unresolved: list[dict[str, Any]]
    row_mapping: list[dict[str, Any]]
    cell_mapping: list[dict[str, Any]]


@dataclass(slots=True)
class _RowView:
    row: TableRow
    row_index: int
    is_header: bool
    cells: dict[int, TableCell]
    signature: tuple[str | None, ...]


@dataclass(slots=True)
class _TableView:
    table: TableStructure
    table_id: str
    order: int
    column_count: int
    rows: list[_RowView]
    header_rows: list[_RowView]
    data_rows: list[_RowView]
    header_signature: tuple[tuple[str | None, ...], ...]
    page: int | None = None
    page_size: tuple[float, float] | None = None


def _normalized(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def _require_int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数（不接受 bool）")
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


def _page_sizes(structure: ParsedStructure) -> dict[int, tuple[float, float] | None]:
    sizes: dict[int, tuple[float, float] | None] = {}
    for page in structure.pages:
        number = _require_int(page.page, "rendered.pages.page", minimum=1)
        if number in sizes:
            raise ValueError(f"rendered.pages 页码重复：{number}")
        width = page.width
        height = page.height
        if width is None or height is None or isinstance(width, bool) or isinstance(height, bool):
            sizes[number] = None
            continue
        try:
            width_value = float(width)
            height_value = float(height)
        except (TypeError, ValueError):
            sizes[number] = None
            continue
        if (
            not math.isfinite(width_value)
            or not math.isfinite(height_value)
            or width_value <= 0
            or height_value <= 0
        ):
            sizes[number] = None
        else:
            sizes[number] = (width_value, height_value)
    return sizes


def _column_count(table: TableStructure) -> int:
    declared = table.column_count
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
        raise ValueError(f"{table.table_id}.column_count 必须是非负整数")
    derived = 0
    for row in table.rows:
        for cell in row.cells:
            col = _require_int(cell.col, f"{table.table_id}.cell.col")
            _require_int(cell.rowspan, f"{table.table_id}.cell.rowspan", minimum=1)
            colspan = _require_int(cell.colspan, f"{table.table_id}.cell.colspan", minimum=1)
            _require_int(cell.origin_row, f"{table.table_id}.cell.origin_row")
            _require_int(cell.origin_col, f"{table.table_id}.cell.origin_col")
            if not isinstance(cell.covered, bool):
                raise ValueError(f"{table.table_id}.covered 必须是 bool")
            if cell.covered:
                # Covered cells inherit the origin span; they do not add columns.
                continue
            derived = max(derived, col + colspan)
    if declared > 0 and derived > declared:
        raise ValueError(f"{table.table_id} column_count 与单元格不一致")
    return declared if declared > 0 else derived


def _row_views(
    table: TableStructure, column_count: int, *, strict_covered: bool
) -> list[_RowView]:
    table_id = table.table_id
    header_indices = {
        _require_int(item, f"{table_id}.header_row") for item in table.header_row_indices
    }
    rows_by_index: dict[int, TableRow] = {}
    cells_by_position: dict[tuple[int, int], TableCell] = {}
    for row in table.rows:
        row_index = _require_int(row.row_index, f"{table_id}.row")
        if row_index in rows_by_index:
            raise ValueError(f"{table_id} 行号重复：{row_index}")
        rows_by_index[row_index] = row
        for cell in row.cells:
            cell_row = _require_int(cell.row, f"{table_id}.cell.row")
            col = _require_int(cell.col, f"{table_id}.cell.col")
            _require_int(cell.rowspan, f"{table_id}.cell.rowspan", minimum=1)
            colspan = _require_int(cell.colspan, f"{table_id}.cell.colspan", minimum=1)
            origin_row = _require_int(cell.origin_row, f"{table_id}.cell.origin_row")
            origin_col = _require_int(cell.origin_col, f"{table_id}.cell.origin_col")
            if cell_row != row_index:
                raise ValueError(f"{table_id} 单元格行号与行不一致")
            if column_count > 0 and col >= column_count:
                raise ValueError(f"{table_id} 单元格列号超出 column_count")
            if (row_index, col) in cells_by_position:
                raise ValueError(f"{table_id} 单元格坐标重复：{row_index},{col}")
            if not isinstance(cell.covered, bool):
                raise ValueError(f"{table_id}.covered 必须是 bool")
            if not cell.covered:
                if origin_row != row_index or origin_col != col:
                    raise ValueError(f"{table_id} origin 格坐标与其位置不一致")
                if column_count > 0 and col + colspan > column_count:
                    raise ValueError(f"{table_id} colspan 超出 column_count")
            cells_by_position[(row_index, col)] = cell
    if not header_indices.issubset(rows_by_index):
        raise ValueError(f"{table_id} header_row_indices 不在原生行内")

    if strict_covered:
        for row_index, row in rows_by_index.items():
            for cell in row.cells:
                if not cell.covered:
                    continue
                origin = cells_by_position.get((cell.origin_row, cell.origin_col))
                if origin is None:
                    raise ValueError(f"{table_id} covered 单元格 origin 缺失或不在表内")
                if origin.covered:
                    raise ValueError(f"{table_id} covered 单元格 origin 不能是覆盖格")
                if cell.rowspan != origin.rowspan or cell.colspan != origin.colspan:
                    raise ValueError(f"{table_id} covered 单元格跨度与 origin 不一致")
                if not (
                    origin.row <= row_index < origin.row + origin.rowspan
                    and origin.col <= cell.col < origin.col + origin.colspan
                ):
                    raise ValueError(f"{table_id} covered 单元格不在 origin 覆盖范围内")

    views: list[_RowView] = []
    for row_index in sorted(rows_by_index):
        row = rows_by_index[row_index]
        cells_by_col = {cell.col: cell for cell in row.cells}
        signature: list[str | None] = []
        for col in range(column_count):
            cell = cells_by_col.get(col)
            if cell is None:
                signature.append("")
            elif cell.covered:
                signature.append(None)
            else:
                signature.append(_normalized(str(cell.text)))
        views.append(
            _RowView(
                row=row,
                row_index=row_index,
                is_header=bool(row.is_header or row_index in header_indices),
                cells=cells_by_col,
                signature=tuple(signature),
            )
        )
    return views


def _build_views(
    structure: ParsedStructure,
    *,
    page_sizes: Mapping[int, tuple[float, float] | None] | None,
    strict_covered: bool,
) -> list[_TableView]:
    views: list[_TableView] = []
    seen_ids: set[str] = set()
    for position, table in enumerate(structure.tables):
        if table.kind == "layout":
            continue
        table_id = table.table_id
        if not isinstance(table_id, str) or not table_id:
            raise ValueError("table.table_id 必须是非空字符串")
        if table_id in seen_ids:
            raise ValueError(f"table_id 重复：{table_id}")
        seen_ids.add(table_id)
        column_count = _column_count(table)
        rows = _row_views(table, column_count, strict_covered=strict_covered)
        header_rows = [row for row in rows if row.is_header]
        data_rows = [row for row in rows if not row.is_header]
        page = table.page
        page_size = None
        if page is not None:
            page = _require_int(page, f"{table_id}.page", minimum=1)
            if page_sizes is not None:
                page_size = page_sizes.get(page)
        views.append(
            _TableView(
                table=table,
                table_id=table_id,
                order=position,
                column_count=column_count,
                rows=rows,
                header_rows=header_rows,
                data_rows=data_rows,
                header_signature=tuple(row.signature for row in header_rows),
                page=page,
                page_size=page_size,
            )
        )
    return views


def _unusable_rows(view: _TableView) -> dict[int, str]:
    counts = Counter(row.signature for row in view.data_rows)
    reasons: dict[int, str] = {}
    for row in view.data_rows:
        if all(value is None for value in row.signature):
            reasons[row.row_index] = "no_origin_cells"
        elif not any(value for value in row.signature if value is not None):
            reasons[row.row_index] = "blank_row"
        elif counts[row.signature] > 1:
            reasons[row.row_index] = "duplicate_row_signature"
    return reasons


def _compare_rows(native: _RowView, render: _RowView) -> tuple[bool, str, int | None]:
    if len(native.signature) != len(render.signature):
        return False, "column_count_mismatch", None
    has_origin = False
    has_value = False
    for col, expected in enumerate(native.signature):
        if expected is None:
            continue
        has_origin = True
        if expected:
            has_value = True
        native_cell = native.cells.get(col)
        render_cell = render.cells.get(col)
        if render_cell is not None and render_cell.covered:
            return False, "cell_covered_mismatch", col
        actual = "" if render_cell is None else _normalized(str(render_cell.text))
        if actual != expected:
            if expected == "" and actual:
                return False, "native_blank_render_conflict", col
            return False, "cell_value_mismatch", col
        if (
            native_cell is not None
            and render_cell is not None
            and (
                native_cell.rowspan != render_cell.rowspan
                or native_cell.colspan != render_cell.colspan
            )
        ):
            return False, "span_mismatch", col
    if not has_origin:
        return False, "no_origin_cells", None
    if not has_value:
        return False, "blank_row", None
    return True, "", None


def _valid_bbox(
    value: Sequence[float] | None, page_size: tuple[float, float]
) -> list[float] | None:
    if value is None:
        return None
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != 4
    ):
        return None
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    x0, y0, x1, y1 = numbers
    width, height = page_size
    if not (0.0 <= x0 < x1 <= width and 0.0 <= y0 < y1 <= height):
        return None
    return [x0, y0, x1, y1]


class _GeometryBuilder:
    def __init__(
        self,
        native_views: Sequence[_TableView],
        render_views: Sequence[_TableView],
    ) -> None:
        self.native_views = list(native_views)
        self.render_views = list(render_views)
        self.table_order = {view.table_id: index for index, view in enumerate(self.native_views)}
        self.expected_rows: dict[str, set[int]] = {}
        self.mapped_rows: dict[str, set[int]] = {}
        self.unresolved: list[dict[str, Any]] = []
        self._unresolved_keys: set[tuple[str, int | None, int | None, str]] = set()
        self.row_mapping: list[dict[str, Any]] = []
        self.cell_mapping: list[dict[str, Any]] = []
        self.page_tables: dict[int, list[TableStructure]] = {}
        self.used_positions: set[int] = set()
        self.cursor = 0
        for view in self.native_views:
            self.expected_rows[view.table_id] = {row.row_index for row in view.rows}
            self.mapped_rows[view.table_id] = set()

    def build(self) -> HtmlGridGeometry:
        for view in self.native_views:
            self._map_table(view)
        self._finalize_rows()
        self._sort_audits()
        page_tables = {
            page: sorted(items, key=lambda table: self.table_order[table.table_id])
            for page, items in sorted(self.page_tables.items())
        }
        return HtmlGridGeometry(
            page_tables=page_tables,
            expected_rows={
                view.table_id: self.expected_rows[view.table_id]
                for view in self.native_views
            },
            mapped_rows={
                view.table_id: self.mapped_rows[view.table_id]
                for view in self.native_views
            },
            unresolved=self.unresolved,
            row_mapping=self.row_mapping,
            cell_mapping=self.cell_mapping,
        )

    def _add_unresolved(
        self,
        table_id: str,
        row: int | None,
        col: int | None,
        reason: str,
        *,
        page: int | None = None,
        render_table_id: str | None = None,
    ) -> None:
        key = (table_id, row, col, reason)
        if key in self._unresolved_keys:
            return
        self._unresolved_keys.add(key)
        self.unresolved.append(
            {
                "table_id": table_id,
                "row": row,
                "col": col,
                "reason": reason,
                "page": page,
                "render_table_id": render_table_id,
            }
        )

    def _next_candidate(
        self, view: _TableView
    ) -> tuple[_TableView | None, int | None, str]:
        saw_same_count = False
        for position in range(self.cursor, len(self.render_views)):
            if position in self.used_positions:
                continue
            candidate = self.render_views[position]
            if candidate.column_count != view.column_count:
                continue
            saw_same_count = True
            if candidate.header_signature != view.header_signature:
                continue
            return candidate, position, ""
        return None, None, "header_mismatch" if saw_same_count else "no_render_table"

    def _compare_headers(
        self, view: _TableView, candidate: _TableView
    ) -> tuple[int, int | None, str] | None:
        if len(view.header_rows) != len(candidate.header_rows):
            row_index = view.header_rows[0].row_index if view.header_rows else None
            return (row_index, None, "header_mismatch") if row_index is not None else None
        for native_row, render_row in zip(view.header_rows, candidate.header_rows, strict=True):
            ok, reason, col = _compare_rows(native_row, render_row)
            if not ok:
                return native_row.row_index, col, reason
        return None

    def _map_table(self, view: _TableView) -> None:
        if view.column_count <= 0:
            self._add_unresolved(view.table_id, None, None, "column_count_invalid")
            return
        if not view.rows:
            self._add_unresolved(view.table_id, None, None, "empty_table")
            return
        unusable_rows = _unusable_rows(view)
        for row_index, reason in unusable_rows.items():
            self._add_unresolved(view.table_id, row_index, None, reason)
        if not view.data_rows:
            self._map_header_only(view)
            return

        data_pos = 0
        last_page: int | None = None
        while data_pos < len(view.data_rows):
            candidate, position, reason = self._next_candidate(view)
            if candidate is None:
                self._add_unresolved(view.table_id, None, None, reason)
                return
            if candidate.page is None:
                self._add_unresolved(
                    view.table_id,
                    None,
                    None,
                    "render_page_missing",
                    render_table_id=candidate.table_id,
                )
                return
            if candidate.page_size is None:
                self._add_unresolved(
                    view.table_id,
                    None,
                    None,
                    "render_page_size_missing",
                    page=candidate.page,
                    render_table_id=candidate.table_id,
                )
                return
            if last_page is not None and candidate.page <= last_page:
                self._add_unresolved(
                    view.table_id,
                    None,
                    None,
                    "page_order_mismatch",
                    page=candidate.page,
                    render_table_id=candidate.table_id,
                )
                return

            header_failure = self._compare_headers(view, candidate)
            if header_failure is not None:
                row_index, col, header_reason = header_failure
                self._add_unresolved(
                    view.table_id,
                    row_index,
                    col,
                    header_reason,
                    page=candidate.page,
                    render_table_id=candidate.table_id,
                )
                self._add_unresolved(
                    view.table_id,
                    None,
                    None,
                    "header_mismatch",
                    page=candidate.page,
                    render_table_id=candidate.table_id,
                )
                return

            render_data = candidate.data_rows
            if not render_data:
                self._add_unresolved(
                    view.table_id,
                    None,
                    None,
                    "render_data_missing",
                    page=candidate.page,
                    render_table_id=candidate.table_id,
                )
                return

            prefix, failure = self._match_data(view, candidate, data_pos)
            if prefix == 0:
                failure_row, failure_col, failure_reason = failure
                self._add_unresolved(
                    view.table_id,
                    failure_row,
                    failure_col,
                    failure_reason,
                    page=candidate.page,
                    render_table_id=candidate.table_id,
                )
                return

            pairs = list(zip(view.header_rows, candidate.header_rows, strict=True))
            pairs.extend(
                (view.data_rows[data_pos + offset], render_data[offset])
                for offset in range(prefix)
            )
            self._emit_page_table(view, candidate, pairs, unusable_rows)
            self.used_positions.add(position)
            self.cursor = position + 1
            data_pos += prefix
            last_page = candidate.page

            if failure is not None:
                failure_row, failure_col, failure_reason = failure
                if failure_row is None:
                    self._add_unresolved(
                        view.table_id,
                        None,
                        None,
                        failure_reason,
                        page=candidate.page,
                        render_table_id=candidate.table_id,
                    )
                else:
                    self._add_unresolved(
                        view.table_id,
                        failure_row,
                        failure_col,
                        failure_reason,
                        page=candidate.page,
                        render_table_id=candidate.table_id,
                    )
                return

    def _match_data(
        self, view: _TableView, candidate: _TableView, data_pos: int
    ) -> tuple[int, tuple[int | None, int | None, str] | None]:
        render_data = candidate.data_rows
        prefix = 0
        while prefix < len(render_data):
            if data_pos + prefix >= len(view.data_rows):
                return prefix, (None, None, "render_extra_rows")
            native_row = view.data_rows[data_pos + prefix]
            render_row = render_data[prefix]
            ok, reason, col = _compare_rows(native_row, render_row)
            if not ok:
                return prefix, (native_row.row_index, col, reason)
            prefix += 1
        return prefix, None

    def _map_header_only(self, view: _TableView) -> None:
        candidate, position, reason = self._next_candidate(view)
        if candidate is None:
            self._add_unresolved(view.table_id, None, None, reason)
            return
        if candidate.page is None:
            self._add_unresolved(
                view.table_id,
                None,
                None,
                "render_page_missing",
                render_table_id=candidate.table_id,
            )
            return
        if candidate.page_size is None:
            self._add_unresolved(
                view.table_id,
                None,
                None,
                "render_page_size_missing",
                page=candidate.page,
                render_table_id=candidate.table_id,
            )
            return
        failure = self._compare_headers(view, candidate)
        if failure is not None:
            row_index, col, header_reason = failure
            self._add_unresolved(
                view.table_id,
                row_index,
                col,
                header_reason,
                page=candidate.page,
                render_table_id=candidate.table_id,
            )
            self._add_unresolved(
                view.table_id,
                None,
                None,
                "header_mismatch",
                page=candidate.page,
                render_table_id=candidate.table_id,
            )
            return
        if candidate.data_rows:
            self._add_unresolved(
                view.table_id,
                None,
                None,
                "render_extra_rows",
                page=candidate.page,
                render_table_id=candidate.table_id,
            )
        pairs = list(zip(view.header_rows, candidate.header_rows, strict=True))
        self._emit_page_table(view, candidate, pairs, {})
        self.used_positions.add(position)
        self.cursor = position + 1

    def _emit_page_table(
        self,
        view: _TableView,
        candidate: _TableView,
        pairs: Sequence[tuple[_RowView, _RowView]],
        unusable_rows: Mapping[int, str],
    ) -> None:
        if candidate.page is None or candidate.page_size is None:
            raise ValueError("页面几何缺失时不能写入 page_tables")
        page = candidate.page
        page_size = candidate.page_size
        output_pairs = [
            (native_row, render_row)
            for native_row, render_row in pairs
            if native_row.is_header or native_row.row_index not in unusable_rows
        ]
        if not output_pairs:
            return
        if view.data_rows and not any(
            not native_row.is_header for native_row, _ in output_pairs
        ):
            return

        origin_rows = {native_row.row_index: render_row.row_index for native_row, render_row in pairs}
        copied_rows: list[TableRow] = []
        for native_row, render_row in sorted(output_pairs, key=lambda pair: pair[0].row_index):
            was_mapped = native_row.row_index in self.mapped_rows[view.table_id]
            copied_cells: list[TableCell] = []
            for cell in sorted(native_row.row.cells, key=lambda item: item.col):
                render_cell = render_row.cells.get(cell.col)
                if cell.covered:
                    if cell.origin_row in origin_rows:
                        expected_origin = origin_rows[cell.origin_row]
                        if (
                            render_cell is None
                            or not render_cell.covered
                            or render_cell.origin_row != expected_origin
                            or render_cell.origin_col != cell.origin_col
                            or render_cell.rowspan != cell.rowspan
                            or render_cell.colspan != cell.colspan
                        ):
                            self._add_unresolved(
                                view.table_id,
                                native_row.row_index,
                                cell.col,
                                "covered_origin_mismatch",
                                page=page,
                                render_table_id=candidate.table_id,
                            )
                    bbox = None
                    self.cell_mapping.append(
                        {
                            "table_id": view.table_id,
                            "row_index": native_row.row_index,
                            "col": cell.col,
                            "cell_id": cell.cell_id,
                            "render_table_id": candidate.table_id,
                            "render_row_index": render_row.row_index,
                            "render_cell_id": None if render_cell is None else render_cell.cell_id,
                            "bbox": None,
                            "covered": True,
                            "origin_row": cell.origin_row,
                            "origin_col": cell.origin_col,
                            "page": page,
                        }
                    )
                else:
                    bbox = None
                    if render_cell is None or render_cell.covered:
                        self._add_unresolved(
                            view.table_id,
                            native_row.row_index,
                            cell.col,
                            "render_bbox_missing",
                            page=page,
                            render_table_id=candidate.table_id,
                        )
                    else:
                        bbox = _valid_bbox(render_cell.bbox, page_size)
                        if bbox is None:
                            bbox_reason = (
                                "render_bbox_missing"
                                if render_cell.bbox is None
                                else "render_bbox_invalid"
                            )
                            self._add_unresolved(
                                view.table_id,
                                native_row.row_index,
                                cell.col,
                                bbox_reason,
                                page=page,
                                render_table_id=candidate.table_id,
                            )
                    self.cell_mapping.append(
                        {
                            "table_id": view.table_id,
                            "row_index": native_row.row_index,
                            "col": cell.col,
                            "cell_id": cell.cell_id,
                            "render_table_id": candidate.table_id,
                            "render_row_index": render_row.row_index,
                            "render_cell_id": None if render_cell is None else render_cell.cell_id,
                            "bbox": bbox,
                            "covered": False,
                            "origin_row": cell.origin_row,
                            "origin_col": cell.origin_col,
                            "page": page,
                        }
                    )
                copied_cells.append(cell.model_copy(deep=True, update={"bbox": bbox}))
            copied_row = native_row.row.model_copy(deep=True, update={"cells": copied_cells})
            copied_rows.append(copied_row)
            self.mapped_rows[view.table_id].add(native_row.row_index)
            self.row_mapping.append(
                {
                    "table_id": view.table_id,
                    "row_index": native_row.row_index,
                    "row_id": native_row.row.row_id,
                    "page": page,
                    "render_table_id": candidate.table_id,
                    "render_row_index": render_row.row_index,
                    "is_header": native_row.is_header,
                    "repeated_header": native_row.is_header and was_mapped,
                }
            )

        if not copied_rows:
            return
        page_table = view.table.model_copy(
            deep=True,
            update={
                "page": page,
                "bbox": None,
                "rows": copied_rows,
                "row_count": len(copied_rows),
            },
        )
        existing = self.page_tables.setdefault(page, [])
        if any(table.table_id == view.table_id for table in existing):
            self._add_unresolved(
                view.table_id,
                None,
                None,
                "duplicate_page_table",
                page=page,
                render_table_id=candidate.table_id,
            )
            return
        existing.append(page_table)

    def _finalize_rows(self) -> None:
        rows_with_unresolved = {
            (table_id, row)
            for (table_id, row, _col, _reason) in self._unresolved_keys
            if row is not None
        }
        for view in self.native_views:
            missing = self.expected_rows[view.table_id] - self.mapped_rows[view.table_id]
            for row_index in sorted(missing):
                if (view.table_id, row_index) not in rows_with_unresolved:
                    self._add_unresolved(view.table_id, row_index, None, "row_unmatched")

    def _sort_audits(self) -> None:
        order = self.table_order

        def unresolved_key(item: dict[str, Any]) -> tuple[int, bool, int, int, str]:
            row = item["row"]
            col = item["col"]
            return (
                order.get(item["table_id"], len(order)),
                row is not None,
                -1 if row is None else row,
                -1 if col is None else col,
                item["reason"],
            )

        self.unresolved.sort(key=unresolved_key)
        self.row_mapping.sort(
            key=lambda item: (
                order.get(item["table_id"], len(order)),
                item["row_index"],
                item["page"],
                item["render_row_index"],
            )
        )
        self.cell_mapping.sort(
            key=lambda item: (
                order.get(item["table_id"], len(order)),
                item["row_index"],
                item["col"],
                item["page"],
            )
        )


def build_html_grid_geometry(
    native_structure: ParsedStructure,
    rendered_structure: ParsedStructure,
) -> HtmlGridGeometry:
    if not isinstance(native_structure, ParsedStructure):
        raise ValueError("native_structure 必须是 ParsedStructure")
    if not isinstance(rendered_structure, ParsedStructure):
        raise ValueError("rendered_structure 必须是 ParsedStructure")
    if native_structure.format != "html":
        raise ValueError("native_structure.format 必须是 html")
    if rendered_structure.format != "pdf":
        raise ValueError("rendered_structure.format 必须是 pdf")
    page_sizes = _page_sizes(rendered_structure)
    native_views = _build_views(native_structure, page_sizes=None, strict_covered=True)
    # 渲染侧合并格不一致仍交给既有逐格比对记入 unresolved，不提前失败。
    render_views = _build_views(
        rendered_structure, page_sizes=page_sizes, strict_covered=False
    )
    return _GeometryBuilder(native_views, render_views).build()


__all__ = ["HtmlGridGeometry", "build_html_grid_geometry"]
