from __future__ import annotations

from campusguide.config import load_organization
from campusguide.domain.enums import AudienceStage, ProgramType
from campusguide.domain.profiles import UserProfile
from campusguide.retrieval.profile_expand import ProfileLexicon


def _lexicon() -> ProfileLexicon:
    return ProfileLexicon.from_organization(load_organization("configs/organization.yaml"))


def test_major_name_is_appended_for_seed_science() -> None:
    variants = _lexicon().query_variants(
        "农学专业培养方案",
        UserProfile(colleges=["agri"], majors=["seed-science"]),
    )
    assert variants
    assert any("种子科学与工程" in variant for variant in variants)


def test_intake_year_is_prioritized() -> None:
    variants = _lexicon().query_variants(
        "农学专业 2022 版培养方案",
        UserProfile(colleges=["agri"], majors=["agronomy"], intake_year=2024),
    )
    assert variants[0].endswith("2024级")


def test_college_alias_is_used_when_major_already_in_query() -> None:
    variants = _lexicon().query_variants(
        "计算机科学与技术专业培养方案",
        UserProfile(colleges=["info"], majors=["computer-science"]),
    )
    assert variants
    assert any("数信学院" in variant or "数学与信息学院" in variant for variant in variants)


def test_no_profile_or_empty_lexicon_returns_no_variants() -> None:
    lexicon = _lexicon()
    assert lexicon.query_variants("奖学金", None) == []
    assert lexicon.query_variants("奖学金", UserProfile()) == []
    assert ProfileLexicon({}, {}).query_variants("奖学金", UserProfile(colleges=["agri"])) == []


def test_variants_are_capped() -> None:
    variants = _lexicon().query_variants(
        "培养方案",
        UserProfile(
            colleges=["agri"],
            majors=["agronomy"],
            intake_year=2024,
            stage=AudienceStage.YEAR2,
            program_type=ProgramType.ACADEMIC,
        ),
        max_variants=2,
    )
    assert len(variants) == 2
    assert all(variant.startswith("培养方案 ") for variant in variants)
