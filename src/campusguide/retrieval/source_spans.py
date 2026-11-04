"""Literal source spans for tables/lists: model selects IDs, server copies originals."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from campusguide.domain.models import (
    Article,
    CellProvenance,
    Evidence,
    HeaderColumnProvenance,
)

_HEADER_KEYS = ("课程代码", "课程名称", "学分", "学时", "开课学期", "课程性质", "考核方式")
_MATRIX_KEYS = ("支撑", "矩阵", "培养目标与毕业要求")
_HEADER_START_KEYS = ("课程类别", "课程代码", "课程名称")
_HEADER_WORDS = (
    "课程类别",
    "课程代码",
    "课程名称",
    "学分",
    "总学时",
    "学时",
    "理论学时",
    "实验学时",
    "实习学时",
    "修读学期",
    "开课学期",
    "开课单位",
    "备注",
    "课程性质",
    "考核方式",
    "周数",
    "周学时",
    "模块",
    "方向",
)
_HEADER_SUBWORDS = ("总数", "理论", "实验", "实习")
_HEADER_ALL = set(_HEADER_WORDS) | set(_HEADER_SUBWORDS)
_SEMESTER_RE = re.compile(r"第\s*[一二三四五六七八九十\d]+\s*学期|\d+\s*/\s*\d+|\d+\s*学期")
_WEEKS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*周(?!学时)")

# 本科课程代码 6 位、研究生课程代码 14 位；两者都是课程实体的一部分。
_COURSE_CODE_RE = re.compile(r"(?<!\d)(?:\d{14}|\d{6})(?!\d)")
_STRUCTURED_ROW_KINDS = frozenset({"table_row", "matrix_row"})
# 表头/行块都可跨多行（竖排表头、中英双语课程名）；块边界只认行首标记。
_STRUCTURED_HEADER_RE = re.compile(r"(?m)^表头[：:]")
_STRUCTURED_ROW_RE = re.compile(r"(?m)^行[：:]")
_STRUCTURED_CELL_RE = re.compile(r"^第?\s*(\d+(?:\.\d+)?)\s*(学分|学时|周|学期|元)?$")
_STRUCTURED_FIELD_UNITS = {
    "credits": "学分",
    "total_hours": "学时",
    "theory_hours": "学时",
    "experiment_hours": "学时",
    "practice_hours": "学时",
    "weekly_hours": "学时",
    "weeks": "周",
    "semester": "学期",
    "amount": "元",
}
_HOURS_TOTALS = ("总数", "合计", "总计")
_HOURS_THEORY = ("理论", "讲授")
_HOURS_EXPERIMENT = ("实验", "上机")
_HOURS_PRACTICE = ("实习", "实践", "实训")

_ROW_PREFIX_RE = re.compile(r"^\s*(?:表头|行)[：:]\s*")
_ENTITY_LABEL_RE = re.compile(r"^[^\s，,。；;：:（）()\[\]【】/、!?！？]{2,16}$")
_ENTITY_NUMERIC_UNIT_RE = re.compile(r"\d\s*(?:学分|学时|元|周|学期)")
_FOREIGN_CURRENCY_RE = re.compile(
    r"美元|美金|日元|欧元|港币|港元|澳门元|新台币|英镑|USD|JPY|EUR|GBP|HKD|MOP|TWD",
    re.IGNORECASE,
)
_CNY_UNIT_RE = re.compile(
    r"人民币|RMB|CNY|(?:^|[\s（(【\[、,，/])元(?:[\s）)】\]/、,，]|$)",
    re.IGNORECASE,
)
_REPEATABLE_STRUCTURED_FIELDS = frozenset({"category", "nature"})
_BOUND_BINDING_STATUSES = frozenset({"bound", "shared"})
_UNBOUND_BINDING_STATUSES = frozenset({"unresolved", "offset", "ambiguous"})
_PHYSICAL_BINDING_METHODS = frozenset(
    {"physical_interval", "trailing_empty_recovery"}
)
# 与 structure._HEADER_INTERVAL_MATCH_TOLERANCE 一致：运行时只复核来源
# 抽取器自身的严格物理匹配，不在这里放宽。
_BINDING_INTERVAL_TOLERANCE = 1.0
_BINDING_BBOX_EPSILON = 1e-6


def _normalize_structured_text(text: str) -> str:
    """NFKC 并去掉所有空白；只用于列语义/单元格比对，不用于生成 quote。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _entity_aliases(field: str, value: str) -> list[str]:
    """Entity anchors from one row cell: code, course name (including Chinese line), labels."""
    raw = (value or "").strip()
    if not raw:
        return []
    aliases: list[str] = []

    def add(text: str) -> None:
        # Keep both NFKC and raw forms so a claim using "Ⅰ" or "I" still matches.
        for variant in (unicodedata.normalize("NFKC", text), text):
            alias = re.sub(r"\s+", "", variant)
            if len(alias) >= 2 and alias not in aliases:
                aliases.append(alias)

    if field in {"course_name", "course_code", "category"}:
        add(raw)
    elif field == "other":
        label = _normalize_structured_text(raw)
        if _ENTITY_LABEL_RE.fullmatch(label) and re.search(r"[\u4e00-\u9fff]", label):
            add(label)
    if field == "course_name":
        # 真实 PDF 把中文名和英文名放在同一单元格的相邻行；claim 只需中文行。
        for line in raw.splitlines():
            line_text = _normalize_structured_text(line)
            if len(line_text) >= 2 and re.search(r"[\u4e00-\u9fff]", line_text):
                add(line)
    if field == "category":
        for line in raw.splitlines():
            line_text = _normalize_structured_text(line)
            if len(line_text) < 4 or not re.search(r"[\u4e00-\u9fff]", line_text):
                continue
            if _ENTITY_NUMERIC_UNIT_RE.search(line_text):
                continue
            add(line)
    return aliases


