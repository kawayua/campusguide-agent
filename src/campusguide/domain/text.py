"""文本归一化与轻量清洗，供切分、引用定位和查询规范化共用。"""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\ufeff")
# 部分网页/WPS 导出会在段首插入私用区字形作为列表符号，文本层没有语义。
_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
_WHITESPACE_RE = re.compile(r"\s")
_QUOTE_MAP = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "【": "[",
        "】": "]",
        "　": " ",
    }
)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """归一化文本，同时记录每个归一化字符对应的原文索引。

    W3 起 ``normalize_text`` 也走同一套逐字符变换，保证 quote 定位与
    既有归一化口径完全一致。返回的索引是 ``text`` 的原文偏移，可直接
    写回 ``Citation.quote_start/quote_end``。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        if char in _ZERO_WIDTH_CHARS:
            continue
        piece = unicodedata.normalize("NFKC", char).translate(_QUOTE_MAP)
        piece = piece.replace("\u00a0", " ")
        for out_char in piece:
            if _WHITESPACE_RE.fullmatch(out_char):
                continue
            chars.append(out_char)
            index_map.append(index)
    return "".join(chars), index_map


def normalize_text(text: str) -> str:
    """去掉空白并统一标点，用于 quote 包含关系与去重判断。

    只做确定性、可逆性可控的变换；不删除中文标点，避免改变语义。
    """
    return normalize_with_map(text)[0]


def clean_whitespace(text: str) -> str:
    """保留段落结构，清理行内多余空白与过多空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = _PRIVATE_USE_RE.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def count_cjk(text: str) -> int:
    """统计 CJK 字符数量，用于判断 HTML 正文区域是否有实质内容。"""
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def is_mostly_empty(text: str) -> bool:
    return count_cjk(text) < 20 and len(normalize_text(text)) < 80
