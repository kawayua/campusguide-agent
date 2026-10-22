"""ID 生成与解析，以及中文条款序号转换。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000}

ARTICLE_NO_RE = re.compile(
    r"^第(?P<number>[一二三四五六七八九十百千零〇两0-9]+)条(?:之(?P<sub>[一二三四五六七八九十0-9]+))?$"
)


def chinese_to_int(value: str) -> int | None:
    """把“十二”“一百零一”“23”等转成整数；无法解析时返回 None。"""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if "万" in value:
        return None

    total = 0
    number = 0
    for char in value:
        if char in _DIGITS:
            number = _DIGITS[char]
        elif char in _UNITS:
            if number == 0:
                number = 1
            total += number * _UNITS[char]
            number = 0
        else:
            return None
    return total + number


@dataclass(frozen=True)
class ArticleNumber:
    """解析后的条款号，例如“第十二条之一”。"""

    raw: str
    number: int
    sub: int | None = None


def parse_article_no(article_no: str) -> ArticleNumber | None:
    match = ARTICLE_NO_RE.match(article_no.strip())
    if not match:
        return None
    number = chinese_to_int(match.group("number"))
    if number is None:
        return None
    sub_raw = match.group("sub")
    sub = chinese_to_int(sub_raw) if sub_raw is not None else None
    return ArticleNumber(raw=article_no.strip(), number=number, sub=sub)


def article_index_from_no(article_no: str) -> int:
    """返回条款的规范化序号；无法解析时返回 0（并应在上游记录告警）。"""
    parsed = parse_article_no(article_no)
    return parsed.number if parsed else 0


def make_article_id(
    doc_id: str,
    version_id: str,
    article_no: str,
    *,
    duplicate: int = 1,
) -> str:
    """生成 {doc_id}@{version_id}#{article_no}；重复条款号追加 ~N 保证唯一。"""
    base = f"{doc_id}@{version_id}#{article_no}"
    return base if duplicate <= 1 else f"{base}~{duplicate}"


def parse_article_id(article_id: str) -> tuple[str, str, str]:
    """拆解 article_id；格式非法时抛出 ValueError。"""
    try:
        left, article_no = article_id.split("#", 1)
        doc_id, version_id = left.split("@", 1)
    except ValueError as exc:  # pragma: no cover - 明确错误路径
        raise ValueError(f"非法 article_id: {article_id!r}") from exc
    if not doc_id or not version_id or not article_no:
        raise ValueError(f"非法 article_id: {article_id!r}")
    return doc_id, version_id, article_no
