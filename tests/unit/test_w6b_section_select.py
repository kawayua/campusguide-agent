from __future__ import annotations

from dataclasses import dataclass

from campusguide.domain.enums import BlockType
from campusguide.retrieval.section_select import (
    allows_sections,
    allows_tables,
    is_table_like,
    select_sections,
)
from tests.w2_helpers import make_article


@dataclass
class Candidate:
    article: object
    rerank_score: float | None


def _section(article_id: str, no: str, text: str) -> Candidate:
    article = make_article(
        article_id=article_id,
        article_no=no,
        block_type=BlockType.SECTION,
        text=text,
    )
    return Candidate(article=article, rerank_score=0.70)


def test_table_like_detection() -> None:
    table = _section(
        "plan@v2024#八、课程结构",
        "八、课程结构",
        "课程代码 | 课程名称 | 学分 | 学时\n601001 | 高等数学 | 4 | 64\n601002 | 大学英语 | 3 | 48",
    )
    narrative = _section(
        "plan@v2024#二、培养目标",
        "二、培养目标",
        "本专业培养具有社会责任感、能够服务现代农业发展的复合型人才。",
    )
    assert is_table_like(table.article) is True
    assert is_table_like(narrative.article) is False


def test_broad_query_prefers_narrative_and_caps_tables() -> None:
    narrative = _section(
        "plan@v2024#二、培养目标",
        "二、培养目标",
        "本专业培养具有社会责任感的复合型人才。",
    )
    table_a = _section(
        "plan@v2024#八、课程结构",
        "八、课程结构",
        "课程代码 | 课程名称 | 学分 | 学时\n601001 | 高等数学 | 4 | 64",
    )
    table_b = _section(
        "plan@v2024#十、教学计划进程表",
        "十、教学计划进程表",
        "课程代码 | 课程名称 | 学分 | 学时\n601002 | 大学英语 | 3 | 48",
    )
    # 表格分数略高，但宽问题应优先叙事 section，并最多保留 1 个表格。
    table_a.rerank_score = 0.72
    table_b.rerank_score = 0.71
    selection = select_sections(
        [table_a, table_b, narrative],
        query="农学专业培养方案",
        limit=3,
        max_table_sections=1,
    )
    ids = [item.article.article_id for item in selection.candidates]
    assert "plan@v2024#二、培养目标" in ids
    assert sum("课程" in item.article.article_no for item in selection.candidates) <= 1
    assert any("TABLE_SECTION_DEPRIORITIZED" in warning for warning in selection.warnings)


def test_table_intent_query_allows_tables() -> None:
    assert allows_tables("专业核心课程和学分学时怎么安排") is True
    assert allows_tables("培养目标是什么") is False
    tables = [
        _section(
            f"plan@v2024#表{i}",
            f"表{i}",
            "课程代码 | 课程名称 | 学分 | 学时\n601001 | 高等数学 | 4 | 64",
        )
        for i in range(3)
    ]
    selection = select_sections(
        tables,
        query="课程设置与学分学时",
        limit=3,
        max_table_sections=1,
    )
    assert len(selection.candidates) == 3


def test_course_intent_recognizes_subject_and_elective_group() -> None:
    query = "对于2024年入学的计算机科学与技术的学生来讲 我们的人工智能选修组有什么科目可以进行选择"
    assert allows_sections(query) is True
    assert allows_tables(query) is True
    assert allows_sections("申请国家奖学金需要满足哪些条件") is False


def test_table_intent_does_not_penalize_table_sections() -> None:
    table = _section(
        "plan@v2024#十、培养计划进程表",
        "十、培养计划进程表",
        "方向选修模块 | 人工智能方向\n课程代码 | 课程名称 | 学分\n613068 | 人工智能 | 2",
    )
    narrative = _section(
        "plan@v2024#二、培养目标",
        "二、培养目标",
        "本专业培养具有社会责任感的复合型人才。",
    )
    table.rerank_score = 0.74
    narrative.rerank_score = 0.71

    broad = select_sections([table, narrative], query="培养方案", limit=2)
    assert broad.candidates[0].article.article_id.endswith("#二、培养目标")

    table_query = select_sections([table, narrative], query="人工智能选修组有哪些科目", limit=2)
    assert table_query.candidates[0].article.article_id.endswith("#十、培养计划进程表")


def test_job_intent_query_boosts_placement_section() -> None:
    target = _section(
        "plan@v2026#一、人才培养目标",
        "一、人才培养目标",
        "博士生在理论研究或技术研究中取得被同行认可的创新性成果。"
        "可在高等院校、研究机构、企业或相关部门从事教学、科研、生产、推广、经营和管理工作。",
    )
    research = _section(
        "plan@v2026#五、研究生科研成果要求",
        "五、研究生科研成果要求",
        "学术型博士生至少满足以下条件之一：发表A类及以上级别期刊研究性论文至少2篇。",
    )
    target.rerank_score = 0.70
    research.rerank_score = 0.74

    selection = select_sections(
        [research, target],
        query="2026 版培养方案：博士生与硕士生在研究成果要求和毕业后可从事的工作方面有何规定？",
        limit=2,
    )
    assert selection.candidates[0].article.article_id == "plan@v2026#一、人才培养目标"

    # 不问就业去向时保持 rerank 顺序，不额外提权。
    plain = select_sections(
        [research, target],
        query="2026 版培养方案：博士生与硕士生的科研成果要求是什么？",
        limit=2,
    )
    assert plain.candidates[0].article.article_id == "plan@v2026#五、研究生科研成果要求"
