"""W3 引文 span：归一化索引映射与原文偏移定位。"""

from __future__ import annotations

import pytest

from campusguide.domain.text import normalize_text, normalize_with_map
from campusguide.verification.quotes import locate_quote


def test_normalize_with_map_matches_normalize_text_and_indexes_original() -> None:
    text = "　【第一条】\u200b“学习成绩”　优异。\n"
    normalized, index_map = normalize_with_map(text)

    assert normalized == normalize_text(text)
    assert len(normalized) == len(index_map)
    assert all(0 <= index < len(text) for index in index_map)
    assert normalized == '[第一条]"学习成绩"优异。'


def test_locate_quote_prefers_exact_match() -> None:
    text = "第一条 申请条件：学习成绩优异。"
    span = locate_quote(text, "学习成绩优异")

    assert span is not None
    assert span.match == "exact"
    assert text[span.start : span.end] == "学习成绩优异"


def test_locate_quote_tolerates_whitespace_and_newlines() -> None:
    text = "申请条件：\n（一）成绩优异；\n（二）品行端正。"
    span = locate_quote(text, "（一）成绩优异；（二）品行端正")

    assert span is not None
    assert span.match == "normalized"
    assert normalize_text(text[span.start : span.end]) == normalize_text(
        "（一）成绩优异；（二）品行端正"
    )


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("【第一条】学习成绩优异。", "[第一条]学习成绩优异"),
        ("“学习成绩”优异。", '"学习成绩"优异'),
        ("２０２５年９月１日实施。", "2025年9月1日实施"),
        ("第一条　学习成绩优异。", "第一条 学习成绩优异"),
        ("学\u200b习\u200c成\u200d绩优异。", "学习成绩优异"),
    ],
)
def test_locate_quote_normalizes_fullwidth_quotes_zero_width(text: str, quote: str) -> None:
    span = locate_quote(text, quote)

    assert span is not None
    assert span.match in {"exact", "normalized"}
    assert normalize_text(text[span.start : span.end]) == normalize_text(quote)


def test_locate_quote_returns_none_for_missing_or_empty() -> None:
    assert locate_quote("学习成绩优异。", "品行端正") is None
    assert locate_quote("学习成绩优异。", "") is None
    assert locate_quote("学习成绩优异。", "   \n") is None


def test_locate_quote_offsets_are_relative_to_original_text() -> None:
    prefix = "前言\n"
    text = f"{prefix}第一条 学习成绩优异。"
    span = locate_quote(text, "学习成绩优异")

    assert span is not None
    assert span.start == len(prefix) + len("第一条 ")
    assert text[span.start : span.end] == "学习成绩优异"
