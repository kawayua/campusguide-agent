"""查询规范化（W1 只做确定性清洗，别名扩展在 W2 加入）。"""

from __future__ import annotations

import unicodedata

from campusguide.domain.text import normalize_text

__all__ = ["normalize_query", "normalize_text"]


def normalize_query(query: str) -> str:
    """统一全半角、去首尾空白、压缩内部空白；保留原始大小写语义。"""
    query = unicodedata.normalize("NFKC", query).replace("\u00a0", " ")
    return " ".join(query.split())
