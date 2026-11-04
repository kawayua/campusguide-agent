"""PDF 拆分表格恢复：候选必须保留完整表头、备注列和原文数字。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from campusguide.ingest.parse import _pdf_page_tables


class _FakeRect:
    x0 = 0.0
    y0 = 0.0
    x1 = 595.0
    y1 = 842.0


class _FakeTable:
    def __init__(
        self,
        rows: list[list[str | None]],
        bbox: list[float],
        *,
        header_rows: int = 1,
        col_count: int | None = None,
    ) -> None:
        self._rows = [list(row) for row in rows]
        self.bbox = bbox
        self.header_rows = header_rows
        self.col_count = col_count or max(len(row) for row in rows)
        self.row_count = len(rows)

    def extract(self) -> list[list[str | None]]:
        return [list(row) for row in self._rows]


class _FakePage:
    rect = _FakeRect()

    def __init__(self, defaults: list[_FakeTable], candidates: list[_FakeTable]) -> None:
        self._defaults = defaults
        self._candidates = candidates
        self.calls: list[dict[str, Any]] = []

    def find_tables(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        if "clip" in kwargs:
            return SimpleNamespace(tables=self._candidates)
        return SimpleNamespace(tables=self._defaults)


def _default_split_table() -> _FakeTable:
    return _FakeTable(
        [
            [
                "课程类别",
                "",
                "课程编号",
                "",
                "",
                "课程中文名称",
                "",
                "学分",
                "",
                "",
                "开课学期",
                "",
                "",
                "硕士",
                "",
                "",
                "备注",
            ],
            [
                "学位课－公共必修课（6学分）",
                "",
                "19021000000004",
                "",
                "",
                "新时代中国特色社会主义理论与实践",
                "",
                "2",
                "",
                "",
                "秋",
                "",
                "",
                "必修",
                "",
                "",
                "",
            ],
            [
                "",
                "",
                "19021000000002",
                "",
                "",
                "马克思主义与社会科学方法论",
                "",
                "1",
                "",
                "",
                "春/秋",
                "",
                "",
                "必修",
                "",
                "",
                "二选一",
            ],
        ],
        bbox=[72.48, 439.71, 496.71, 738.96],
        col_count=17,
    )


def _course_table(*, with_note: bool) -> _FakeTable:
    header = [
        "课程类别",
        "课程编号",
        "课程中文名称",
        "学分",
        "开课学期",
        "硕士",
    ]
    first = [
        "学位课－公共必修课（6学分）",
        "19021000000004",
        "新时代中国特色社会主义理论与实践",
        "2",
        "秋",
        "必修",
    ]
    second = [
        "",
        "19021000000002",
        "马克思主义与社会科学方法论",
        "1",
        "春/秋",
        "必修",
    ]
    if with_note:
        header.append("备注")
        first.append("")
        second.append("二选一")
    return _FakeTable(
        [header, first, second],
        bbox=[66.94, 435.84, 510.50, 748.35],
    )


def _tight_table() -> _FakeTable:
    return _FakeTable(
        [
            ["19021000000004", "新时代中国特色社会主义理论与实践", "2", "秋", "必修"],
            ["19021000000002", "马克思主义与社会科学方法论", "1", "春/秋", "必修"],
        ],
        bbox=[72.48, 439.71, 496.71, 738.96],
    )


def test_recovers_table_with_complete_headers_and_note_column() -> None:
    default = _default_split_table()
    incomplete_six = _course_table(with_note=False)
    tight = _tight_table()
    complete = _course_table(with_note=True)
    page = _FakePage([default], [incomplete_six, tight, complete])

    selected = _pdf_page_tables(
        page,
        page_no=6,
        table_recovery=True,
        padding_ratio=0.08,
    )

    assert len(selected) == 1
    chosen, warnings = selected[0]
    assert chosen is complete
    assert chosen.extract()[0][-1] == "备注"
    assert chosen.extract()[1][2] == "新时代中国特色社会主义理论与实践"
    assert chosen.extract()[1][3] == "2"
    assert any("重新提取" in warning for warning in warnings)
    assert any("7 列" in warning for warning in warnings)
    assert any("clip" in call for call in page.calls)
    assert any(
        call.get("use_layout") is False and call.get("strategy") == "lines_strict"
        for call in page.calls
    )


def test_clean_header_table_is_not_reextracted() -> None:
    table = _FakeTable(
        [
            ["课程名称", "学分"],
            ["软件工程实践", "2"],
        ],
        bbox=[100.0, 100.0, 300.0, 180.0],
    )
    page = _FakePage([table], [])

    selected = _pdf_page_tables(
        page,
        page_no=1,
        table_recovery=True,
        padding_ratio=0.08,
    )

    assert selected[0][0] is table
    assert selected[0][1] == []
    assert page.calls == [{}]


def test_legacy_page_without_use_layout_kwarg_degrades_gracefully() -> None:
    default = _default_split_table()

    class _LegacyPage(_FakePage):
        def find_tables(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(dict(kwargs))
            if "clip" not in kwargs:
                return SimpleNamespace(tables=self._defaults)
            if "use_layout" in kwargs or "strategy" in kwargs:
                raise TypeError("unexpected keyword argument")
            return SimpleNamespace(tables=[])

    page = _LegacyPage([default], [])
    selected = _pdf_page_tables(
        page,
        page_no=1,
        table_recovery=True,
        padding_ratio=0.08,
    )

    assert selected[0][0] is default
    assert any("use_layout" in call for call in page.calls)
    assert any(set(call) == {"clip"} for call in page.calls)
