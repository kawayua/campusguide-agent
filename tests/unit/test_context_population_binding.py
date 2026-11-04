"""Population/context binding regressions for coverage answer support."""

from __future__ import annotations

from datetime import date

from campusguide.agents.coverage import (
    _effective_population,
    _scoped_content,
    _topic_covered,
)
from campusguide.agents.coverage_context import (
    COMPLETED,
    ENROLLED,
    filter_population_branches,
    scan_population,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.domain.enums import BlockType
from campusguide.domain.models import Citation, Claim


def _unit(span: str, **kwargs) -> QuestionUnit:
    return QuestionUnit(unit_id="u1", original_span=span, **kwargs)


def _claim(text: str, title: str, claim_id: str = "c1") -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id="a1",
                quote=text[:60],
                quote_start=0,
                quote_end=min(len(text), 60),
                doc_title=title,
                article_no="第一条",
                block_type=BlockType.ARTICLE,
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/policy",
            )
        ],
    )


def test_population_aliases_keep_unfinished_students_enrolled() -> None:
    assert scan_population("本科未毕业学生").levels == frozenset({"本科"})
    assert scan_population("本科未毕业学生").statuses == frozenset({ENROLLED})
    assert scan_population("未毕业生").statuses == frozenset({ENROLLED})
    assert scan_population("本科在读").statuses == frozenset({ENROLLED})
    assert scan_population("学籍在籍").statuses == frozenset({ENROLLED})
    assert scan_population("已毕业学生").statuses == frozenset({COMPLETED})
    assert scan_population("本科毕业生").statuses == frozenset({COMPLETED})
    # Certificate names are not enrolment status; 研究生 is a level only.
    assert scan_population("在读证明").statuses == frozenset()
    assert scan_population("毕业证明").statuses == frozenset()
    assert scan_population("研究生").statuses == frozenset()
    assert scan_population("研究生").levels == frozenset({"研究生"})


def test_wrong_student_location_is_not_covered_by_title_lender() -> None:
    topic = "我还是本科在校生"
    wrong = _claim("本科毕业生成绩单自助打印点为档案馆一楼大厅。", "本科生成绩单办理指南")
    neutral_wrong_group = _claim(
        "成绩单自助打印点为档案馆一楼大厅。", "毕业生成绩单办理指南"
    )
    right = _claim("本科在校生成绩单自助打印点为档案馆一楼大厅。", "本科生成绩单办理指南")
    split_binding = _claim("在校生成绩单自助打印点为档案馆一楼大厅。", "本科生成绩单办理指南")

    assert _topic_covered(topic, wrong) is False
    assert _topic_covered(topic, neutral_wrong_group) is False
    assert _topic_covered(topic, right) is True
    assert _topic_covered(topic, split_binding) is True


def test_wrong_student_proof_is_not_covered_by_title_lender() -> None:
    topic = "我还是本科在校生"
    wrong = _claim("本科毕业生可以办理学历证明，只出具毕业证明。", "本科生证明办理指南")
    right = _claim("本科在校生可以办理学历证明，只出具在读证明。", "本科生证明办理指南")

    assert _topic_covered(topic, wrong) is False
    assert _topic_covered(topic, right) is True


def test_wrong_mixed_branch_does_not_lend_the_graduate_alternative() -> None:
    unit = _unit("我还是本科在校生，能办理学历证明吗，可以出具哪一种证明？")
    wrong = _claim(
        "在校生不办理学历证明；已毕业学生可办理学历证明，只出具毕业证明。",
        "本科生证明办理指南",
    )
    right = _claim(
        "在校生不办理学历证明，只出具在读证明；已毕业学生可以办理学历证明。",
        "本科生证明办理指南",
    )
    semicolon_continuation = _claim(
        "在校生不办理学历证明；只出具在读证明；已毕业学生可以办理学历证明。",
        "本科生证明办理指南",
    )

    wrong_scoped = _scoped_content(unit, wrong, wrong.text, None)
    right_scoped = _scoped_content(unit, right, right.text, None)
    continuation_scoped = _scoped_content(
        unit, semicolon_continuation, semicolon_continuation.text, None
    )

    assert "在校生不办理学历证明" in wrong_scoped
    assert "已毕业" not in wrong_scoped
    assert "毕业证明" not in wrong_scoped
    assert "只出具在读证明" in right_scoped
    assert "已毕业" not in right_scoped
    assert "毕业证明" not in right_scoped
    assert "只出具在读证明" in continuation_scoped
    assert "已毕业" not in continuation_scoped


def test_explicit_asked_group_overrides_irrelevant_biography() -> None:
    span = "我是在校生，想了解毕业生办理学历证明的流程。"
    topic = "我是在校生"
    claim = _claim("毕业生可以办理学历证明，只出具毕业证明。", "本科生证明办理指南")

    population = _effective_population(span, [topic])
    assert population is not None
    assert population.statuses == frozenset({COMPLETED})
    assert _topic_covered(topic, claim) is False
    assert _topic_covered(topic, claim, population=population) is True
    assert _scoped_content(_unit(span), claim, claim.text, None) == claim.text


def test_generic_student_context_needs_no_literal_restatement() -> None:
    topic = "我还是学生"
    claim = _claim("图书馆开放时间为周一至周五。", "图书馆开放时间")
    assert _topic_covered(topic, claim) is True


def test_population_comparison_is_not_collapsed_to_one_branch() -> None:
    unit = _unit("本科生和研究生补考和重修成绩如何记载？")
    claim = _claim("本科生和研究生补考和重修成绩均按最高分记载。", "成绩记载规定")

    population = _effective_population(unit.original_span)
    assert population is not None
    assert not population.has_critical
    assert _scoped_content(unit, claim, claim.text, None) == claim.text


def test_title_naming_both_groups_cannot_lend_a_branch() -> None:
    required = scan_population("我还是本科在校生")
    neutral = "成绩单自助打印点为档案馆一楼大厅。"

    assert filter_population_branches(
        required, neutral, "本科生与毕业生成绩单办理指南"
    ) == ""
    assert filter_population_branches(
        required, neutral, "在校生与毕业生成绩单办理指南"
    ) == ""


def test_title_only_cannot_override_explicit_or_ambiguous_population() -> None:
    unit = _unit("我还是本科在校生，哪些地点可以自助打印成绩单？")
    wrong_body = _claim(
        "本科毕业生成绩单自助打印点为档案馆一楼大厅。", "本科生成绩单办理指南"
    )
    ambiguous_title = _claim(
        "成绩单自助打印点为档案馆一楼大厅。", "在校生与毕业生成绩单办理指南"
    )
    explicit = _claim("成绩单自助打印点为档案馆一楼大厅。", "本科在校生成绩单办理指南")

    assert _scoped_content(unit, wrong_body, wrong_body.text, None) == ""
    assert _scoped_content(unit, ambiguous_title, ambiguous_title.text, None) == ""
    assert _scoped_content(unit, explicit, explicit.text, None) == explicit.text