def _cells(line: str) -> list[str]:
    # 结构化行块使用“表头：…”/“行：…”记录原始列，不是 markdown 表格线。
    return [cell.strip() for cell in re.split(r"[|｜]", _ROW_PREFIX_RE.sub("", line))]


def _header_tokens(cell: str) -> tuple[list[str], bool]:
    """Split a flattened header cell; trailing data means it is no longer header."""
    cleaned = re.sub(r"^[\d\s.．、]+", "", cell or "").strip()
    tokens: list[str] = []
    while cleaned:
        if cleaned in _HEADER_ALL:
            tokens.append(cleaned)
            cleaned = ""
            break
        matched = False
        for token in ("备注", *_HEADER_SUBWORDS):
            if cleaned.startswith(token):
                tokens.append(token)
                cleaned = cleaned[len(token) :]
                matched = True
                break
        if not matched:
            break
    return tokens, bool(cleaned)


def _header_info(line: str) -> tuple[tuple[str, ...], bool]:
    """Return cleaned columns and whether positional row mapping is trustworthy.

    PDF flattening can glue a numeric prefix and the first course row to the
    header, and multi-level headers (总学时 -> 总数/理论/实验/实习) lose their
    grouping when flattened. Those columns are still shown as source context,
    but fields must stay gaps instead of being mapped by position.
    """
    cells = [cell for cell in _cells(line) if cell]
    if not cells:
        return (), False
    if any(key in line for key in _MATRIX_KEYS):
        return tuple(cells), True
    start = None
    for index, cell in enumerate(cells):
        cleaned = re.sub(r"^[\d\s.．、]+", "", cell).strip()
        if any(key in cleaned for key in _HEADER_START_KEYS):
            start = index
            break
    if start is None:
        return (), False
    columns: list[str] = []
    reliable = True
    for cell in cells[start:]:
        tokens, trailing = _header_tokens(cell)
        if not tokens:
            reliable = False
            break
        columns.extend(tokens)
        if trailing:
            reliable = False
            break
    if any(token in _HEADER_SUBWORDS for token in columns):
        reliable = False
    if len(columns) < 3:
        return (), False
    return tuple(columns), reliable


def _header_cells(line: str) -> tuple[str, ...]:
    return _header_info(line)[0]


def _header_reliable(line: str) -> bool:
    return _header_info(line)[1]


def _is_header_line(line: str) -> bool:
    if not re.search(r"[|｜]", line):
        return False
    if _COURSE_CODE_RE.match(line.lstrip()):
        return False
    keys = sum(1 for key in _HEADER_KEYS if key in line)
    if keys >= 3:
        return True
    # 支撑矩阵表头常只有“课程代码/课程名称”两个常规列，需靠矩阵列名识别。
    return keys >= 2 and any(key in line for key in _MATRIX_KEYS)


def table_header(text: str, position: int) -> tuple[tuple[str, ...], str]:
    """Nearest preceding header row of a markdown-style table, if any."""
    lines = text[:position].splitlines()[-60:]
    for line in reversed(lines):
        if _is_header_line(line):
            header = _header_cells(line)
            if header:
                return header, line
    return (), ""


def _external_headers(
    evidence: list[Evidence],
) -> dict[str, list[tuple[int, tuple[str, ...], str]]]:
    """Original header rows per article, in part order (never merged into row text)."""
    headers: dict[str, list[tuple[int, tuple[str, ...], str]]] = {}
    for item in evidence:
        article = item.article
        for line in article.text.splitlines():
            if _is_header_line(line):
                cells = _header_cells(line)
                if cells:
                    headers.setdefault(article.article_id, []).append(
                        (article.part_no, cells, line.strip())
                    )
    for rows in headers.values():
        rows.sort(key=lambda row: row[0])
    return headers


def _external_header(
    headers: dict[str, list[tuple[int, tuple[str, ...], str]]],
    article_id: str,
    part_no: int,
) -> tuple[int, tuple[str, ...], str] | None:
    candidates = [
        row
        for row in headers.get(article_id, [])
        # Only earlier fragments: a later header cannot describe this row, and a
        # same-fragment header is found by the local nearest-preceding scan.
        if row[0] < part_no
    ]
    return candidates[-1] if candidates else None


def table_header_context(
    evidence: list[Evidence],
    article_id: str,
    part_no: int,
    position: int = 0,
) -> dict[str, object] | None:
    """Original table header for one fragment, as separate metadata (no joining)."""
    article = next(
        (
            item.article
            for item in evidence
            if item.article.article_id == article_id and item.article.part_no == part_no
        ),
        None,
    )
    if article is not None:
        row = structured_table_row(article)
        if row is not None:
            payload: dict[str, object] = {
                "article_id": article_id,
                "part_no": part_no,
                "quote": row.header_quote,
                "columns": list(row.headers),
                "table_id": row.table_id,
                "row_id": row.row_id,
                "header_paths": [list(column.path) for column in row.columns],
            }
            if row.reliable and not row.is_matrix:
                payload["row_values"] = list(row.values)
            if not row.reliable:
                payload["reliable"] = False
            return payload
        headers, line = table_header(article.text, max(0, min(position, len(article.text))))
        if headers:
            payload: dict[str, object] = {
                "article_id": article_id,
                "part_no": part_no,
                "quote": line.strip(),
                "columns": list(headers),
            }
            if not _header_reliable(line):
                payload["reliable"] = False
            return payload
    external = _external_header(_external_headers(evidence), article_id, part_no)
    if external is None:
        return None
    header_part_no, headers, quote = external
    payload = {
        "article_id": article_id,
        "part_no": header_part_no,
        "quote": quote,
        "columns": list(headers),
    }
    if not _header_reliable(quote):
        payload["reliable"] = False
    return payload


