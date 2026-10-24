from __future__ import annotations

from campusguide.domain.enums import OutcomeCode
from campusguide.domain.profiles import UserProfile
from campusguide.qa.guard import check_guard, check_version_intent


def _profile(year: int = 2024) -> UserProfile:
    return UserProfile(colleges=["agri"], majors=["agronomy"], intake_year=year)


def test_old_plan_year_is_version_mismatch() -> None:
    decision = check_version_intent("农学专业 2022 版培养方案", _profile(2024))
    assert decision is not None
    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.VERSION_MISMATCH
    assert "2022" in decision.message
    assert "2024" in decision.message


def test_matching_plan_year_is_allowed() -> None:
    assert check_version_intent("农学专业 2024 版培养方案", _profile(2024)) is None


def test_no_profile_does_not_guess() -> None:
    assert check_version_intent("农学专业 2022 版培养方案", None) is None


def test_non_version_date_is_not_blocked() -> None:
    assert check_version_intent("2022 年国家奖学金标准是多少", _profile(2024)) is None


def test_other_named_university_is_out_of_scope() -> None:
    decision = check_guard("华南理工大学对处分有什么规定？")
    assert decision.outcome is OutcomeCode.OUT_OF_SCOPE
    assert check_guard("中山大学的转专业规定是什么？").outcome is OutcomeCode.OUT_OF_SCOPE


def test_own_university_and_generic_student_words_are_allowed() -> None:
    assert check_guard("华南农业大学对处分有什么规定？").allowed is True
    assert check_guard("大学生在校期间可以申请奖学金吗？").allowed is True
    assert check_guard("农学院的学生可以申请吗？").allowed is True
