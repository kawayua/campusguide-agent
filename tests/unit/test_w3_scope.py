"""W3 Scope 层：受众、版本、冲突与输出隐私。"""

from __future__ import annotations

from datetime import date

from campusguide.domain.enums import ArticleStatus, Audience, IssueScope, IssueSeverity, OutcomeCode
from campusguide.verification.privacy import privacy_issue, scan_privacy
from campusguide.verification.scope import apply_scope
from tests.w2_helpers import make_article, make_evidence

AS_OF = date(2026, 10, 23)


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def _ids(result) -> list[tuple[str, int]]:
    return [(item.article.article_id, item.article.part_no) for item in result.evidence]


def test_scope_without_audience_keeps_evidence_and_warns() -> None:
    article = make_article()
    result = apply_scope([make_evidence(article)], audience=None, as_of=AS_OF)

    assert _ids(result) == [(article.article_id, 1)]
    assert "AUDIENCE_UNDETERMINED" in _codes(result)
    assert result.reason_code is None
    issue = next(issue for issue in result.issues if issue.code == "AUDIENCE_UNDETERMINED")
    assert issue.scope is IssueScope.EVIDENCE
    assert issue.severity is IssueSeverity.WARNING


def test_scope_filters_audience_mismatch_and_abstains_when_all_filtered() -> None:
    article = make_article(audience=[Audience.GRADUATE])
    result = apply_scope(
        [make_evidence(article)],
        audience=Audience.UNDERGRADUATE,
        as_of=AS_OF,
    )

    assert result.evidence == []
    assert "AUDIENCE_MISMATCH" in _codes(result)
    assert result.reason_code is OutcomeCode.UNVERIFIABLE


def test_scope_accepts_all_audience_and_keeps_other_evidence() -> None:
    all_article = make_article(
        article_id="all@v1#第一条",
        doc_id="all",
        audience=[Audience.ALL],
    )
    undergrad = make_article()
    graduate = make_article(
        article_id="grad@v1#第一条",
        doc_id="grad",
        audience=[Audience.GRADUATE],
    )
    result = apply_scope(
        [
            make_evidence(all_article, rank=1),
            make_evidence(undergrad, rank=2),
            make_evidence(graduate, rank=3),
        ],
        audience=Audience.UNDERGRADUATE,
        as_of=AS_OF,
    )

    assert {article_id for article_id, _ in _ids(result)} == {
        all_article.article_id,
        undergrad.article_id,
    }
    assert "AUDIENCE_MISMATCH" in _codes(result)
    assert result.reason_code is None


def test_scope_treats_all_audience_as_unspecified() -> None:
    article = make_article()
    result = apply_scope([make_evidence(article)], audience=Audience.ALL, as_of=AS_OF)

    assert _ids(result) == [(article.article_id, 1)]
    assert "AUDIENCE_UNDETERMINED" in _codes(result)


def test_scope_filters_repealed_and_all_filtered_is_version_mismatch() -> None:
    article = make_article(status=ArticleStatus.REPEALED)
    result = apply_scope([make_evidence(article)], audience=Audience.UNDERGRADUATE, as_of=AS_OF)

    assert result.evidence == []
    assert "VERSION_MISMATCH" in _codes(result)
    assert result.reason_code is OutcomeCode.VERSION_MISMATCH


def test_scope_filters_future_and_expired_evidence() -> None:
    future = make_article(
        article_id="future@v1#第一条",
        doc_id="future",
        effective_from=date(2027, 1, 1),
    )
    expired = make_article(
        article_id="expired@v1#第一条",
        doc_id="expired",
        effective_from=date(2020, 1, 1),
        effective_to=date(2025, 12, 31),
    )
    result = apply_scope(
        [make_evidence(future, rank=1), make_evidence(expired, rank=2)],
        audience=Audience.UNDERGRADUATE,
        as_of=AS_OF,
    )

    assert result.evidence == []
    assert _codes(result) == {"VERSION_MISMATCH"}
    assert result.reason_code is OutcomeCode.VERSION_MISMATCH


