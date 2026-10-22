from __future__ import annotations

from datetime import UTC, date, datetime

from campusguide.domain.enums import ArticleStatus, Audience, BlockType, DocType
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


def test_loose_rule_supplements_missing_article_in_mixed_document() -> None:
    text = "第一章 总则\n第一条 内容一。\n第二条内容二。\n第三条 内容三。\n"
    result = segment_document(make_parsed(text), make_version())
    assert result.used_loose_article_rule
    assert [item.article_no for item in result.indexable_articles] == [
        "第一条",
        "第二条",
        "第三条",
    ]
    assert result.missing_article_indices == []
    assert any("补充无分隔符条款：第二条" in warning for warning in result.warnings)


def test_wrapped_cjk_lines_are_joined_without_spaces() -> None:
    text = "第一章 总则\n第一条 学位申请人有下列情形之一\n的，不授予学位。\n（一）学术不端；\n"
    result = segment_document(make_parsed(text), make_version())
    article = next(item for item in result.indexable_articles if item.article_no == "第一条")
    assert "情形之一的" in article.text
    assert "之一\n的" not in article.text
    assert "\n（一）" in article.text


def test_sections_are_indexed_when_no_article_numbers() -> None:
    text = (
        "一、培养目标\n"
        "培养具有实践能力的高素质人才。\n"
        "二、课程设置\n"
        "专业核心课程包括数据结构与算法。\n"
    )
    result = segment_document(make_parsed(text), make_version())

    assert result.used_section_rule
    assert [article.article_no for article in result.indexable_articles] == [
        "一、培养目标",
        "二、课程设置",
    ]
    assert all(article.block_type is BlockType.SECTION for article in result.indexable_articles)
    assert "数据结构" in result.indexable_articles[1].text


def test_guide_without_headings_falls_back_to_paragraph_chunks() -> None:
    text = "本指南说明学院办事流程。" * 100
    version = make_version().model_copy(update={"doc_type": DocType.GUIDE})
    result = segment_document(make_parsed(text), version)

    assert result.used_paragraph_rule
    assert len(result.indexable_articles) > 1
    assert all(article.block_type is BlockType.SECTION for article in result.indexable_articles)
    assert all(article.article_no.startswith("段落") for article in result.indexable_articles)


def test_metadata_rows_with_dates_stay_separate() -> None:
    text = (
        "2026-10-20｜新闻一｜https://example.org/a\n"
        "2026-10-19｜新闻二｜https://example.org/b\n"
        "2026-10-18｜新闻三｜https://example.org/c\n"
    )
    version = make_version().model_copy(
        update={"doc_type": DocType.GUIDE, "title": "近一年学院新闻"}
    )
    result = segment_document(make_parsed(text), version)

    articles = result.indexable_articles
    assert len(articles) == 3
    assert [article.text.split("｜", 1)[0] for article in articles] == [
        "2026-10-20",
        "2026-10-19",
        "2026-10-18",
    ]
    assert all(article.article_no.startswith("段落") for article in articles)


def test_duplicate_section_headings_get_unique_ids() -> None:
    text = "一、培养目标\n第一段目标。\n二、课程设置\n课程一。\n一、培养目标\n第二段目标。\n"
    result = segment_document(make_parsed(text), make_version())
    goals = [
        article for article in result.indexable_articles if article.article_no == "一、培养目标"
    ]

    assert len(goals) == 2
    assert goals[0].article_id != goals[1].article_id
    assert goals[1].article_id.endswith("~2")


def test_chapter_preamble_between_sections_gets_own_block() -> None:
    text = (
        "一、学科概况\n"
        "学科概况正文，介绍学科历史与研究方向。\n"
        "第二章 培养方案\n"
        "学院 数学与信息学院 学制 3 年，课程学分 23 学分，培养环节 3 学分。\n"
        "一、课程设置\n"
        "专业核心课程包括数据结构与算法。\n"
    )
    result = segment_document(make_parsed(text), make_version())
    articles = result.indexable_articles

    assert [article.article_no for article in articles] == [
        "一、学科概况",
        "第二章 培养方案",
        "一、课程设置",
    ]
    assert "第二章" not in articles[0].text
    assert "学制 3 年" in articles[1].text
    assert articles[1].chapter_path == []
    assert articles[2].chapter_path == ["第二章 培养方案"]
    assert any("章节导语单独建块 1 处" in warning for warning in result.warnings)


def test_bound_volume_groups_sections_by_major() -> None:
    text = (
        "计算机科学与技术专业人才培养方案（2024 版）\n"
        "一、培养目标\n"
        "培养计算机领域拔尖创新人才。\n"
        "二、课程设置\n"
        "数据结构、操作系统。\n"
        "软件工程专业人才培养方案（2024 版）\n"
        "一、培养目标\n"
        "培养软件工程领域工程人才。\n"
        "二、课程设置\n"
        "软件工程、软件测试。\n"
    )
    result = segment_document(make_parsed(text), make_version())
    articles = result.indexable_articles

    assert [article.article_no for article in articles] == [
        "一、培养目标",
        "二、课程设置",
        "一、培养目标",
        "二、课程设置",
    ]
    assert articles[0].chapter_path == ["计算机科学与技术专业人才培养方案（2024 版）"]
    assert articles[2].chapter_path == ["软件工程专业人才培养方案（2024 版）"]
    assert "软件测试" in articles[3].text
