"""Literal source spans for tables/lists: model selects IDs, server copies originals."""

from __future__ import annotations

import re
from dataclasses import dataclass

from campusguide.domain.models import Evidence

_HEADER_KEYS = ("课程代码", "课程名称", "学分", "学时", "开课学期", "课程性质", "考核方式")
_MATRIX_KEYS = ("支撑", "矩阵", "培养目标与毕业要求")
_SEMESTER_RE = re.compile(r"第\s*[一二三四五六七八九十\d]+\s*学期|\d+\s*/\s*\d+|\d+\s*学期")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"[|｜]", line)]


def table_header(text: str, position: int) -> tuple[tuple[str, ...], str]:
    """Nearest preceding header row of a markdown-style table, if any."""
    lines = text[:position].splitlines()[-60:]
    for line in reversed(lines):
        if sum(1 for key in _HEADER_KEYS if key in line) >= 3 and re.search(r"[|｜]", line):
            header = tuple(cell for cell in _cells(line) if cell)
            if len(header) >= 3:
                return header, line
    return (), ""


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


def _row_fields(row_text: str, headers: tuple[str, ...]) -> dict[str, str]:
    if not headers:
        return {}
    cells = _cells(row_text.splitlines()[0])
    if len(cells) < 2:
        return {}
    fields: dict[str, str] = {}
    for index, header in enumerate(headers):
        if index >= len(cells):
            break
        value = cells[index]
        if not value:
            continue
        if "学分" in header:
            fields["credits"] = value
        elif "学时" in header:
            fields["hours"] = value
        elif "学期" in header:
            fields["semester"] = value
        elif "课程性质" in header or "考核" in header:
            fields.setdefault("nature", value)
    if "semester" not in fields:
        match = _SEMESTER_RE.search(row_text)
        if match:
            fields["semester"] = match.group(0).strip()
    return fields


def source_spans(evidence: list[Evidence]) -> dict[str, SourceSpan]:
    result = {}
    for index, item in enumerate(evidence, 1):
        article = item.article
        text = article.text
        prefix = f"source_{index}"
        result[prefix + "_body"] = SourceSpan(
            prefix + "_body",
            article.article_id,
            article.part_no,
            0,
            len(text),
            text,
            "完整原始片段",
        )
        codes = list(re.finditer(r"(?<!\d)\d{6}(?!\d)\s*[|｜]\s*([^|｜\n]+)", text))
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
            row_text = text[match.start() : end]
            fields = _row_fields(row_text, headers)
            # 支撑矩阵里的课程只说明“该课支撑哪些毕业要求”，不是培养方案进程表，
            # 不能当作“开设课程/学分”的来源（报告 020）。
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
            )
    return result