def test_scope_keeps_current_low_confidence_evidence() -> None:
    article = make_article(date_confidence="low")
    result = apply_scope([make_evidence(article)], audience=Audience.UNDERGRADUATE, as_of=AS_OF)

    assert _ids(result) == [(article.article_id, 1)]
    assert "VERSION_MISMATCH" not in _codes(result)


def test_scope_mixed_versions_warns_but_keeps_evidence() -> None:
    old = make_article(
        article_id="demo-policy@v2024#第一条",
        version_id="v2024",
        effective_from=date(2024, 1, 1),
    )
    new = make_article(
        article_id="demo-policy@v2025#第一条",
        version_id="v2025",
        effective_from=date(2025, 1, 1),
    )
    result = apply_scope(
        [make_evidence(old, rank=1), make_evidence(new, rank=2)],
        audience=Audience.UNDERGRADUATE,
        as_of=AS_OF,
    )

    assert len(result.evidence) == 2
    assert "MIXED_VERSIONS" in _codes(result)
    assert result.reason_code is None


def test_scope_same_effective_from_conflict_filters_and_abstains() -> None:
    old = make_article(
        article_id="demo-policy@v2024#第一条",
        version_id="v2024",
        effective_from=date(2025, 1, 1),
    )
    new = make_article(
        article_id="demo-policy@v2025#第一条",
        version_id="v2025",
        effective_from=date(2025, 1, 1),
    )
    result = apply_scope(
        [make_evidence(old, rank=1), make_evidence(new, rank=2)],
        audience=Audience.UNDERGRADUATE,
        as_of=AS_OF,
    )

    assert result.evidence == []
    assert "VERSION_CONFLICT" in _codes(result)
    assert result.reason_code is OutcomeCode.VERSION_CONFLICT


def test_scope_conflict_removes_doc_but_keeps_other_docs() -> None:
    old = make_article(
        article_id="demo-policy@v2024#第一条",
        version_id="v2024",
        effective_from=date(2025, 1, 1),
    )
    new = make_article(
        article_id="demo-policy@v2025#第一条",
        version_id="v2025",
        effective_from=date(2025, 1, 1),
    )
    other = make_article(
        article_id="other@v1#第一条",
        doc_id="other",
        effective_from=date(2022, 1, 1),
    )
    result = apply_scope(
        [make_evidence(old, rank=1), make_evidence(new, rank=2), make_evidence(other, rank=3)],
        audience=Audience.UNDERGRADUATE,
        as_of=AS_OF,
    )

    assert _ids(result) == [(other.article_id, 1)]
    assert "VERSION_CONFLICT" in _codes(result)
    assert result.reason_code is None


def test_scope_empty_evidence_returns_no_evidence_reason() -> None:
    result = apply_scope([], audience=None, as_of=AS_OF)

    assert result.evidence == []
    assert result.reason_code is OutcomeCode.NO_EVIDENCE


def test_scan_privacy_detects_personal_data() -> None:
    text = (
        "手机号 13800138000，身份证 440101199001011234，"
        "学号：2023123456，邮箱 someone@example.com。"
    )
    assert set(scan_privacy(text)) == {
        "id_card",
        "mobile",
        "personal_email",
        "student_id",
    }


def test_scan_privacy_allows_landline_and_official_email() -> None:
    text = "咨询电话 020-85281234，邮箱 admissions@scau.edu.cn，学院邮箱 teacher@mail.scau.edu.cn。"
    assert scan_privacy(text) == []


def test_privacy_issue_never_contains_raw_values() -> None:
    issue = privacy_issue("c1", ["mobile", "personal_email"])
    payload = issue.model_dump(mode="json")

    assert issue.code == "PRIVACY_VIOLATION"
    assert issue.scope is IssueScope.CLAIM
    assert issue.severity is IssueSeverity.BLOCKING
    assert issue.facts == ["mobile", "personal_email"]
    assert "13800138000" not in str(payload)
    assert "someone@example.com" not in str(payload)
