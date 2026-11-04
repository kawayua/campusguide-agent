"""Category regressions for repeated enumerations in separate rule branches.

The source has four branches (four/five-year first major x 农林理工/文科 second
major). A claim that quotes only the first two branches must not be rejected as
a category merge just because later branches repeat the same category subject.
"""

from __future__ import annotations

import pytest

from campusguide.verification.conditions import check_conditions, compare_categories

try:
    from tests.unit.test_real_replay_a1_a6 import (
        CASE_098_BAD,
        CASE_098_GOOD,
        CASE_098_QUOTE,
    )
except ModuleNotFoundError:
    from test_real_replay_a1_a6 import (
        CASE_098_BAD,
        CASE_098_GOOD,
        CASE_098_QUOTE,
    )

SOURCE = (
    "第一主修专业为四年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
    "培养方案总学分原则上不超过185 学分；第二主修专业为文科类专业（含管理类专业）的，"
    "培养方案总学分原则上不超过175 学分。第一主修专业为五年制的双学士学位项目，"
    "第二主修专业为农林类、理工类专业的，培养方案总学分原则上不超过215 学分；"
    "第二主修专业为文科类专业（含管理类专业）的，培养方案总学分原则上不超过205 学分。"
)

CLAIM = (
    "第一主修专业为四年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
    "培养方案总学分原则上不超过185学分；第二主修专业为文科类专业（含管理类专业）的，"
    "培养方案总学分原则上不超过175学分。"
)

CENTERS_QUOTE = (
    "学院现有实验教学示范中心：基于云平台的计算机实验教学示范中心、"
    "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心。"
    "另设云计算与大数据实验室、物联网实验室、软件实验室。"
)

# Literal citation text from opened_VD03_hard_claim_coverage.json: PDF extraction
# wraps "不超过\n185 学分" and the five-year "文科类专业\n（含管理类专业）".
PDF_SOURCE = (
    "第一主修专业为四年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
    "培养方案总学分原则上不超过\n"
    "185 学分；第二主修专业为文科类专业（含管理类专业）的，"
    "培养方案总学分原则上不超过175 学分。"
    "第一主修专业为五年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
    "培养方案总学分原则上不超过215 学分；第二主修专业为文科类专业\n"
    "（含管理类专业）的，培养方案总学分原则上不超过205 学分。"
)

SWAPPED = (
    "第一主修专业为四年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
    "培养方案总学分原则上不超过175学分；第二主修专业为文科类专业（含管理类专业）的，"
    "培养方案总学分原则上不超过185学分。"
)

GENERIC_SOURCE = (
    "第一主修专业为三年制的双学士学位项目，第二主修专业为艺术类、体育类专业的，"
    "培养方案总学分原则上不超过120\n学分；第二主修专业为外语类专业\n"
    "（含翻译类专业）的，培养方案总学分原则上不超过110 学分。"
)

GENERIC_CLAIM = (
    "第一主修专业为三年制的双学士学位项目，第二主修专业为艺术类、体育类专业的，"
    "培养方案总学分原则上不超过120学分；第二主修专业为外语类专业（含翻译类专业）的，"
    "培养方案总学分原则上不超过110学分。"
)

GENERIC_SWAPPED = (
    "第一主修专业为三年制的双学士学位项目，第二主修专业为艺术类、体育类专业的，"
    "培养方案总学分原则上不超过110学分；第二主修专业为外语类专业（含翻译类专业）的，"
    "培养方案总学分原则上不超过120学分。"
)


def _linewrap_variants(source: str) -> list[str]:
    return [
        source.replace("不超过185 学分", "不超过\n185 学分", 1),
        source.replace("185 学分", "185\n学分", 1),
        source.replace(
            "第二主修专业为文科类专业（含管理类专业）的，"
            "培养方案总学分原则上不超过175 学分",
            "第二主修专业为文科类专业\n（含管理类专业）的，"
            "培养方案总学分原则上不超过175 学分",
            1,
        ),
        source.replace(
            "第二主修专业为文科类专业（含管理类专业）的，"
            "培养方案总学分原则上不超过205 学分",
            "第二主修专业为文科类专业\n（含管理类专业）的，"
            "培养方案总学分原则上不超过205 学分",
            1,
        ),
        PDF_SOURCE,
    ]


def test_correct_two_branch_prefix_is_not_a_category_merge() -> None:
    assert compare_categories(CLAIM, [SOURCE]) == []
    assert check_conditions(CLAIM, [SOURCE]) == []


def test_identical_source_is_reflexive() -> None:
    assert compare_categories(SOURCE, [SOURCE]) == []
    assert check_conditions(SOURCE, [SOURCE]) == []


@pytest.mark.parametrize(
    ("claim", "quote"),
    [
        (CLAIM, SOURCE),
        (CLAIM, SOURCE.replace("；", "；\n")),
        (CLAIM.replace("；", "\n"), SOURCE),
        (CLAIM.replace("；", "；\n"), SOURCE.replace("；", "；\n")),
        (
            CLAIM.replace("185学分", "185 学分").replace("175学分", "175 学分"),
            SOURCE,
        ),
        (
            CLAIM,
            SOURCE.replace("185 学分", "185学分").replace("175 学分", "175学分"),
        ),
    ],
)
def test_whitespace_and_newline_adjacency_do_not_merge_branches(
    claim: str, quote: str
) -> None:
    assert compare_categories(claim, [quote]) == []


