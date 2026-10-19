"""文本归一化与轻量清洗，供切分、引用定位和查询规范化共用。"""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
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


def normalize_text(text: str) -> str:
    """去掉空白并统一标点，用于 quote 包含关系与去重判断。

    只做确定性、可逆性可控的变换；不删除中文标点，避免改变语义。
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH).translate(_QUOTE_MAP)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", "", text)


def clean_whitespace(text: str) -> str:
    """保留段落结构，清理行内多余空白与过多空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def count_cjk(text: str) -> int:
    """统计 CJK 字符数量，用于判断 HTML 正文区域是否有实质内容。"""
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def is_mostly_empty(text: str) -> bool:
    return count_cjk(text) < 20 and len(normalize_text(text)) < 80
