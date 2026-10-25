from __future__ import annotations

import json

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.agents.plan import Clarification
from campusguide.agents.providers import ChatProviderError, FakeChatProvider
from campusguide.agents.verifier import GraphVerifier
from campusguide.domain.enums import GraphDecision, OutcomeCode, VerificationDecision
from campusguide.domain.models import ChecklistItem, Evidence
from campusguide.verification.semantic import SemanticVerifier
from tests.w6_helpers import article


def evidence() -> list[Evidence]:
    return [Evidence(article=article("national-aid-undergrad@v2021#第九条"))]


def quote() -> str:
    return article("national-aid-undergrad@v2021#第九条").text[:70]


def draft(
    claim_id: str = "c1",
    *,
    text: str = "国家奖学金要求二年级及以上。",
    kind: str = "eligibility",
    bad_quote: bool = False,
) -> ClaimDraft:
    item = article("national-aid-undergrad@v2021#第九条")
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        kind=kind,
        citations=[
            CitationDraft(
                article_id=item.article_id,
                quote="不存在的连续片段但是足够长" if bad_quote else quote(),
            )
        ],
    )


def semantic(labels: dict[str, str], *, error: Exception | None = None):
    if error is not None:
        return SemanticVerifier(FakeChatProvider([error]))
    payload = json.dumps(
        {
            "labels": [
                {"claim_id": claim_id, "label": label, "detail": "d"}
                for claim_id, label in labels.items()
            ]
        },
        ensure_ascii=False,
    )
    return SemanticVerifier(FakeChatProvider([payload]))


def checklist() -> list[ChecklistItem]:
    return [
        ChecklistItem(
            step_id="step1",
            title="提交申请",
            status="available",
            description="",
            materials=["国家奖学金申请审批表"],
            department="学院学生工作办公室",
            citations=["national-aid-undergrad@v2021#第九条"],
        )
    ]


def test_hard_pass_and_semantic_supported() -> None:
    report = GraphVerifier(semantic_verifier=semantic({"c1": "supported"})).verify(
        [draft()], evidence()
    )
    assert report.decision is GraphDecision.PASS
    assert report.verification is not None
    assert report.verification.decision is VerificationDecision.PASS
    assert not report.failed_claim_ids


def test_hard_quote_mismatch_revises_then_partial() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c2": "supported"}),
        max_revise=0,
    )
    report = verifier.verify([draft(bad_quote=True), draft("c2")], evidence())
    assert report.decision is GraphDecision.PASS
    assert report.verification is not None
    assert report.verification.decision is VerificationDecision.PARTIAL
    assert report.failed_claim_ids == ["c1"]
    assert report.verification.claims[0].claim_id == "c2"


def test_hard_quote_mismatch_requests_revise_when_budget_left() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "supported"}),
        max_revise=2,
    )
    report = verifier.verify([draft(bad_quote=True)], evidence(), revision_count=0)
    assert report.decision is GraphDecision.REVISE
    assert report.failed_claim_ids == ["c1"]
    assert report.revise_issues


def test_max_claims_overflow_is_not_revised() -> None:
    drafts = [draft(f"c{index}") for index in range(1, 7)]
    labels = {f"c{index}": "supported" for index in range(1, 6)}
    verifier = GraphVerifier(
        semantic_verifier=semantic(labels),
        max_revise=2,
    )
    report = verifier.verify(drafts, evidence())
    assert report.decision is GraphDecision.PASS
    assert report.verification is not None
    assert report.verification.decision is VerificationDecision.PARTIAL
    assert report.failed_claim_ids == ["c6"]
    assert not any(issue.code == "TOO_MANY_CLAIMS" for issue in report.revise_issues)


def test_semantic_contradicted_revises_then_abstains() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "contradicted"}),
        max_revise=0,
    )
    report = verifier.verify([draft()], evidence())
    assert report.decision is GraphDecision.ABSTAIN
    assert report.reason_code is OutcomeCode.UNVERIFIABLE


def test_semantic_contradicted_with_budget_requests_revise() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "contradicted"}),
        max_revise=2,
    )
    report = verifier.verify([draft()], evidence(), revision_count=1)
    assert report.decision is GraphDecision.REVISE


def test_semantic_partial_dropped_after_max_revise() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "partially_supported", "c2": "supported"}),
        max_revise=0,
    )
    report = verifier.verify([draft("c1"), draft("c2", text="国家奖学金还要看成绩。")], evidence())
    assert report.verification is not None
    assert report.verification.decision is VerificationDecision.PARTIAL
    assert [claim.claim_id for claim in report.verification.claims] == ["c2"]


def test_semantic_insufficient_dropped_after_max_revise() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "insufficient"}),
        max_revise=0,
    )
    report = verifier.verify([draft()], evidence())
    assert report.decision is GraphDecision.ABSTAIN
    assert report.reason_code is OutcomeCode.UNVERIFIABLE


def test_semantic_unavailable_strict_abstains() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({}, error=ChatProviderError("down")),
        semantic_mode="strict",
    )
    report = verifier.verify([draft()], evidence())
    assert report.decision is GraphDecision.ABSTAIN
    assert report.reason_code is OutcomeCode.UNVERIFIABLE
    assert any(issue.code == "SEMANTIC_UNAVAILABLE" for issue in report.issues)


def test_semantic_unavailable_allow_with_warning_passes() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({}, error=ChatProviderError("down")),
        semantic_mode="allow_with_warning",
    )
    report = verifier.verify([draft()], evidence())
    assert report.decision is GraphDecision.PASS
    assert report.semantic_unchecked is True
    assert any(issue.code == "SEMANTIC_UNCHECKED" for issue in report.issues)


def test_no_semantic_verifier_marks_unchecked() -> None:
    report = GraphVerifier(semantic_verifier=None).verify([draft()], evidence())
    assert report.decision is GraphDecision.PASS
    assert report.semantic_unchecked is True


def test_h9_department_mismatch_blocks_flow_claim() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "supported"}),
        max_revise=0,
    )
    bad = draft("c1", text="请到教务处办理。", kind="department")
    report = verifier.verify([bad], evidence(), checklist=checklist())
    assert report.decision is GraphDecision.ABSTAIN
    assert any(issue.code == "FLOW_FACT_MISMATCH" for issue in report.issues)


def test_h9_department_allowed() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "supported"}),
        max_revise=0,
    )
    good = draft("c1", text="请到学院学生工作办公室办理。", kind="department")
    report = verifier.verify([good], evidence(), checklist=checklist())
    assert report.decision is GraphDecision.PASS


def test_h9_material_mismatch_blocks() -> None:
    verifier = GraphVerifier(
        semantic_verifier=semantic({"c1": "supported"}),
        max_revise=0,
    )
    bad = draft("c1", text="需要提交成绩单。", kind="material")
    report = verifier.verify([bad], evidence(), checklist=checklist())
    assert report.decision is GraphDecision.ABSTAIN


def test_no_drafts_abstains_no_valid_claim() -> None:
    report = GraphVerifier(semantic_verifier=None).verify([], evidence())
    assert report.decision is GraphDecision.ABSTAIN
    assert report.reason_code is OutcomeCode.NO_VALID_CLAIM


def test_clarification_returns_clarify() -> None:
    report = GraphVerifier(semantic_verifier=None).verify(
        [],
        [],
        clarification=Clarification(reason="SLOT_MISSING", question="你是哪个学院？"),
    )
    assert report.decision is GraphDecision.CLARIFY