def table_role(
    columns: tuple[str, ...] | list[str],
    header_quote: str = "",
    context: str = "",
) -> str:
    """Classify a table fragment without inventing column semantics."""
    joined = " ".join([*columns, header_quote, context])
    if any(key in joined for key in _MATRIX_KEYS):
        return "support_matrix"
    if sum(1 for key in _HEADER_KEYS if key in joined) >= 3:
        return "curriculum_row"
    return "text"


def _format_structured_number(value: float | str) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def structured_cell_canonical(value: str, field: str) -> str | None:
    """Bind a simple cell value to one field; complex/ambiguous cells stay unsupported."""
    expected_unit = _STRUCTURED_FIELD_UNITS.get(field)
    if expected_unit is None:
        return None
    match = _STRUCTURED_CELL_RE.fullmatch(_normalize_structured_text(value))
    if match is None:
        return None
    unit = match.group(2) or expected_unit
    if unit != expected_unit:
        return None
    return f"{_format_structured_number(match.group(1))}{unit}"


def _is_cny_header(text: str) -> bool:
    """Only an explicit RMB/yuan unit may become a CNY amount column."""
    if _FOREIGN_CURRENCY_RE.search(text):
        return False
    return _CNY_UNIT_RE.search(text) is not None


def _structured_column_field(path: tuple[str, ...]) -> str:
    """Derive a column's business field from its real header path only."""
    if not path:
        return "other"
    parts = [_normalize_structured_text(part) for part in path if part]
    if not parts:
        return "other"
    joined = " ".join(parts)
    last = parts[-1]
    if "课程代码" in joined or "课程编号" in joined or last in {"编号", "代码"}:
        return "course_code"
    if (
        "课程名称" in joined
        or "课程名" in joined
        or ("课程" in joined and last.endswith("名称"))
    ):
        return "course_name"
    if "周学时" in joined or "学时/周" in joined:
        return "weekly_hours"
    if "学分" in joined:
        return "credits"
    if "周数" in joined or last == "周":
        return "weeks"
    if "学期" in joined:
        return "semester"
    if _is_cny_header(joined):
        return "amount"
    if "课程类别" in joined or "模块" in joined or "方向" in joined or last in {
        "类别",
        "模块",
        "方向",
    }:
        return "category"
    if "课程性质" in joined:
        return "nature"
    if "考核" in joined:
        return "assessment"
    if "学时" in joined:
        if any(token in joined for token in _HOURS_TOTALS):
            return "total_hours"
        if any(token in joined for token in _HOURS_THEORY):
            return "theory_hours"
        if any(token in joined for token in _HOURS_EXPERIMENT):
            return "experiment_hours"
        if any(token in joined for token in _HOURS_PRACTICE):
            return "practice_hours"
        return "total_hours"
    return "other"


def _binding_rect(
    bbox: list[float] | None,
) -> tuple[float, float, float, float] | None:
    if bbox is None or len(bbox) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in bbox):
        return None
    values = tuple(float(item) for item in bbox)
    if not all(math.isfinite(item) for item in values):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _binding_interval(bbox: list[float] | None) -> tuple[float, float] | None:
    if bbox is None or len(bbox) not in (2, 4):
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in bbox):
        return None
    values = [float(item) for item in bbox]
    if not all(math.isfinite(item) for item in values):
        return None
    left, right = (values[0], values[1]) if len(values) == 2 else (values[0], values[2])
    if right <= left:
        return None
    return (left, right)


