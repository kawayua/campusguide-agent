from __future__ import annotations

from datetime import UTC, date, datetime

from campusguide.domain.enums import ArticleStatus, Audience, BlockType
from campusguide.domain.models import DocumentVersion
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.segment import segment_document


def make_version() -> DocumentVersion:
    return DocumentVersion(
        doc_id="status-rules",
        version_id="v2021",
        title="华南农业大学本科学生学籍管理实施细则",
        doc_type="regulation",
        issuing_department="教务处",
        source_url="https://jwc.scau.edu.cn/example.htm",
        published_at=date(2021, 8, 30),
        effective_from=date(2021, 9, 1),
        status=ArticleStatus.ACTIVE,
        source_sha256="abc123",
        fetched_at=datetime(2026, 10, 19, tzinfo=UTC),
        parser_version="test",
        audience=[Audience.UNDERGRADUATE],
    )


def make_parsed(text: str) -> ParsedDocument:
    return ParsedDocument(
        doc_id="status-rules",
        version_id="v2021",
        title="华南农业大学本科学生学籍管理实施细则",
        text=text,
        format="html",
        source_sha256="abc123",
        parser_version="test",
        cjk_count=len(text),
        char_count=len(text),
    )


SAMPLE = """华南农业大学本科学生学籍管理实施细则
（华南农办〔2021〕68 号）

第一章 总则
第一条 为规范本科学生学籍管理，制定本细则。
第二条 本细则适用于本校全日制本科学生。

第二章 学籍异动
第三条 学生申请转专业，应当符合学校规定。
第三条规定的情形除外。
第四条 学生应当提交申请材料，咨询电话 13800138000。

华南农业大学
2021年8月30日
"""


def test_segments_chapters_and_articles() -> None:
    result = segment_document(make_parsed(SAMPLE), make_version())
    articles = result.indexable_articles

    assert [article.article_no for article in articles] == ["第一条", "第二条", "第三条", "第四条"]
    assert articles[0].chapter_path == ["第一章 总则"]
    assert articles[2].chapter_path == ["第二章 学籍异动"]
    assert articles[0].text.startswith("为规范本科学生学籍管理")
    assert "第一条" not in articles[0].text
    assert "第三条规定的情形除外" in articles[2].text
    assert "第三条规定的情形除外" not in articles[3].text

    assert any(article.block_type is BlockType.PREFACE for article in result.articles)
    # 落款不足最小长度，按规格书不进入条款库，只记录告警。
    assert not any(article.block_type is BlockType.APPENDIX for article in result.articles)
    assert any("忽略尾部短文本" in warning for warning in result.warnings)


def test_redacts_contact_info_in_article_text() -> None:
    result = segment_document(make_parsed(SAMPLE), make_version())
    article = next(item for item in result.articles if item.article_no == "第四条")
    assert "13800138000" not in article.text
    assert "[手机号已脱敏]" in article.text
    assert result.redaction_counts["mobile"] == 1


def test_duplicate_article_numbers_get_unique_ids_and_warning() -> None:
    text = """第一章 总则
第一条 内容一。
第三条 内容二。
第三条 内容三。
"""
    result = segment_document(make_parsed(text), make_version())
    third_articles = [item for item in result.articles if item.article_no == "第三条"]
    assert len(third_articles) == 2
    assert third_articles[0].article_id != third_articles[1].article_id
    assert third_articles[1].article_id.endswith("~2")
    assert result.duplicate_article_nos == ["第三条"]


def test_long_article_is_split_into_parts_sharing_article_id() -> None:
    body = "这是条款正文内容。" * 40
    text = f"第一章 总则\n第一条 {body}\n"
    result = segment_document(make_parsed(text), make_version(), max_article_chars=120)
    parts = [item for item in result.articles if item.article_no == "第一条"]
    assert len(parts) > 1
    assert len({item.article_id for item in parts}) == 1
    assert [item.part_no for item in parts] == list(range(1, len(parts) + 1))


def test_missing_article_numbers_are_reported() -> None:
    text = "第一章 总则\n第一条 内容一。\n第三条 内容三。\n"
    result = segment_document(make_parsed(text), make_version())
    assert result.missing_article_indices == [2]


def test_no_articles_becomes_non_indexable_block() -> None:
    result = segment_document(make_parsed("这是一份扫描件说明，没有条款编号。"), make_version())
    assert result.indexable_articles == []
    assert len(result.articles) == 1
    assert result.articles[0].block_type is BlockType.OTHER
    assert any("未识别到任何" in warning for warning in result.warnings)


def test_loose_rule_matches_glued_article_number() -> None:
    text = "第一章 总则\n第一条学生应当遵守学校规定。\n第二条学生应当按时注册。\n"
    result = segment_document(make_parsed(text), make_version())
    assert result.used_loose_article_rule
    assert [item.article_no for item in result.indexable_articles] == ["第一条", "第二条"]
