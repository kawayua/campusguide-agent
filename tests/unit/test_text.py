from __future__ import annotations

from campusguide.domain.text import clean_whitespace, count_cjk, normalize_text


def test_normalize_text_removes_whitespace_and_normalizes_punctuation() -> None:
    raw = "　申请国家奖学金的学生，\n应当“学习成绩优异”。 "
    assert normalize_text(raw) == '申请国家奖学金的学生,应当"学习成绩优异"。'


def test_normalize_text_is_idempotent() -> None:
    once = normalize_text("第一條  測試（一）")
    assert normalize_text(once) == once


def test_clean_whitespace_keeps_paragraphs() -> None:
    raw = "第一条  总则\r\n\r\n\r\n正文   内容  \r\n"
    assert clean_whitespace(raw) == "第一条 总则\n\n正文 内容"


def test_clean_whitespace_drops_private_use_bullet_glyphs() -> None:
    raw = "第五章 休学与复学\n\ue004第二十四条 学生可以分阶段完成学业。"
    assert clean_whitespace(raw) == "第五章 休学与复学\n第二十四条 学生可以分阶段完成学业。"


def test_count_cjk() -> None:
    assert count_cjk("abc中文，。") == 2