def test_swapped_credit_totals_blocked() -> None:
    findings = compare_categories(SWAPPED, [SOURCE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_pdf_linewrap_correct_claim_is_accepted() -> None:
    assert compare_categories(CLAIM, [PDF_SOURCE]) == []
    assert check_conditions(CLAIM, [PDF_SOURCE]) == []


def test_pdf_linewrap_source_is_reflexive() -> None:
    assert compare_categories(PDF_SOURCE, [PDF_SOURCE]) == []
    assert check_conditions(PDF_SOURCE, [PDF_SOURCE]) == []


def test_pdf_linewrap_swapped_credits_blocked() -> None:
    findings = compare_categories(SWAPPED, [PDF_SOURCE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}
    details = " ".join(finding.detail for finding in findings)
    assert "185" in details and "175" in details


@pytest.mark.parametrize("quote", _linewrap_variants(SOURCE))
def test_linewrap_variants_bind_credits_to_their_branch(quote: str) -> None:
    assert compare_categories(CLAIM, [quote]) == []
    findings = compare_categories(SWAPPED, [quote])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_newline_only_branches_keep_credit_binding() -> None:
    quote = SOURCE.replace("；", "\n")
    assert compare_categories(CLAIM, [quote]) == []
    findings = compare_categories(SWAPPED, [quote])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_generic_linewrapped_constants_are_branch_bound() -> None:
    assert compare_categories(GENERIC_CLAIM, [GENERIC_SOURCE]) == []
    findings = compare_categories(GENERIC_SWAPPED, [GENERIC_SOURCE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_first_major_role_is_not_matched_to_second_major_group() -> None:
    claim = (
        "第一主修专业为农林类、理工类专业的，"
        "培养方案总学分原则上不超过185学分。"
    )
    findings = compare_categories(claim, [SOURCE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_role_swap_blocked() -> None:
    swapped = (
        "第二主修专业为四年制的双学士学位项目，第一主修专业为农林类、理工类专业的，"
        "培养方案总学分原则上不超过185学分；第一主修专业为文科类专业（含管理类专业）的，"
        "培养方案总学分原则上不超过175学分。"
    )
    findings = compare_categories(swapped, [SOURCE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_merged_category_branch_blocked() -> None:
    merged = (
        "第一主修专业为四年制的双学士学位项目，"
        "第二主修专业为农林类、理工类及文科类专业的，"
        "培养方案总学分原则上不超过185学分。"
    )
    findings = compare_categories(merged, [SOURCE])
    assert findings
    assert any("文科" in finding.detail for finding in findings)


@pytest.mark.parametrize(
    "claim",
    [
        (
            "第一主修专业为五年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
            "培养方案总学分原则上不超过185学分。"
        ),
        (
            "第一主修专业为四年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
            "培养方案总学分原则上不超过215学分。"
        ),
    ],
)
def test_wrong_four_or_five_year_threshold_blocked(claim: str) -> None:
    assert compare_categories(claim, [SOURCE])


def test_numeric_agreement_does_not_skip_category_guard() -> None:
    quote = CENTERS_QUOTE + "专项经费共30万元。"
    claim = (
        "学院实验教学示范中心：基于云平台的计算机实验教学示范中心、"
        "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心、"
        "云计算与大数据实验室，专项经费共30万元。"
    )
    findings = compare_categories(claim, [quote])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


BARE_TYPED_QUOTE = (
    "甲实验教学示范中心乙实验教学示范中心丙实验教学示范中心"
    "计算机软件实验室计算机硬件实验室"
)

BARE_TYPED_CLAIM = (
    "实验教学示范中心包括甲实验教学示范中心、乙实验教学示范中心、"
    "丙实验教学示范中心。"
)

EXPLICIT_TYPED_QUOTE = (
    "实验教学示范中心：甲实验教学示范中心、乙实验教学示范中心、"
    "丙实验教学示范中心。普通实验室：计算机软件实验室、计算机硬件实验室。"
)

EXPLICIT_TYPED_CLAIM = (
    "实验教学示范中心包括甲实验教学示范中心、乙实验教学示范中心、"
    "丙实验教学示范中心、计算机软件实验室。"
)


def test_case_098_unpunctuated_three_centres_are_accepted() -> None:
    assert compare_categories(CASE_098_GOOD, [CASE_098_QUOTE]) == []
    assert check_conditions(CASE_098_GOOD, [CASE_098_QUOTE]) == []


def test_case_098_ordinary_labs_in_centre_list_still_blocked() -> None:
    findings = compare_categories(CASE_098_BAD, [CASE_098_QUOTE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}


def test_bare_typed_list_is_not_foreign_category_evidence() -> None:
    assert compare_categories(BARE_TYPED_CLAIM, [BARE_TYPED_QUOTE]) == []


def test_explicit_foreign_category_assignment_still_blocked() -> None:
    findings = compare_categories(EXPLICIT_TYPED_CLAIM, [EXPLICIT_TYPED_QUOTE])
    assert findings
    assert {finding.kind for finding in findings} == {"category"}
