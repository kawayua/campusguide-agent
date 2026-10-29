"""R6/A1/A6 merge behaviour, using the review's real claims and counterexamples."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from campusguide.agents.graph import boundary_notes
from campusguide.domain.enums import OutcomeCode
from campusguide.domain.models import Citation, Claim
from campusguide.qa.merge import clean_claim_text, merge_claims

ROOT = Path(__file__).resolve().parents[2]


def _citation(doc_title: str, article_no: str = "第一条") -> Citation:
    return Citation(
        article_id=f"{doc_title}#{article_no}",
        quote="引用原文片段足够长以便通过校验。",
        quote_start=0,
        quote_end=18,
        doc_title=doc_title,
        article_no=article_no,
        effective_from=date(2025, 9, 1),
        source_url="https://www.scau.edu.cn/example.htm",
    )


def _claim(claim_id: str, text: str, *doc_titles: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[_citation(title) for title in doc_titles] or [_citation("某办法")],
    )


def _texts(claims: list[Claim]) -> list[str]:
    return [claim.text for claim in claims]


def _kinds(result) -> set[str]:
    return {note.kind for note in result.notes}


PLAN_2022 = "2022级计算机科学与技术专业毕业总学分为163学分。"
PLAN_2024 = "2024级计算机科学与技术专业毕业总学分为159.5学分。"


def test_cross_version_facts_are_both_kept() -> None:
    result = merge_claims(
        [
            _claim("c1", PLAN_2022, "计算机科学与技术专业人才培养方案"),
            _claim("c2", PLAN_2024, "计算机科学与技术专业人才培养方案"),
        ]
    )
    assert _texts(result.claims) == [PLAN_2022, PLAN_2024]
    assert result.dropped_claim_ids == []


def test_cross_version_result_is_order_independent() -> None:
    forward = merge_claims([_claim("c1", PLAN_2022), _claim("c2", PLAN_2024)])
    backward = merge_claims([_claim("c1", PLAN_2024), _claim("c2", PLAN_2022)])
    assert set(_texts(forward.claims)) == set(_texts(backward.claims))
    assert _kinds(forward) == _kinds(backward) == set()


def test_different_class_types_are_not_merged() -> None:
    result = merge_claims(
        [
            _claim("c1", "创新班毕业总学分为160学分。", "计算机科学与技术专业人才培养方案"),
            _claim("c2", "普通班毕业总学分为163学分。", "计算机科学与技术专业人才培养方案"),
        ]
    )
    assert len(result.claims) == 2


def test_true_conflict_keeps_both_statements_with_a_verification_note() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金奖励标准为每人每年8000元。", "华南农业大学国家奖学金评审办法"),
            _claim("c2", "国家奖学金奖励标准为每人每年6000元。", "农学院国家奖学金评审细则"),
        ]
    )
    assert len(result.claims) == 2
    assert "conflict" in _kinds(result)
    assert "核实" in next(n.detail for n in result.notes if n.kind == "conflict")


def test_conflict_is_not_decided_by_citation_count_or_order() -> None:
    one = _claim("c1", "国家奖学金奖励标准为每人每年8000元。", "办法甲")
    two = _claim(
        "c2",
        "国家奖学金奖励标准为每人每年6000元。",
        "办法乙",
        "办法乙",
    )
    forward = merge_claims([one, two])
    backward = merge_claims([two, one])
    assert len(forward.claims) == len(backward.claims) == 2
    assert set(_texts(forward.claims)) == set(_texts(backward.claims))


def test_duplicate_claims_collapse_without_changing_facts() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
            _claim("c2", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
        ]
    )
    assert len(result.claims) == 1
    assert result.dropped_claim_ids == ["c2"]
    assert "duplicate" in _kinds(result)


def test_similar_wording_with_different_numbers_is_not_a_duplicate() -> None:
    result = merge_claims(
        [
            _claim("c1", "计算机科学与技术专业毕业总学分为163学分。", "培养方案"),
            _claim("c2", "计算机科学与技术专业毕业总学分为159.5学分。", "培养方案"),
        ]
    )
    assert len(result.claims) == 2


def test_table_fragment_lines_are_removed() -> None:
    text = "| 课程代码 | 学分 |\n|---|---|\n610001 | 5 |\n学生需修满学分。"
    assert clean_claim_text(text) == "学生需修满学分。"


def test_stale_missing_is_retracted_in_either_order() -> None:
    negative = _claim("c1", "微专业结业证书规则未收录。")
    positive = _claim("c2", "微专业结业证书规则为修满规定学分后颁发证书。")
    for pair in ([negative, positive], [positive, negative]):
        result = merge_claims(pair)
        assert _texts(result.claims) == ["微专业结业证书规则为修满规定学分后颁发证书。"]
        assert "stale_missing" in _kinds(result)


def test_missing_statement_survives_when_nothing_covers_it() -> None:
    result = merge_claims([_claim("c1", "微专业结业证书规则未收录。")])
    assert _texts(result.claims) == ["微专业结业证书规则未收录。"]


def test_info_notes_do_not_become_substantive() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
            _claim("c2", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
        ]
    )
    assert result.substantive_notes == []
    assert result.info_notes


def test_boundary_notes_separate_corpus_gap_from_scope_filtering() -> None:
    gap = boundary_notes({}, OutcomeCode.NO_EVIDENCE)
    assert "可能" in gap[0] and "未命中" in gap[0]
    scope = boundary_notes(
        {"retrieval_warnings": ["AUDIENCE_MISMATCH: some-article"]}, OutcomeCode.NO_EVIDENCE
    )
    assert "被剔除" in scope[0] and "不等于学校没有规定" in scope[0]
    assert boundary_notes({}, OutcomeCode.UNVERIFIABLE) == []