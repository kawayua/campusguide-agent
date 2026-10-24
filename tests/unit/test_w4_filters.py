from __future__ import annotations

from datetime import date

from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import ArticleStatus, Audience, AudienceStage, VersionMode
from campusguide.domain.models import PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.retrieval.filters import (
    academic_cycle,
    article_matches_profile,
    build_qdrant_filter,
    effective_statuses,
    filter_explanations,
    profile_undetermined_warnings,
)
from tests.w2_helpers import make_article

AS_OF = date(2026, 10, 24)


def _article(detail: AudienceDetail):
    return make_article().model_copy(update={"audience_detail": detail})


def test_college_mismatch_is_excluded_but_unknown_profile_keeps() -> None:
    article = _article(AudienceDetail(colleges=["info"]))
    matched, reasons = article_matches_profile(article, UserProfile(colleges=["agri"]), as_of=AS_OF)
    assert matched is False
    assert reasons and "学院不匹配" in reasons[0]

    matched, reasons = article_matches_profile(article, None, as_of=AS_OF)
    assert matched is True and reasons == []


def test_major_mismatch_is_excluded() -> None:
    article = _article(AudienceDetail(colleges=["agri"], majors=["agronomy"]))
    matched, reasons = article_matches_profile(
        article,
        UserProfile(colleges=["agri"], majors=["seed-science"]),
        as_of=AS_OF,
    )
    assert matched is False
    assert reasons and "专业不匹配" in reasons[0]


def test_intake_year_mismatch_is_excluded() -> None:
    article = _article(AudienceDetail(intake_years=[2022]))
    matched, reasons = article_matches_profile(article, UserProfile(intake_year=2024), as_of=AS_OF)
    assert matched is False
    assert reasons and "入学年份不匹配" in reasons[0]


def test_intake_from_and_stage_program_group_status_cycle() -> None:
    detail = AudienceDetail(
        intake_from=2023,
        stages=[AudienceStage.FINAL_YEAR],
        program_types=["academic"],
        groups=["financial_aid"],
        status=["graduating"],
        cycle="2026",
    )
    article = _article(detail)
    profile = UserProfile(
        intake_year=2022,
        stage=AudienceStage.YEAR2,
        program_type="professional",
        groups=["international"],
        status="enrolled",
    )
    matched, reasons = article_matches_profile(article, profile, as_of=AS_OF)
    assert matched is False
    joined = "；".join(reasons)
    assert "入学年份早于适用起点" in joined
    assert "阶段不匹配" in joined
    assert "培养类型不匹配" in joined
    assert "群体不匹配" in joined
    assert "学籍状态不匹配" in joined

    matched, reasons = article_matches_profile(
        _article(AudienceDetail(cycle="2025")), UserProfile(), as_of=AS_OF
    )
    assert matched is False
    assert "学年不匹配" in reasons[0]


def test_empty_detail_never_excludes() -> None:
    article = _article(AudienceDetail())
    matched, reasons = article_matches_profile(
        article,
        UserProfile(colleges=["agri"], intake_year=2024, stage=AudienceStage.YEAR2),
        as_of=AS_OF,
    )
    assert matched is True
    assert reasons == []


def test_profile_undetermined_warnings_only_when_constraint_present() -> None:
    constrained = _article(
        AudienceDetail(colleges=["agri"], majors=["agronomy"], intake_years=[2024])
    )
    plain = _article(AudienceDetail())
    warnings = profile_undetermined_warnings(
        [constrained, plain], None, audience=[Audience.UNDERGRADUATE]
    )
    assert any("PROFILE_UNDETERMINED" in warning and "学院" in warning for warning in warnings)
    assert any("入学年份" in warning for warning in warnings)
    assert not any("AUDIENCE_UNDETERMINED" in warning for warning in warnings)

    no_constraint = profile_undetermined_warnings(
        [plain], UserProfile(), audience=[Audience.UNDERGRADUATE]
    )
    assert no_constraint == []


def test_audience_undetermined_warning() -> None:
    warnings = profile_undetermined_warnings([], None, audience=None)
    assert any("AUDIENCE_UNDETERMINED" in warning for warning in warnings)


def test_academic_cycle_boundaries() -> None:
    assert academic_cycle(date(2026, 8, 31)) == ["2025", "2025-2026"]
    assert academic_cycle(date(2026, 9, 1)) == ["2026", "2026-2027"]


def test_effective_statuses_by_version_mode() -> None:
    current = PolicyFilters(as_of=AS_OF)
    assert effective_statuses(current) == [ArticleStatus.ACTIVE]

    historical = PolicyFilters(
        as_of=AS_OF,
        version_mode=VersionMode.HISTORICAL,
        status=None,
    )
    assert effective_statuses(historical) is None

    repealed = PolicyFilters(
        as_of=AS_OF,
        version_mode=VersionMode.INCLUDE_REPEALED,
        status=None,
    )
    assert effective_statuses(repealed) == [ArticleStatus.ACTIVE, ArticleStatus.REPEALED]


def test_qdrant_filter_contains_profile_and_cycle_conditions() -> None:
    filters = PolicyFilters(
        as_of=AS_OF,
        profile=UserProfile(
            colleges=["agri"],
            majors=["agronomy"],
            intake_year=2024,
            stage=AudienceStage.YEAR2,
            program_type="academic",
            groups=["financial_aid"],
            status="enrolled",
        ),
    )
    qfilter = build_qdrant_filter(filters)
    dumped = str(qfilter)
    for key in (
        "audience_colleges",
        "audience_majors",
        "audience_intake_years",
        "audience_intake_from",
        "audience_stages",
        "audience_program_types",
        "audience_groups",
        "audience_status",
        "audience_cycle",
    ):
        assert key in dumped
    assert any("profile[" in item for item in filter_explanations(filters))
