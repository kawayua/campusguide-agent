"""W3 Verifier 层测试：范围检查、claim 硬校验与判定树。"""

from __future__ import annotations

from datetime import date

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.enums import ArticleStatus, Audience
from campusguide.verification.verifier import DeterministicVerifier
from tests.w2_helpers import make_article, make_evidence

AS_OF = date(2025, 10, 1)


def _draft(
    text: str,
    quote: str = "申请奖学金应当学习成绩优异，品行端正。",
    claim_id: str = "c1",
    article_id: str = "demo-policy@v2021#第一条",
) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        kind="eligibility",
        citations=[CitationDraft(article_id=article_id, quote=quote)],
    )


# ---------- check_scope（H6 版本 / H5 受众） ----------


def test_scope_passes_active_evidence() -> None:
    verifier = DeterministicVerifier()
    result = verifier.check_scope([make_evidence(make_article())], None, AS_OF)
    assert result.abstain_reason is None
    assert len(result.evidence) == 1
    codes = {issue.code for issue in result.issues}
    assert codes == {"AUDIENCE_UNDETERMINED"}


def test_scope_repealed_only_abstains() -> None:
    verifier = DeterministicVerifier()
    article = make_article(status=ArticleStatus.REPEALED)
    result = verifier.check_scope([make_evidence(article)], None, AS_OF)
    assert result.abstain_reason is not None
    assert result.abstain_reason.value == "ONLY_REPEALED"


def test_scope_version_window_mismatch() -> None:
    verifier = DeterministicVerifier()
    article = make_article(effective_from=date(2026, 1, 1))
    result = verifier.check_scope([make_evidence(article)], None, AS_OF)
    assert result.abstain_reason is not None
    assert result.abstain_reason.value == "VERSION_MISMATCH"
    assert result.issues[0].code == "VERSION_MISMATCH"


def test_scope_detects_overlapping_active_versions() -> None:
    verifier = DeterministicVerifier()
    # 生效日相同的多版本无法裁决 -> VERSION_CONFLICT
    v1 = make_article(version_id="v2021")
    v2 = make_article(version_id="v2025")
    result = verifier.check_scope([make_evidence(v1), make_evidence(v2)], None, AS_OF)
    assert result.abstain_reason is not None
    assert result.abstain_reason.value == "VERSION_CONFLICT"


def test_scope_cohort_versions_warn_but_do_not_abstain() -> None:
    verifier = DeterministicVerifier()
    # 培养方案等"届别版本"：生效日不同 -> 保留证据并记 MIXED_VERSIONS warning
    v1 = make_article(version_id="v2022", effective_from=date(2022, 9, 1))
    v2 = make_article(version_id="v2024", effective_from=date(2024, 9, 1))
    result = verifier.check_scope([make_evidence(v1), make_evidence(v2)], None, AS_OF)
    assert result.abstain_reason is None
    assert len(result.evidence) == 2
    codes = {issue.code for issue in result.issues}
    assert "MIXED_VERSIONS" in codes
    assert "VERSION_CONFLICT" not in codes


def test_scope_audience_mismatch_blocks_only_bad_evidence() -> None:
    verifier = DeterministicVerifier()
    undergrad = make_article()
    faculty = make_article(
        article_id="faculty@v2021#第一条",
        audience=[Audience.FACULTY],
    )
    result = verifier.check_scope(
        [make_evidence(undergrad, 0.9, 1), make_evidence(faculty, 0.8, 2)],
        Audience.UNDERGRADUATE,
        AS_OF,
    )
    assert result.abstain_reason is None
    assert [item.article.article_id for item in result.evidence] == [undergrad.article_id]
    assert any(issue.code == "AUDIENCE_MISMATCH" for issue in result.issues)


def test_scope_audience_all_matches_any_request() -> None:
    verifier = DeterministicVerifier()
    article = make_article(audience=[Audience.ALL])
    result = verifier.check_scope([make_evidence(article)], Audience.GRADUATE, AS_OF)
    assert result.abstain_reason is None
    assert len(result.evidence) == 1


def test_scope_audience_all_mismatch_abstains_unverifiable() -> None:
    verifier = DeterministicVerifier()
    faculty = make_article(audience=[Audience.FACULTY])
    result = verifier.check_scope([make_evidence(faculty)], Audience.UNDERGRADUATE, AS_OF)
    assert result.abstain_reason is not None
    assert result.abstain_reason.value == "UNVERIFIABLE"