def _binding_bbox_equal(left: list[float] | None, right: list[float] | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if len(left) != len(right):
        return False
    return all(
        abs(float(a) - float(b)) <= _BINDING_BBOX_EPSILON
        for a, b in zip(left, right, strict=True)
    )


def _binding_edge_delta(
    left: tuple[float, float], right: tuple[float, float]
) -> float:
    return max(abs(left[0] - right[0]), abs(left[1] - right[1]))


def _binding_labels(cells: list[CellProvenance]) -> list[str]:
    labels: list[str] = []
    for cell in cells:
        text = _normalize_structured_text(cell.text)
        if text and (not labels or labels[-1] != text):
            labels.append(text)
    return labels


def _binding_has_source_fields(binding: HeaderColumnProvenance) -> bool:
    return bool(
        binding.source_table_id is not None
        or binding.source_via_table_id is not None
        or binding.source_page is not None
        or binding.source_col is not None
        or binding.source_header_cell_ids
        or binding.source_header_row_indices
        or binding.source_bbox is not None
        or binding.source_header_cells
    )


def _binding_geometry_problems(
    binding: HeaderColumnProvenance, index: int
) -> list[str]:
    problems: list[str] = []
    if binding.target_bbox is not None:
        expected_kind = (
            "x_interval"
            if len(binding.target_bbox) == 2
            else "rect" if len(binding.target_bbox) == 4 else None
        )
        if expected_kind is None:
            problems.append(f"BINDING_TARGET_BBOX_INVALID:{index}")
        elif (
            binding.target_bbox_kind is not None
            and binding.target_bbox_kind != expected_kind
        ):
            problems.append(f"BINDING_TARGET_BBOX_KIND:{index}")
        elif (
            (expected_kind == "rect"
            and _binding_rect(binding.target_bbox) is None)
            or (expected_kind == "x_interval"
            and _binding_interval(binding.target_bbox) is None)
        ):
            problems.append(f"BINDING_TARGET_BBOX_INVALID:{index}")
    if (
        binding.source_bbox is not None
        and _binding_rect(binding.source_bbox) is None
    ):
        problems.append(f"BINDING_SOURCE_BBOX_INVALID:{index}")
    return problems


def _binding_source_problems(
    article: Article,
    binding: HeaderColumnProvenance,
    index: int,
    binding_path: tuple[str, ...],
) -> list[str]:
    problems: list[str] = []
    status = binding.status
    method = binding.method
    scope = f"{article.doc_id}@{article.version_id}"
    source_id = binding.source_table_id
    table_id = (article.table_id or "").strip()
    if not source_id or source_id != source_id.strip():
        problems.append(f"BINDING_SOURCE_ID_INVALID:{index}")
    else:
        source_scope = (
            source_id.split("#", 1)[0] if "#" in source_id else source_id
        )
        if source_scope != scope:
            problems.append(f"BINDING_SOURCE_SCOPE:{index}")
        if method == "local_header":
            if source_id != table_id:
                problems.append(f"BINDING_LOCAL_SOURCE:{index}")
            if binding.source_via_table_id is not None:
                problems.append(f"BINDING_LOCAL_VIA:{index}")
            if binding.source_page != article.page or (article.page is None and status in _BOUND_BINDING_STATUSES):
                problems.append(f"BINDING_SOURCE_PAGE:{index}")
        else:
            if source_id == table_id:
                problems.append(f"BINDING_INHERITED_SOURCE:{index}")
            via = binding.source_via_table_id
            if via is not None and (
                via != via.strip() or via in {source_id, table_id}
            ):
                problems.append(f"BINDING_VIA_INVALID:{index}")
            if (
                binding.source_page is None
                or binding.source_page <= 0
                or article.page is None
                or binding.source_page >= article.page
            ):
                problems.append(f"BINDING_SOURCE_PAGE:{index}")
    if binding.source_col is None or binding.source_col < 0:
        problems.append(f"BINDING_SOURCE_COL:{index}")

    rows = list(binding.source_header_row_indices)
    if not rows or len(set(rows)) != len(rows) or any(row < 0 for row in rows):
        problems.append(f"BINDING_SOURCE_ROWS:{index}")
    cells = list(binding.source_header_cells)
    if (
        not binding.source_header_cell_ids
        or [cell.cell_id for cell in cells] != list(binding.source_header_cell_ids)
    ):
        problems.append(f"BINDING_SOURCE_CELLS:{index}")
    source_prefix = f"{source_id}#" if source_id else ""
    previous_row = -1
    for cell in cells:
        if not source_prefix or not cell.cell_id.startswith(source_prefix):
            problems.append(f"BINDING_SOURCE_CELL_ID:{index}")
        if (
            cell.covered
            or not cell.is_header
            or cell.origin_row != cell.row
            or cell.origin_col != cell.col
        ):
            problems.append(f"BINDING_SOURCE_CELL_ORIGIN:{index}")
        if cell.row not in rows:
            problems.append(f"BINDING_SOURCE_CELL_ROW:{index}")
        if cell.row < previous_row:
            problems.append(f"BINDING_SOURCE_CELL_ORDER:{index}")
        previous_row = cell.row
        if cell.bbox is not None and _binding_rect(cell.bbox) is None:
            problems.append(f"BINDING_SOURCE_CELL_BBOX:{index}")
    if any(cell.needs_review for cell in cells):
        # bound/shared 列只要有一个贡献来源格待复核，就不能继续提供列语义；
        # 防止陈旧/伪造 payload 绕过 ingestion 的降级。
        problems.append(f"BINDING_SOURCE_NEEDS_REVIEW:{index}")

    labels = _binding_labels(cells)
    expected_path = tuple(
        _normalize_structured_text(part) for part in binding_path
    )
    label_cell: CellProvenance | None = cells[-1] if cells else None
    if status in _BOUND_BINDING_STATUSES:
        if not labels or tuple(labels) != expected_path:
            problems.append(f"BINDING_SOURCE_LABELS:{index}")
        if not labels or labels[-1] != _normalize_structured_text(
            binding.header_text
        ):
            problems.append(f"BINDING_SOURCE_HEADER_TEXT:{index}")
        nonempty = [cell for cell in cells if _normalize_structured_text(cell.text)]
        label_cell = nonempty[-1] if nonempty else None
    elif labels:
        problems.append(f"BINDING_GAP_LABELS:{index}")
    if label_cell is None:
        problems.append(f"BINDING_SOURCE_LABELS:{index}")
    else:
        if binding.source_col is not None and label_cell.col != binding.source_col:
            problems.append(f"BINDING_SOURCE_COL:{index}")
        if binding.source_bbox is not None and not _binding_bbox_equal(
            binding.source_bbox, label_cell.bbox
        ):
            problems.append(f"BINDING_SOURCE_BBOX:{index}")

    if method in _PHYSICAL_BINDING_METHODS:
        source_interval = _binding_interval(binding.source_bbox)
        target_interval = _binding_interval(binding.target_bbox)
        if source_interval is None or target_interval is None:
            problems.append(f"BINDING_PHYSICAL_GEOMETRY:{index}")
        elif (
            _binding_edge_delta(target_interval, source_interval)
            > _BINDING_INTERVAL_TOLERANCE
        ):
            problems.append(f"BINDING_PHYSICAL_MISMATCH:{index}")
    return problems


def _binding_problems(
    article: Article,
    headers: tuple[str, ...],
    paths: tuple[tuple[str, ...], ...],
    column_count: int,
) -> list[str]:
    """Validate nonempty header-column provenance against article and literal cells."""
    bindings = tuple(article.table_column_bindings)
    if not bindings:
        return []
    problems: list[str] = []
    if len(bindings) != column_count:
        problems.append("BINDING_COUNT_MISMATCH")
    if sorted(binding.target_col for binding in bindings) != list(
        range(column_count)
    ):
        problems.append("BINDING_TARGET_SLOTS_INVALID")
    for binding in bindings:
        index = binding.target_col
        if not 0 <= index < column_count:
            continue
        path = paths[index] if index < len(paths) else ()
        header = headers[index] if index < len(headers) else ""
        status = binding.status
        method = binding.method
        if status in _BOUND_BINDING_STATUSES:
            if method == "none":
                problems.append(f"BINDING_METHOD_INVALID:{index}")
        elif status == "gap":
            if method != "local_header":
                problems.append(f"BINDING_METHOD_INVALID:{index}")
        elif status in _UNBOUND_BINDING_STATUSES:
            if method != "none":
                problems.append(f"BINDING_METHOD_INVALID:{index}")
        else:
            problems.append(f"BINDING_STATUS_INVALID:{index}")
            continue

        binding_path = tuple(part.strip() for part in binding.header_path)
        if status in _BOUND_BINDING_STATUSES:
            if not binding_path or any(not part for part in binding_path):
                problems.append(f"BINDING_HEADER_PATH_INVALID:{index}")
            elif binding_path != path:
                problems.append(f"BINDING_HEADER_PATH_MISMATCH:{index}")
            if _normalize_structured_text(
                " > ".join(binding_path)
            ) != _normalize_structured_text(header):
                problems.append(f"BINDING_HEADER_MISMATCH:{index}")
            if not _normalize_structured_text(binding.header_text):
                problems.append(f"BINDING_HEADER_TEXT_MISSING:{index}")
            if status == "shared" and not binding.shared_scope:
                problems.append(f"BINDING_SHARED_FLAG_MISMATCH:{index}")
            if status == "bound" and binding.shared_scope:
                problems.append(f"BINDING_SHARED_FLAG_MISMATCH:{index}")
        else:
            if binding_path or _normalize_structured_text(binding.header_text):
                problems.append(f"BINDING_UNBOUND_HEADER_PRESENT:{index}")
            if path or header:
                problems.append(f"BINDING_ARTICLE_HEADER_PRESENT:{index}")
            # gap 的 covered/shared_scope 是抽取器记录的上下文，不是列名证据；
            # 它们仍被限制为 other 字段，不能反过来否掉其他列的证明。
            if status in _UNBOUND_BINDING_STATUSES and (
                binding.covered or binding.shared_scope
            ):
                problems.append(f"BINDING_UNBOUND_FLAG_SET:{index}")

        problems.extend(_binding_geometry_problems(binding, index))
        if status in _UNBOUND_BINDING_STATUSES:
            if _binding_has_source_fields(binding):
                problems.append(f"BINDING_UNBOUND_SOURCE_PRESENT:{index}")
            continue
        problems.extend(
            _binding_source_problems(article, binding, index, binding_path)
        )

    physical_sources = [
        (binding.target_col, _binding_interval(binding.source_bbox))
        for binding in bindings
        if binding.status in _BOUND_BINDING_STATUSES
        and binding.method in _PHYSICAL_BINDING_METHODS
        and _binding_interval(binding.source_bbox) is not None
    ]
    for binding in bindings:
        if (
            binding.status not in _BOUND_BINDING_STATUSES
            or binding.method not in _PHYSICAL_BINDING_METHODS
        ):
            continue
        target_interval = _binding_interval(binding.target_bbox)
        matches = [
            target_col
            for target_col, interval in physical_sources
            if target_interval is not None
            and interval is not None
            and _binding_edge_delta(target_interval, interval)
            <= _BINDING_INTERVAL_TOLERANCE
        ]
        if matches != [binding.target_col]:
            problems.append(f"BINDING_PHYSICAL_AMBIGUOUS:{binding.target_col}")
    return problems


def _structured_binding_field(
    path: tuple[str, ...],
    binding: HeaderColumnProvenance | None,
    *,
    bindings_present: bool,
) -> str:
    """Unbound/shared columns keep source context but provide no unique field."""
    if bindings_present and (
        binding is None
        or binding.status == "shared"
        or binding.shared_scope
        or binding.status not in _BOUND_BINDING_STATUSES
    ):
        return "other"
    return _structured_column_field(path)


@dataclass(frozen=True)
class StructuredColumn:
    index: int
    path: tuple[str, ...]
    text: str
    field: str


@dataclass(frozen=True)
class StructuredTableRow:
    """已校验的结构化行：列语义只来自 header_paths，正文值只来自引用行。"""

    table_id: str
    row_id: str
    row_index: int | None
    columns: tuple[StructuredColumn, ...]
    values: tuple[str, ...]
    quote: str
    quote_start: int
    quote_end: int
    header_quote: str
    is_matrix: bool
    reliable: bool
    problems: tuple[str, ...] = ()
    cell_spans: tuple[tuple[int, int], ...] = ()
    # Raw row cells (including covered merged text); never used as row facts.
    raw_values: tuple[str, ...] = ()

    @property
    def headers(self) -> tuple[str, ...]:
        return tuple(column.text for column in self.columns)

    def value(self, field: str) -> str:
        for column, value in zip(self.columns, self.values, strict=True):
            if column.field == field:
                return value
        return ""

    @property
    def course_code(self) -> str:
        return self.value("course_code")

    @property
    def course_name(self) -> str:
        return self.value("course_name")

    @property
    def category(self) -> str:
        return self.value("category")

    @property
    def entities(self) -> tuple[str, ...]:
        """Every label that identifies this row, including category + grade relations."""
        values: list[str] = []
        for column, value in zip(self.columns, self.values, strict=True):
            for alias in _entity_aliases(column.field, value):
                if alias not in values:
                    values.append(alias)
        return tuple(values)

    @property
    def entity(self) -> str:
        return self.entities[0] if self.entities else ""

    @property
    def curriculum_row(self) -> bool:
        return self.reliable and not self.is_matrix

    def cell_text(self, field: str) -> str:
        for column, value in zip(self.columns, self.values, strict=True):
            if column.field == field:
                return value
        return ""


def _split_cell_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split only the row substring; returned offsets stay absolute in article.text."""
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in re.finditer(r"[|｜]", text[start:end]):
        spans.append(_trim_span(text, cursor, start + match.start()))
        cursor = start + match.end()
    spans.append(_trim_span(text, cursor, end))
    return spans


def _covered_origin_ok(
    row_index: int | None,
    index: int,
    cell: CellProvenance,
    cells_by_col: dict[int, CellProvenance],
) -> bool:
    """合并格占位必须能回溯到真实原点，且原点不能位于右侧或未来行。

    同 row 的 colspan 位置用本行 provenance 校验原点存在与跨度覆盖；
    更早行的纵向合并只保留可追溯上下文，不构成本行数字事实。
    """
    if row_index is None:
        return False
    origin_row = cell.origin_row
    origin_col = cell.origin_col
    if origin_row < 0 or origin_col < 0:
        return False
    if origin_row > row_index or origin_col > index:
        return False
    if origin_row < row_index:
        return True
    if origin_col >= index:
        return False
    origin = cells_by_col.get(origin_col)
    if origin is None or origin.covered:
        return False
    if origin.origin_row != origin_row or origin.origin_col != origin_col:
        return False
    if origin.rowspan < 1 or origin.colspan < 1:
        return False
    return origin.origin_col + origin.colspan > index


def _covered_value_ok(
    article: Article,
    row_index: int | None,
    index: int,
    cell: CellProvenance,
    value: str,
) -> bool:
    """covered 列位只能留空，或保留可追溯的纵向继承标签作为上下文。

    横向 colspan 的空占位必须保持为空；篡改出来的新值即使没有写进
    cell.text，也会出现在 raw row_cells 里。纵向合并的标签可能被
    join_row_texts 写回行文本，但它不是本行事实，且不能是数字。
    """
    raw = _normalize_structured_text(value)
    if not raw:
        return True
    if row_index is None:
        return False
    if cell.origin_row >= row_index or cell.origin_col != index:
        return False
    if re.fullmatch(r"[\d.,%/\-—–]+", raw):
        return False
    if not article.source_cell_ids:
        return True
    origin_id = f"{article.table_id or ''}#r{cell.origin_row}c{cell.origin_col}"
    return origin_id in article.source_cell_ids


def _has_native_cell_source(article: Article, cell: CellProvenance) -> bool:
    """Provenance proof without pretending a source flag exists.

    PDF cells carry text_source; HTML rows keep text_source=None but have a real
    DOM path and source cell id. An empty cell is handled before this check.
    """
    if cell.text_source:
        return True
    if cell.cell_id and cell.dom_path:
        return True
    return bool(
        cell.cell_id and article.dom_path and cell.cell_id in article.source_cell_ids
    )


def structured_table_row(article: Article) -> StructuredTableRow | None:
    """Parse a new-ingestion table row; return None for legacy/flat fragments.

    可靠性是逐项证据校验的结果：完整网格、span/origin、text_source、needs_review、
    header_paths/正文表头一致、行列数一致。任一项不满足就保留原文但拒绝列语义。
    """
    if article.block_kind not in _STRUCTURED_ROW_KINDS:
        return None
    problems: list[str] = []
    table_id = (article.table_id or "").strip()
    row_id = (article.row_id or "").strip()
    row_index = article.row_index
    headers = tuple(header.strip() for header in article.table_headers)
    paths = tuple(tuple(part.strip() for part in path) for path in article.header_paths)
    if not table_id:
        problems.append("MISSING_TABLE_ID")
    if not row_id:
        problems.append("MISSING_ROW_ID")
    if row_index is None:
        problems.append("MISSING_ROW_INDEX")
    if not headers:
        problems.append("MISSING_TABLE_HEADERS")
    if not paths:
        problems.append("MISSING_HEADER_PATHS")
    if headers and paths and len(headers) != len(paths):
        problems.append("HEADER_LENGTH_MISMATCH")
    column_count = max(len(headers), len(paths))
    for index in range(column_count):
        path = paths[index] if index < len(paths) else ()
        header = headers[index] if index < len(headers) else ""
        if any(not part for part in path):
            problems.append(f"EMPTY_HEADER_PATH:{index}")
        if len(path) > 3:
            problems.append(f"HEADER_PATH_TOO_DEEP:{index}")
        if _normalize_structured_text(" > ".join(path)) != _normalize_structured_text(
            header
        ):
            problems.append(f"HEADER_PATH_MISMATCH:{index}")
    problems.extend(_binding_problems(article, headers, paths, column_count))

    header_match = _STRUCTURED_HEADER_RE.search(article.text)
    row_match = _STRUCTURED_ROW_RE.search(
        article.text, header_match.end() if header_match is not None else 0
    )
    header_quote = ""
    row_quote = ""
    row_cells: list[str] = []
    cell_spans: list[tuple[int, int]] = []
    if header_match is None:
        problems.append("MISSING_HEADER_LINE")
    else:
        header_end = row_match.start() if row_match is not None else len(article.text)
        header_quote = article.text[header_match.start() : header_end].strip()
        raw_headers = [
            cell.strip()
            for cell in re.split(r"[|｜]", article.text[header_match.end() : header_end])
        ]
        if headers and (
            len(raw_headers) != len(headers)
            or any(
                _normalize_structured_text(raw) != _normalize_structured_text(expected)
                for raw, expected in zip(raw_headers, headers, strict=False)
            )
        ):
            problems.append("HEADER_LINE_MISMATCH")
    row_end = len(article.text.rstrip())
    if row_match is None:
        problems.append("MISSING_ROW_LINE")
    else:
        content_start = row_match.end()
        row_quote = article.text[row_match.start() : row_end]
        row_cells = [
            cell.strip()
            for cell in re.split(r"[|｜]", article.text[content_start:row_end])
        ]
        cell_spans = _split_cell_spans(article.text, content_start, row_end)
        if headers and len(row_cells) != len(headers):
            problems.append("ROW_LENGTH_MISMATCH")

    cells_by_col: dict[int, CellProvenance] = {}
    for cell in article.cell_provenance:
        if cell.row == row_index:
            cells_by_col.setdefault(cell.col, cell)
    values: list[str] = []
    # 纵向继承标签只作为上下文显示，不能作为本行 raw 值参与事实核对。
    raw_row_cells = list(row_cells)
    for index in range(column_count):
        cell = cells_by_col.get(index)
        value = row_cells[index] if index < len(row_cells) else ""
        if cell is None:
            problems.append(f"MISSING_CELL:{index}")
            values.append("")
            continue
        if cell.is_header:
            problems.append(f"HEADER_CELL_IN_ROW:{index}")
        if cell.covered:
            # 合并格的重复值不属于本行自身事实，只保留列位；covered 必须为空占位。
            if _normalize_structured_text(cell.text):
                problems.append(f"COVERED_CELL_HAS_TEXT:{index}")
            if not _covered_origin_ok(row_index, index, cell, cells_by_col):
                problems.append(f"COVERED_ORIGIN_MISMATCH:{index}")
            if not _covered_value_ok(article, row_index, index, cell, value):
                problems.append(f"COVERED_CELL_HAS_VALUE:{index}")
            if index < len(raw_row_cells):
                raw_row_cells[index] = ""
            values.append("")
            continue
        if cell.origin_row != row_index or cell.origin_col != index:
            problems.append(f"CELL_ORIGIN_MISMATCH:{index}")
        if _normalize_structured_text(cell.text) != _normalize_structured_text(value):
            problems.append(f"CELL_TEXT_MISMATCH:{index}")
        if not value:
            # 空属性没有事实可提供；文本来源缺失不应因此否掉整行。
            values.append("")
            continue
        if cell.needs_review:
            problems.append(f"NEEDS_REVIEW:{index}")
        if not _has_native_cell_source(article, cell):
            problems.append(f"MISSING_TEXT_SOURCE:{index}")
        values.append(value)

    bindings_present = bool(article.table_column_bindings)
    binding_by_target = {
        binding.target_col: binding for binding in article.table_column_bindings
    }
    fields = tuple(
        _structured_binding_field(
            paths[index] if index < len(paths) else (),
            binding_by_target.get(index),
            bindings_present=bindings_present,
        )
        for index in range(column_count)
    )
    seen_fields: set[str] = set()
    for column_field in fields:
        if column_field == "other":
            continue
        if column_field in seen_fields and column_field not in _REPEATABLE_STRUCTURED_FIELDS:
            # Repeated category/nature labels are context, not numeric facts.
            problems.append(f"DUPLICATE_FIELD:{column_field}")
        seen_fields.add(column_field)

    columns = tuple(
        StructuredColumn(
            index,
            paths[index] if index < len(paths) else (),
            headers[index] if index < len(headers) else "",
            fields[index],
        )
        for index in range(column_count)
    )
    matrix = article.block_kind == "matrix_row" or any(
        key in " ".join(headers) for key in _MATRIX_KEYS
    )
    return StructuredTableRow(
        table_id=table_id,
        row_id=row_id,
        row_index=row_index,
        columns=columns,
        values=tuple(values),
        quote=row_quote,
        quote_start=row_match.start() if row_match else 0,
        quote_end=row_end if row_match else 0,
        header_quote=header_quote,
        is_matrix=matrix,
        reliable=not problems,
        problems=tuple(dict.fromkeys(problems)),
        cell_spans=tuple(cell_spans),
        raw_values=tuple(raw_row_cells),
    )


@dataclass(frozen=True)
class SourceSpan:
    span_id: str
    article_id: str
    part_no: int
    start: int
    end: int
    quote: str
    label: str
    module: str = ""
    headers: tuple[str, ...] = ()
    semester: str = ""
    credits: str = ""
    hours: str = ""
    is_matrix: bool = False
    # Cross-fragment table header: a separate original quote, never concatenated.
    header_article_id: str = ""
    header_part_no: int | None = None
    header_quote: str = ""
    # False when a flattened multi-level header cannot be mapped positionally.
    header_reliable: bool = True
    weeks: str = ""
    weekly_hours: str = ""
    # New-ingestion row: auditable table identity and real column paths.
    structured: bool = False
    curriculum_row: bool = False
    table_id: str = ""
    row_id: str = ""
    row_index: int | None = None
    column_values: tuple[tuple[str, str], ...] = ()
    structure_problems: tuple[str, ...] = ()


def _row_cells(row_text: str, headers: tuple[str, ...]) -> list[str]:
    lines = [line for line in row_text.splitlines() if line.strip()]
    if not lines:
        return []
    cells = _cells(lines[0])
    if len(cells) < len(headers) and len(lines) > 1:
        continuation = _cells(lines[1])
        # Flattened PDF rows often repeat a name cell at the start of the
        # continuation line; drop it only when the first line stopped after the
        # course name, so numeric columns stay aligned with the header.
        if len(cells) == 2 and continuation and not re.search(r"\d", continuation[0]):
            continuation = continuation[1:]
        cells = [*cells, *continuation]
    return cells


def _aligned_headers(
    headers: tuple[str, ...], cells: list[str]
) -> tuple[str, ...]:
    """Row may omit an optional leading category column (row starts with course code)."""
    if (
        len(headers) == len(cells) + 1
        and cells
        and _COURSE_CODE_RE.fullmatch(cells[0])
        and len(headers) > 1
        and "课程代码" in headers[1]
    ):
        return headers[1:]
    return headers


def _row_fields(
    row_text: str, headers: tuple[str, ...], *, reliable: bool = True
) -> dict[str, str]:
    if not headers:
        return {}
    fields: dict[str, str] = {}
    cells = _row_cells(row_text, headers)
    if reliable and len(cells) >= 2:
        aligned = _aligned_headers(headers, cells)
        for index, header in enumerate(aligned):
            if index >= len(cells):
                break
            value = cells[index]
            if not value:
                continue
            if "周学时" in header:
                fields.setdefault("weekly_hours", value)
            elif "学分" in header:
                fields.setdefault("credits", value)
            elif "学时" in header and not any(
                mark in header for mark in ("理论", "实验", "实习", "实践", "上机")
            ):
                fields.setdefault("hours", value)
            elif "周数" in header or header.strip() == "周":
                fields.setdefault("weeks", value)
            elif "学期" in header:
                fields.setdefault("semester", value)
            elif "课程性质" in header or "考核" in header:
                fields.setdefault("nature", value)
    # Explicitly labelled values stay usable even when column alignment is not;
    # bare numbers are never assigned to a field.
    if "semester" not in fields:
        match = _SEMESTER_RE.search(row_text)
        if match:
            fields["semester"] = match.group(0).strip()
    if "weeks" not in fields:
        match = _WEEKS_RE.search(row_text)
        if match:
            fields["weeks"] = match.group(1)
    return fields


def source_spans(evidence: list[Evidence]) -> dict[str, SourceSpan]:
    result = {}
    external_headers = _external_headers(evidence)
    for index, item in enumerate(evidence, 1):
        article = item.article
        text = article.text
        prefix = f"source_{index}"
        structured = structured_table_row(article)
        result[prefix + "_body"] = SourceSpan(
            prefix + "_body",
            article.article_id,
            article.part_no,
            0,
            len(text),
            text,
            "完整原始片段",
            structured=structured is not None,
            curriculum_row=structured.curriculum_row if structured else False,
            table_id=structured.table_id if structured else "",
            row_id=structured.row_id if structured else "",
            row_index=structured.row_index if structured else None,
            header_reliable=structured.reliable if structured else True,
            structure_problems=structured.problems if structured else (),
        )
        if structured is not None:
            # 新ingestion行：列语义只来自 header_paths，quote 仍是同一正文的连续片段。
            fields: dict[str, str] = {}
            column_values: tuple[tuple[str, str], ...] = ()
            if structured.reliable and structured.curriculum_row:
                fields = {
                    column.field: value
                    for column, value in zip(structured.columns, structured.values, strict=True)
                    if value
                }
                column_values = tuple(
                    (column.text, value)
                    for column, value in zip(structured.columns, structured.values, strict=True)
                    if column.text and value
                )
            category = structured.category if structured.reliable else ""
            module = category if re.search(r"模块|方向", category) else ""
            label = structured.course_name or structured.course_code or article.article_no
            span_id = f"{prefix}_row_1"
            result[span_id] = SourceSpan(
                span_id,
                article.article_id,
                article.part_no,
                structured.quote_start if structured.quote else 0,
                structured.quote_end if structured.quote else len(text),
                structured.quote or text,
                label,
                module,
                headers=structured.headers,
                semester=fields.get("semester", ""),
                credits=fields.get("credits", ""),
                hours=fields.get("total_hours", ""),
                is_matrix=structured.is_matrix,
                header_quote=structured.header_quote,
                header_reliable=structured.reliable,
                weeks=fields.get("weeks", ""),
                weekly_hours=fields.get("weekly_hours", ""),
                structured=True,
                curriculum_row=structured.curriculum_row,
                table_id=structured.table_id,
                row_id=structured.row_id,
                row_index=structured.row_index,
                column_values=column_values,
                structure_problems=structured.problems,
            )
            continue
        codes = list(
            re.finditer(
                r"(?<!\d)(?:\d{14}|\d{6})(?!\d)\s*[|｜]\s*([^|｜\n]+)",
                text,
            )
        )
        for row, match in enumerate(codes, 1):
            end = codes[row].start() if row < len(codes) else len(text)
            # A row span ends before the next section/module, never inherits it.
            boundary = re.search(
                r"(?m)^([^\n|｜]{1,35}(?:模块|方向|实践))\s*[|｜]", text[match.end() : end]
            )
            if boundary:
                end = match.end() + boundary.start()
            before = text[: match.start()]
            modules = list(re.finditer(r"(?m)([^\n|｜]{1,30}(?:模块|方向))\s*[|｜]", before))
            module = modules[-1][1].strip() if modules else ""
            span_id = f"{prefix}_row_{row}"
            headers, header_line = table_header(text, match.start())
            header_article_id = ""
            header_part_no: int | None = None
            header_quote = ""
            if not headers:
                external = _external_header(
                    external_headers, article.article_id, article.part_no
                )
                if external is not None:
                    header_part_no, headers, header_quote = external
                    header_article_id = article.article_id
                    header_line = header_quote
            row_text = text[match.start() : end]
            header_reliable = _header_reliable(header_line) if headers else True
            fields = _row_fields(row_text, headers, reliable=header_reliable)
            # 支撑矩阵里的课程只说明“该课支撑哪些毕业要求”，不是培养方案进程表，
            # 不能当作“开设课程/学分”的来源（报告 020）。表头在上一片段时同样生效。
            matrix = any(key in header_line for key in _MATRIX_KEYS) or any(
                key in article.article_no + article.doc_title for key in _MATRIX_KEYS
            )
            result[span_id] = SourceSpan(
                span_id,
                article.article_id,
                article.part_no,
                match.start(),
                end,
                row_text,
                match[1].strip(),
                module,
                headers=headers,
                semester=fields.get("semester", ""),
                credits=fields.get("credits", ""),
                hours=fields.get("hours", ""),
                is_matrix=matrix,
                header_article_id=header_article_id,
                header_part_no=header_part_no,
                header_quote=header_quote,
                header_reliable=header_reliable,
                weeks=fields.get("weeks", ""),
                weekly_hours=fields.get("weekly_hours", ""),
            )
    return result