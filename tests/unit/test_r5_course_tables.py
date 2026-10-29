"""R5 guard cases: course lists and table structure.

Report cases 020/047/049/064/065/071/075/080/092. Requirements: keep the header,
row, semester, credits and module of a curriculum table; never read a 支撑矩阵 as
a course list; split Chinese full stops; drop explanatory "最为核心的5至8门" text;
10.5 must not become 105.
"""

from __future__ import annotations

import pytest

from campusguide.agents.plan import QuestionUnit
from campusguide.retrieval.source_spans import source_spans
from campusguide.verification.claims import ClaimValidator
from campusguide.verification.coverage_items import required_course_items
from campusguide.verification.facts import extract_facts, facts_not_covered
from tests.w2_helpers import make_article, make_evidence

HEADER = "课程代码 | 课程名称 | 学分 | 学时 | 开课学期"
ROW_1 = "617017 | 毕业论文（设计） | 6 | 14周 | 第8学期"
ROW_2 = "610001 | 高等数学 | 5 | 80 | 第1学期"

CURRICULUM = "\n".join(
    [
        "2024版农学专业培养方案课程设置",
        HEADER,
        ROW_1,
        ROW_2,
    ]
)

MATRIX = "\n".join(
    [
        "毕业要求支撑矩阵",
        "课程代码 | 课程名称 | 毕业要求1 | 毕业要求2 | 支撑强度",
        "617017 | 毕业论文（设计） | H | M | 强",
    ]
)


def _unit(aspect: str, subject: str = "农学") -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=f"{subject}专业的{aspect}有哪些？",
        subject=subject,
        requested_aspects=[aspect],
    )


def test_table_rows_keep_header_semester_credits_and_module() -> None:
    article = make_article(
        article_no="课程设置", text=CURRICULUM, doc_title="2024版农学专业培养方案"
    )
    spans = source_spans([make_evidence(article)])
    rows = {span.label: span for span in spans.values() if "_row_" in span.span_id}
    thesis = rows["毕业论文（设计）"]
    assert thesis.credits == "6"
    assert thesis.headers[:3] == ("课程代码", "课程名称", "学分")
    assert thesis.semester == "第8学期"
    assert not thesis.is_matrix


def test_support_matrix_is_not_a_course_list() -> None:
    article = make_article(article_no="支撑矩阵", text=MATRIX, doc_title="培养方案支撑矩阵")
    spans = source_spans([make_evidence(article)])
    rows = [span for span in spans.values() if "_row_" in span.span_id]
    assert rows and all(row.is_matrix for row in rows)
    assert required_course_items(_unit("实践课程"), [make_evidence(article)]) == []


def test_practice_aspect_picks_the_thesis_row_only() -> None:
    article = make_article(
        article_no="课程设置", text=CURRICULUM, doc_title="2024版农学专业培养方案"
    )
    items = required_course_items(_unit("实践课程"), [make_evidence(article)])
    assert "毕业论文（设计）" in items
    assert "高等数学" not in items


def test_chinese_full_stop_splits_core_courses_and_drops_explanatory_text() -> None:
    article = make_article(
        article_no="专业核心课程",
        doc_title="2024版农学专业培养方案",
        text="专业核心课程包括植物学、遗传学、作物栽培学。其中最为核心的5至8门课程由学院确定。",
    )
    items = required_course_items(_unit("核心课程"), [make_evidence(article)])
    assert items == ["植物学", "遗传学", "作物栽培学"]


def test_decimal_credits_survive_normalisation() -> None:
    article = make_article(article_no="学分", text="总学分10.5学分，其中选修不少于6学分。")
    evidence = [make_evidence(article)]
    quote = article.text
    assert "10.5学分" in {fact.canonical for fact in extract_facts(quote)}
    assert facts_not_covered("总学分10.5学分。", [quote]) == []
    # 10.5 被写成 105 属于事实错误，不能被当作等价。
    uncovered = facts_not_covered("总学分105学分。", [quote])
    assert [fact.canonical for fact in uncovered] == ["105学分"]


def test_validator_rejects_unknown_number_but_keeps_known_decimal() -> None:
    article = make_article(article_no="学分", text="总学分10.5学分，其中选修不少于6学分。")
    evidence = [make_evidence(article)]
    result = ClaimValidator().check([], evidence)
    assert result.claims == []