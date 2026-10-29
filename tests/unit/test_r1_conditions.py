"""R1 guard cases: condition direction, modality, role and category conflicts.

Report case 001 ("30分钟后" written as "30分钟内") and case 098 (ordinary labs
listed as demonstration centres) are the P0 anchors. The remaining cases are
boundary variants that must stay accepted, so the new gate cannot block correct
answers.
"""

from __future__ import annotations

import pytest

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.agents.verifier import GraphVerifier
from campusguide.domain.enums import GraphDecision
from campusguide.domain.models import Evidence
from campusguide.verification.conditions import (
    check_conditions,
    compare_categories,
    compare_modalities,
    compare_thresholds,
    extract_thresholds,
)
from tests.w2_helpers import make_article
from tests.w6_helpers import article

EXAM_ID = "exam-room-rules@v2022#第十六条"
EXAM_QUOTE = article(EXAM_ID).text

CENTERS_QUOTE = (
    "学院现有实验教学示范中心：基于云平台的计算机实验教学示范中心、"
    "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心。"
    "另设云计算与大数据实验室、物联网实验室、软件实验室。"
)


# ---------------------------------------------------------------- thresholds


def test_detects_inverted_threshold_direction() -> None:
    findings = check_conditions(
        "开考后30分钟内，监考员禁止迟到的考生入场。", [EXAM_QUOTE]
    )
    assert [finding.kind for finding in findings] == ["threshold_direction"]


@pytest.mark.parametrize(
    "claim",
    [
        "开考30分钟后，监考员禁止迟到的考生入场。",  # correct direction
        "开考后30分钟内，一律不允许考生离开考场。",  # the other rule of the quote
        "迟到35分钟后不得入场。",  # another rule and another threshold
        "迟到30分钟以上按缺考论。",  # unrelated threshold
    ],
)
def test_accepts_correct_or_unrelated_thresholds(claim: str) -> None:
    assert compare_thresholds(claim, [EXAM_QUOTE]) == []


def test_detects_changed_threshold_value_for_the_same_action() -> None:
    findings = compare_thresholds("开考20分钟后，监考员禁止迟到的考生入场。", [EXAM_QUOTE])
    assert [finding.kind for finding in findings] == ["threshold_value"]


def test_threshold_phase_before_and_after_are_opposite() -> None:
    findings = compare_thresholds("开学后两周内办理请假。", ["开学前两周内办理请假。"])
    assert [finding.kind for finding in findings] == ["threshold_phase"]
    assert compare_thresholds("开学前两周内办理请假。", ["开学前两周内办理请假。"]) == []


def test_within_and_at_most_agree() -> None:
    assert compare_thresholds("每周不超过8小时。", ["每周原则上不超过8小时。"]) == []


def test_threshold_parser_relations() -> None:
    parsed = {
        (item.anchor, item.relation)
        for item in extract_thresholds("开考后30分钟内；迟到30分钟以上；开学前两周内")
    }
    assert ("开考", "within") in parsed
    assert ("迟到", "at_least") in parsed
    assert ("开学", "within") in parsed


# ---------------------------------------------------------------- modality


def test_detects_forbidden_versus_allowed() -> None:
    findings = compare_modalities(
        "学生可以携带手机进入考场。", ["学生不得携带手机进入考场。"]
    )
    assert [finding.kind for finding in findings] == ["modality"]


def test_same_modality_is_not_a_conflict() -> None:
    assert (
        compare_modalities("学生不得将手机带至考座。", ["学生不得将手机等物品带至考座。"])
        == []
    )


# ---------------------------------------------------------------- categories


def test_detects_category_overreach() -> None:
    """Case 098: ordinary labs must not inherit the demonstration-centre label."""
    findings = check_conditions(
        "学院实验教学示范中心：基于云平台的计算机实验教学示范中心、"
        "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心、"
        "云计算与大数据实验室、物联网实验室。",
        [CENTERS_QUOTE],
    )
    assert [finding.kind for finding in findings] == ["category"]


def test_accepts_the_three_named_centres() -> None:
    claim = (
        "学院实验教学示范中心：基于云平台的计算机实验教学示范中心、"
        "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心；"
        "另有云计算与大数据实验室。"
    )
    assert compare_categories(claim, [CENTERS_QUOTE]) == []


# ---------------------------------------------------------------- integration


def _draft(claim_id: str, text: str) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        kind="fact",
        citations=[CitationDraft(article_id=EXAM_ID, quote=EXAM_QUOTE)],
    )


def test_verifier_blocks_inverted_condition_and_keeps_other_claims() -> None:
    evidence = [Evidence(article=article(EXAM_ID))]
    drafts = [
        _draft("c1", "开考后30分钟内，监考员禁止迟到的考生入场。"),
        _draft("c2", "开考30分钟后，监考员禁止迟到的考生入场。"),
    ]
    report = GraphVerifier().verify(drafts, evidence, question="考试迟到怎么处理？")
    assert report.decision is GraphDecision.REVISE
    assert report.failed_claim_ids == ["c1"]
    assert "CONDITION_CONTRADICTED" in {issue.code for issue in report.issues}
# ---------------------------------------------------------------- exceptions (035)


def test_exception_is_reported_but_does_not_drop_the_claim() -> None:
    quote = (
        "研究生在学期间发表的论文应以华南农业大学为第一署名单位。"
        "2026年9月14日补充说明：联合培养研究生的署名单位按联合培养协议约定执行，不适用前款规定。"
    )
    findings = check_conditions("研究生发表的论文均须以华南农业大学为第一署名单位。", [quote])
    assert [finding.kind for finding in findings] == ["exception_dropped"]

    evidence = [
        Evidence(
            article=make_article(
                "signature-rule@v2026#第七条",
                doc_id="signature-rule",
                version_id="v2026",
                doc_title="研究生学位论文与发表成果署名规定",
                article_no="第七条",
                text=quote,
            )
        )
    ]
    draft = ClaimDraft(
        claim_id="c1",
        text="研究生发表的论文均须以华南农业大学为第一署名单位。",
        kind="fact",
        citations=[CitationDraft(article_id="signature-rule@v2026#第七条", quote=quote)],
    )
    report = GraphVerifier().verify([draft], evidence, question="论文署名单位怎么要求？")
    assert report.decision is GraphDecision.PASS
    assert "c1" not in report.failed_claim_ids
    assert "EXCEPTION_DROPPED" in {issue.code for issue in report.issues}