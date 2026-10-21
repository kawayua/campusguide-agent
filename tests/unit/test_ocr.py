from __future__ import annotations

from pathlib import Path

from campusguide.ingest.ocr import (
    OcrLine,
    OcrPage,
    _merge_ocr_pages,
    _reading_order,
    ocr_output_path,
)


def _box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def test_reading_order_sorts_same_row_left_to_right() -> None:
    raw = [
        [_box(300, 100, 400, 120), "右侧", 0.9],
        [_box(100, 100, 200, 120), "左侧", 0.95],
        [_box(100, 140, 400, 160), "下一行", 0.92],
    ]

    lines = _reading_order(raw)

    assert [line[0] for line in lines] == ["左侧 右侧", "下一行"]
    assert lines[0][1] == 0.925


def test_reading_order_keeps_adjacent_lines_separate() -> None:
    """相邻文本行即使间距很小也不能被合并。"""
    raw = [
        [_box(100, 100, 500, 121), "第一行正文", 0.95],
        [_box(100, 126, 500, 147), "第二行正文", 0.95],
    ]

    lines = _reading_order(raw)

    assert [line[0] for line in lines] == ["第一行正文", "第二行正文"]


def test_reading_order_merges_overlapping_table_cells() -> None:
    raw = [
        [_box(100, 100, 180, 140), "课程", 0.9],
        [_box(200, 104, 260, 120), "学分", 0.9],
        [_box(280, 100, 360, 140), "2.0", 0.9],
    ]

    lines = _reading_order(raw)

    assert [line[0] for line in lines] == ["课程 学分 2.0"]


def test_ocr_output_path(tmp_path: Path) -> None:
    path = ocr_output_path(tmp_path, "grad-cs-plan", "v2020")

    assert path == tmp_path / "grad-cs-plan" / "v2020.json"


def _make_page(
    rows: list[tuple[float, str]],
    *,
    dpi: int,
    height: int = 1000,
) -> OcrPage:
    lines = [
        OcrLine(
            text=text,
            confidence=0.9,
            box=_box(100, top - 10, 500, top + 10),
        )
        for top, text in rows
    ]
    return OcrPage(
        page_no=1,
        text="\n".join(text for _, text in rows),
        lines=lines,
        mean_confidence=0.9,
        dpi=dpi,
        height=height,
        width=700,
        content_chars=sum(len(text) for _, text in rows),
    )


def test_merge_pages_grafts_line_missing_from_base() -> None:
    base = _make_page(
        [
            (100, "第一行底稿"),
            (300, "第三行底稿"),
            (500, "第四行底稿"),
            (700, "第五行底稿独有而且这段刻意写长以保证字符覆盖最多"),
        ],
        dpi=200,
    )
    supplement = _make_page(
        [
            (100, "第一行底稿"),
            (200, "第二行只在另一 pass 出现"),
            (300, "第三行底稿"),
            (303, "第三行的识别变体"),
            (500, "第四行底稿"),
        ],
        dpi=240,
    )

    merged = _merge_ocr_pages([base, supplement])

    assert [line.text for line in merged.lines] == [
        "第一行底稿",
        "第二行只在另一 pass 出现",
        "第三行底稿",
        "第四行底稿",
        "第五行底稿独有而且这段刻意写长以保证字符覆盖最多",
    ]
    assert merged.grafted_lines == 1
    assert merged.dpi == 200


def test_merge_pages_keeps_base_when_no_line_is_missing() -> None:
    base = _make_page([(100, "第一行"), (300, "第二行")], dpi=240)
    supplement = _make_page([(100, "第一行"), (300, "第二行")], dpi=200)

    merged = _merge_ocr_pages([base, supplement])

    assert [line.text for line in merged.lines] == ["第一行", "第二行"]
    assert merged.grafted_lines == 0
