"""引文 span 定位：把归一化匹配映射回原文偏移。

W2 只回答"quote 在 text_normalized 中可找到"；W3 回答"quote 在 article.text
的哪个位置"，供 UI 高亮与人工复核使用。
"""

from __future__ import annotations

from typing import Literal

from campusguide.domain.text import normalize_text, normalize_with_map

QuoteMatch = Literal["exact", "normalized"]


def locate_quote(text: str, quote: str) -> tuple[int, int, QuoteMatch] | None:
    """在 ``text`` 中定位 ``quote``，返回 ``(start, end, match_type)``。

    先尝试精确子串；失败后对归一化文本查找，再映射回原 span。
    找不到返回 ``None``，由调用方记为 ``QUOTE_MISMATCH``。
    """
    if not text or not quote:
        return None

    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote), "exact"

    normalized_text, mapping = normalize_with_map(text)
    normalized_quote = normalize_text(quote)
    if not normalized_quote:
        return None
    position = normalized_text.find(normalized_quote)
    if position < 0:
        return None
    end_position = position + len(normalized_quote) - 1
    if end_position >= len(mapping):
        return None
    start_original = mapping[position]
    end_original = mapping[end_position] + 1
    return start_original, end_original, "normalized"
