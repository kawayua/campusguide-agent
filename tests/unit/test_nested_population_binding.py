"""Compound student-level population binding regressions."""

from __future__ import annotations

from datetime import date

from campusguide.agents.coverage import _scoped_content, _topic_covered
from campusguide.agents.coverage_context import (
    filter_population_branches,
    population_conflicts,
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


def test_compound_level_phrases_scan_as_one_atom() -> None:
    master = scan_population("硕士研究生")
    doctor = scan_population("博士研究生")

    assert master.levels == frozenset({"硕士"})
    assert doctor.levels == frozenset({"博士"})
    assert master.is_single_branch is True
    assert doctor.is_single_branch is True


def test_explicit_compound_level_comparison_stays_two_atoms() -> None:
    assert scan_population("硕士生和博士生").levels == frozenset({"硕士", "博士"})
    assert scan_population("硕士研究生和博士研究生").levels == frozenset(
        {"硕士", "博士"}
    )
    assert scan_population("本科生和研究生").levels == frozenset({"本科", "研究生"})


def test_body_doctor_level_conflicts_with_required_master_under_broad_title() -> None:
    required = scan_population("硕士研究生")
    body = "博士研究生成绩单自助打印点为研究生院一楼大厅。"

    assert population_conflicts(required, body, "研究生成绩单办理指南") is True


def test_body_master_level_conflicts_with_required_doctor_under_broad_title() -> None:
    required = scan_population("博士研究生")
    body = "硕士研究生成绩单自助打印点为研究生院一楼大厅。"

    assert population_conflicts(required, body, "研究生成绩单办理指南") is True


def test_generic_graduate_source_still_matches_specific_master() -> None:
    required = scan_population("硕士研究生")
    body = "研究生成绩单自助打印点为研究生院一楼大厅。"
    title = "研究生成绩单办理指南"

    assert population_conflicts(required, body, title) is False
    assert filter_population_branches(required, body, title) == body


def test_root_master_questions_reject_nested_doctor_claims() -> None:
    print_unit = _unit("我还是硕士研究生，哪些地点可以自助打印成绩单？")
    print_claim = _claim(
        "博士研究生成绩单自助打印点为研究生院一楼大厅。",
        "研究生成绩单办理指南",
    )
    proof_unit = _unit("我是在读硕士生，能办理学历证明吗，可以出具哪一种证明？")
    proof_claim = _claim(
        "在读博士研究生可以办理学历证明，只出具博士证明。",
        "研究生学历证明办理指南",
    )

    assert _scoped_content(print_unit, print_claim, print_claim.text, None) == ""
    assert _topic_covered("我还是硕士研究生", print_claim) is False
    assert _scoped_content(proof_unit, proof_claim, proof_claim.text, None) == ""
    assert _topic_covered("我是在读硕士生", proof_claim) is False


def test_root_master_questions_accept_same_level_claims() -> None:
    print_unit = _unit("我还是硕士研究生，哪些地点可以自助打印成绩单？")
    print_claim = _claim(
        "硕士研究生成绩单自助打印点为研究生院一楼大厅。",
        "硕士研究生成绩单办理指南",
    )
    proof_unit = _unit("我是在读硕士生，能办理学历证明吗，可以出具哪一种证明？")
    proof_claim = _claim(
        "在读硕士研究生可以办理学历证明，只出具硕士证明。",
        "硕士研究生学历证明办理指南",
    )

    assert _scoped_content(print_unit, print_claim, print_claim.text, None) == (
        print_claim.text
    )
    assert _topic_covered("我还是硕士研究生", print_claim) is True
    assert _scoped_content(proof_unit, proof_claim, proof_claim.text, None) == (
        proof_claim.text
    )
    assert _topic_covered("我是在读硕士生", proof_claim) is True


def test_mixed_claim_branch_cannot_borrow_doctor_location() -> None:
    unit = _unit("我还是硕士研究生，哪些地点可以自助打印成绩单？")
    claim = _claim(
        "硕士研究生成绩单自助打印点为研究生院一楼大厅；"
        "博士研究生成绩单自助打印点为行政楼二楼。",
        "研究生成绩单办理指南",
    )

    scoped = _scoped_content(unit, claim, claim.text, None)

    assert "研究生院一楼大厅" in scoped
    assert "行政楼" not in scoped


def test_recipient_department_name_is_not_an_opposing_level() -> None:
    assert scan_population("研究生院").levels == frozenset()
    assert scan_population("本科生院").levels == frozenset()

    undergraduate = scan_population("我还是本科生")
    graduate_department_body = "成绩单自助打印点为研究生院一楼大厅。"
    undergraduate_title = "本科生成绩单办理指南"

    assert (
        population_conflicts(
            undergraduate, graduate_department_body, undergraduate_title
        )
        is False
    )
    assert (
        filter_population_branches(
            undergraduate, graduate_department_body, undergraduate_title
        )
        == graduate_department_body
    )
    claim = _claim(graduate_department_body, undergraduate_title)
    unit = _unit("我还是本科生，哪些地点可以自助打印成绩单？")
    assert _scoped_content(unit, claim, claim.text, None) == claim.text

    doctoral = scan_population("我还是博士研究生")
    undergraduate_department_body = "成绩单自助打印点为本科生院一楼大厅。"
    doctoral_title = "博士研究生成绩单办理指南"

    assert (
        population_conflicts(
            doctoral, undergraduate_department_body, doctoral_title
        )
        is False
    )
    assert (
        filter_population_branches(
            doctoral, undergraduate_department_body, doctoral_title
        )
        == undergraduate_department_body
    )
