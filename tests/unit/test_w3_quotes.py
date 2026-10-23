"""W3 引文 span 定位测试：normalize_with_map 与 locate_quote。"""

from __future__ import annotations

from campusguide.domain.text import normalize_text, normalize_with_map
from campusguide.verification.quotes import locate_quote


def test_normalize_with_map_is_consistent_with_normalize_text() -> None:
    text = "申请条件：\n（一）成绩优异；\n（二）品行端正。"
    normalized, mapping = normalize_with_map(text)
    assert normalized == normalize_text(text)
    assert len(mapping) == len(normalized)


def test_normalize_with_map_handles_fullwidth_and_quotes() -> None:
    text = "“成绩”优异：ＡＢ１２"
    normalized, mapping = normalize_with_map(text)
    assert normalized == normalize_text(text)
    # 全角引号归一为半角后，映射仍指回原引号字符。
    quote_pos = normalized.find('"')
    assert text[mapping[quote_pos]] == "“"


def test_normalize_with_map_deleted_whitespace_maps_to_neighbor() -> None:
    text = "成绩 优异"
    normalized, mapping = normalize_with_map(text)
    assert normalized == "成绩优异"
    # 中间被删除的空格不占位置："异"仍映射到原文最后的"异"。
    assert text[mapping[-1]] == "异"
    assert len(mapping) == 4


def test_normalize_with_map_zero_width_does_not_shift_mapping() -> None:
    text = "成绩\u200b优异"
    normalized, mapping = normalize_with_map(text)
    assert normalized == "成绩优异"
    assert len(mapping) == len(normalized)
    # 原文 span 天然包含中间被删除的零宽字符，但首尾必须对齐
    hit = locate_quote(text, "成绩优异")
    assert hit is not None
    start, end, match = hit
    assert match == "normalized"
    assert (start, end) == (0, 5)
    assert text[start:end].replace("\u200b", "") == "成绩优异"


def test_normalize_with_map_leading_zero_width_keeps_indices() -> None:
    text = "\u200b成绩"
    _, mapping = normalize_with_map(text)
    assert mapping == [1, 2]


def test_locate_quote_exact() -> None:
    text = "国家奖学金申请条件：二年级及以上，成绩排名前10%。"
    hit = locate_quote(text, "成绩排名前10%")
    assert hit is not None
    start, end, match = hit
    assert match == "exact"
    assert text[start:end] == "成绩排名前10%"


def test_locate_quote_normalized_maps_to_original_span() -> None:
    text = "申请条件：\n（一）热爱祖国；\n（二）品行端正，无违纪记录。"
    hit = locate_quote(text, "（一）热爱祖国；（二）品行端正")
    assert hit is not None
    start, end, match = hit
    assert match == "normalized"
    assert text[start:end] == "（一）热爱祖国；\n（二）品行端正"


def test_locate_quote_fullwidth_halfwidth() -> None:
    text = "奖励标准为每人８０００元。"
    hit = locate_quote(text, "每人8000元")
    assert hit is not None
    start, end, match = hit
    assert match == "normalized"
    assert text[start:end] == "每人８０００元"


def test_locate_quote_not_found_returns_none() -> None:
    text = "申请条件：品行端正。"
    assert locate_quote(text, "这句话不在原文") is None


def test_locate_quote_empty_inputs_return_none() -> None:
    assert locate_quote("正文", "") is None
    assert locate_quote("", "引文") is None


def test_locate_quote_whitespace_only_quote_returns_none() -> None:
    assert locate_quote("正文内容", "   ") is None
