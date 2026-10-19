from __future__ import annotations

import pytest

from campusguide.domain.ids import (
    article_index_from_no,
    chinese_to_int,
    make_article_id,
    parse_article_id,
    parse_article_no,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("一", 1),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("二十三", 23),
        ("一百", 100),
        ("一百零一", 101),
        ("一百一十", 110),
        ("两百零五", 205),
        ("1001", 1001),
    ],
)
def test_chinese_to_int(value: str, expected: int) -> None:
    assert chinese_to_int(value) == expected


def test_chinese_to_int_rejects_unknown() -> None:
    assert chinese_to_int("abc") is None
    assert chinese_to_int("") is None


def test_parse_article_no_with_sub_clause() -> None:
    parsed = parse_article_no("第十二条之一")
    assert parsed is not None
    assert parsed.number == 12
    assert parsed.sub == 1
    assert article_index_from_no("第十二条") == 12


def test_article_id_roundtrip_and_duplicate_suffix() -> None:
    article_id = make_article_id("status-rules", "v2025", "第九条")
    assert article_id == "status-rules@v2025#第九条"
    assert parse_article_id(article_id) == ("status-rules", "v2025", "第九条")

    duplicate = make_article_id("status-rules", "v2025", "第九条", duplicate=2)
    assert duplicate.endswith("~2")
    assert duplicate != article_id


def test_parse_article_id_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        parse_article_id("not-an-article-id")
