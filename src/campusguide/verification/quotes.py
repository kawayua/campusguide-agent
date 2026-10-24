"""引文定位：把 quote 映射回 ``article.text`` 的原文 span。

W3 v2 要求 ``Citation.quote_start/quote_end`` 表示原文偏移，而不是
归一化文本偏移。先做 exact 查找；失败后按 ``normalize_text`` 的同一套
NFKC/零宽/引号/括号/全角空格变换查找，再通过索引映射回原文。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from campusguide.domain.text import normalize_text, normalize_with_map

__all__ = ["QuoteSpan", "locate_quote", "normalize_text", "normalize_with_map"]

MatchKind = Literal["exact", "normalized"]


class QuoteSpan(NamedTuple):
    start: int
    end: int
    match: MatchKind


def locate_quote(text: str, quote: str) -> QuoteSpan | None:
    """返回 quote 在 text 中的原文半开区间；空 quote 或找不到返回 None。"""
    if not text or not quote:
        return None
    cleaned = quote.strip()
    if not cleaned:
        return None

    exact = text.find(cleaned)
    if exact >= 0:
        return QuoteSpan(exact, exact + len(cleaned), "exact")

    normalized_quote = normalize_text(cleaned)
    if not normalized_quote:
        return None
    normalized_text, index_map = normalize_with_map(text)
    position = normalized_text.find(normalized_quote)
    if position < 0:
        return None
    start = index_map[position]
    end = index_map[position + len(normalized_quote) - 1] + 1
    return QuoteSpan(start, end, "normalized")