def test_scope_requesting_all_audience_disables_filter() -> None:
    verifier = DeterministicVerifier()
    undergrad = make_article()
    faculty = make_article(article_id="faculty@v2021#第一条", audience=[Audience.FACULTY])
    result = verifier.check_scope(
        [make_evidence(undergrad, 0.9, 1), make_evidence(faculty, 0.8, 2)],
        Audience.ALL,
        AS_OF,
    )
    assert result.abstain_reason is None
    assert len(result.evidence) == 2
    assert all(issue.code != "AUDIENCE_MISMATCH" for issue in result.issues)


# ---------- verify（H4/H10 + 判定树） ----------


def test_verify_pass_keeps_valid_claim() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [_draft("申请奖学金应当学习成绩优异，品行端正。")],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    assert result.decision.value == "pass"
    assert result.claims and result.issues == []


def test_verify_fact_not_supported_drops_claim() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [_draft("奖励标准为每人8000元。")],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    assert result.decision.value == "abstain"
    assert result.reason_code is not None
    assert result.reason_code.value == "UNVERIFIABLE"
    assert result.issues[0].code == "NUMBER_NOT_SUPPORTED"
    assert "8000元" in result.issues[0].facts


def test_verify_fact_covered_by_other_citation() -> None:
    verifier = DeterministicVerifier()
    article = make_article(
        text="奖励标准为每人8000元。申请奖学金应当学习成绩优异。",
    )
    draft = ClaimDraft(
        claim_id="c1",
        text="奖励标准为每人8000元，且应当学习成绩优异。",
        kind="other",
        citations=[CitationDraft(article_id=article.article_id, quote="每人8000元")],
    )
    result = verifier.verify([draft], [make_evidence(article)], None, AS_OF)
    assert result.decision.value == "pass"


def test_verify_partial_when_one_claim_fails() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [
            _draft("申请奖学金应当学习成绩优异，品行端正。", claim_id="c1"),
            _draft("奖励标准为每人8000元。", claim_id="c2"),
        ],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    assert result.decision.value == "partial"
    assert result.reason_code is not None
    assert result.reason_code.value == "PARTIAL_ANSWER"
    assert [claim.claim_id for claim in result.claims] == ["c1"]
    assert result.dropped_claim_ids == ["c2"]


def test_verify_cap_excess_claims_is_warning_not_partial() -> None:
    verifier = DeterministicVerifier()
    drafts = [
        _draft("申请奖学金应当学习成绩优异，品行端正。", claim_id=f"c{index}")
        for index in range(1, 7)
    ]
    result = verifier.verify(drafts, [make_evidence(make_article())], None, AS_OF)
    assert result.decision.value == "pass"
    assert len(result.claims) == 5
    cap_issues = [issue for issue in result.issues if issue.code == "TOO_MANY_CLAIMS"]
    assert cap_issues and cap_issues[0].severity.value == "warning"
    assert result.dropped_claim_ids == []


def test_verify_privacy_violation_drops_claim() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [_draft("请联系13812345678咨询。")],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    assert result.decision.value == "abstain"
    codes = {issue.code for issue in result.issues}
    assert "PRIVACY_VIOLATION" in codes


def test_verify_zero_drafts_maps_no_valid_claim() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify([], [make_evidence(make_article())], None, AS_OF)
    assert result.decision.value == "abstain"
    assert result.reason_code is not None
    assert result.reason_code.value == "NO_VALID_CLAIM"


def test_verify_fabricated_quote_yields_issues() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [_draft("申请奖学金应当学习成绩优异。", quote="这句话不在原文中")],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    assert result.decision.value == "abstain"
    assert result.reason_code is not None
    assert result.reason_code.value == "UNVERIFIABLE"
    assert {issue.code for issue in result.issues} == {"QUOTE_MISMATCH", "MISSING_CITATION"}


def test_verify_official_email_is_allowed() -> None:
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [_draft("可发送邮件至jwc@scau.edu.cn咨询。")],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    codes = {issue.code for issue in result.issues}
    assert "PRIVACY_VIOLATION" not in codes


def test_verify_contact_name_is_not_treated_as_privacy() -> None:
    # 设计 6.4 只拦手机号/身份证/学号/个人邮箱；公开条款里的“联系人：X”不算
    verifier = DeterministicVerifier()
    result = verifier.verify(
        [_draft("联系人：张三，负责受理申请。")],
        [make_evidence(make_article())],
        None,
        AS_OF,
    )
    assert result.decision.value == "pass"
    assert all(issue.code != "PRIVACY_VIOLATION" for issue in result.issues)
