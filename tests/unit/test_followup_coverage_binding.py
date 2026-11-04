"""Follow-up 2026-10-30: object/version/class/aspect binding and gap isolation."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from campusguide.agents.coverage import CoverageChecker, _claim_supports_unit
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.question_units import decompose_question
from campusguide.config import load_organization
from campusguide.domain.models import Citation, Claim
from campusguide.retrieval.profile_expand import ProfileLexicon

ROOT = Path(__file__).resolve().parents[2]


def _lexicon() -> ProfileLexicon:
    return ProfileLexicon.from_organization(load_organization(ROOT / "configs/organization.yaml"))


def _class_terms() -> dict[str, tuple[str, ...]]:
    return _lexicon().class_terms


def _citation(
    title: str,
    *,
    no: str = "二、课程结构",
    article_id: str = "agro@v2024#二、课程结构",
    quote: str = "农学专业培养方案原文。",
) -> Citation:
    return Citation(
        article_id=article_id,
        part_no=1,
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        doc_title=title,
        article_no=no,
        effective_from=date(2024, 9, 1),
        source_url="https://example.edu.cn/plan",
    )


def _claim(
    text: str, *, title: str = "农学专业培养方案", no: str = "二、课程结构"
) -> Claim:
    return Claim(claim_id="c1", text=text, citations=[_citation(title, no=no)])


def _unit(
    aspect: str = "毕业学分",
    *,
    cohort: int = 2024,
    class_type: str | None = None,
    subject: str = "农学",
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=f"{cohort}级{subject}{aspect}是什么？",
        subject=subject,
        objects=[subject],
        cohort=cohort,
        version=f"{cohort}版",
        class_type=class_type,
        requested_aspects=[aspect],
    )


def test_2024_fact_does_not_cover_2022_unit_but_unknown_version_stays_usable() -> None:
    claim_2024 = _claim("2024版农学专业总学分为160学分。")
    assert _claim_supports_unit(_unit(cohort=2024), claim_2024)
    assert not _claim_supports_unit(_unit(cohort=2022), claim_2024)

    claim_2022 = _claim(
        "2022版农学专业总学分为150学分。",
        title="2022版农学专业培养方案",
    )
    assert _claim_supports_unit(_unit(cohort=2022), claim_2022)

    # 没有版本标记的来源按未知版本处理，不因缺少年份被误判为错版本。
    unknown = _claim("农学专业总学分为150学分。", title="农学专业培养方案")
    assert _claim_supports_unit(_unit(cohort=2022), unknown)
    assert _claim_supports_unit(_unit(cohort=2024), unknown)


def test_comparison_claim_with_both_versions_covers_both_units() -> None:
    claim = _claim(
        "2024版农学专业总学分为160学分，2022版农学专业总学分为150学分。",
        title="农学专业培养方案版本对比",
    )
    assert _claim_supports_unit(_unit(cohort=2022), claim)
    assert _claim_supports_unit(_unit(cohort=2024), claim)


def test_gap_clause_is_not_support_but_answered_clause_survives() -> None:
    mixed = _claim("2024版农学专业总学分为160学分，2022版未提供完整对比。")
    assert _claim_supports_unit(_unit(cohort=2024), mixed)
    # 缺口句里的“2022”不是 2022 单元已被回答的证据。
    assert not _claim_supports_unit(_unit(cohort=2022), mixed)

    gap_only = _claim("农学专业总学分证据不足，无法回答。")
    assert not _claim_supports_unit(_unit(cohort=2024), gap_only)


def test_innovation_class_fact_does_not_cover_general_class() -> None:
    innovation_claim = _claim(
        "农学丁颖创新班培养目标是培养拔尖创新人才。",
        title="农学丁颖创新班培养方案（2024版）",
    )
    terms = _class_terms()
    assert _claim_supports_unit(
        _unit("培养目标", class_type="dingying-innovation"),
        innovation_claim,
        class_terms=terms,
    )
    assert not _claim_supports_unit(
        _unit("培养目标", class_type="agronomy-general"),
        innovation_claim,
        class_terms=terms,
    )

    # 未标注班型的普通培养方案可以回答普通班，但不能回答创新班。
    base_claim = _claim(
        "农学专业培养目标是培养复合型人才。",
        title="农学专业培养方案（2024版）",
    )
    assert _claim_supports_unit(
        _unit("培养目标", class_type="agronomy-general"),
        base_claim,
        class_terms=terms,
    )
    assert not _claim_supports_unit(
        _unit("培养目标", class_type="dingying-innovation"),
        base_claim,
        class_terms=terms,
    )


def test_gap_clause_about_other_class_does_not_reject_general_class() -> None:
    claim = _claim(
        "农学专业总学分为160学分，丁颖创新班未提供完整对比。",
        title="农学专业培养方案（2024版）",
    )
    assert _claim_supports_unit(
        _unit(class_type="agronomy-general"), claim, class_terms=_class_terms()
    )


def test_publicity_method_accepts_equivalent_published_list_text() -> None:
    unit = _unit("公示方式")
    assert _claim_supports_unit(
        unit,
        _claim("农学专业奖学金名单在学院官网以附件形式发布名单。", title="农学专业奖学金评审办法"),
    )
    assert _claim_supports_unit(
        unit,
        _claim("农学专业奖学金名单在学院网站公布结果。", title="农学专业奖学金评审办法"),
    )
    assert not _claim_supports_unit(
        unit,
        _claim("农学专业奖学金名单已经产生，结果另行通知。", title="农学专业奖学金评审办法"),
    )
    assert not _claim_supports_unit(
        unit,
        _claim("农学专业奖学金公示方式未提供。", title="农学专业奖学金评审办法"),
    )


def _coverage_response() -> str:
    return json.dumps(
        {
            "units": [
                {
                    "unit_id": "u1",
                    "status": "answered",
                    "claim_ids": ["c1"],
                    "missing_items": [],
                }
            ],
            "unanswered_questions": [],
        },
        ensure_ascii=False,
    )


def _audit(unit: QuestionUnit, claim: Claim):
    checker = CoverageChecker(FakeChatProvider([_coverage_response()]), profile_lexicon=_lexicon())
    return checker.check("农学专业培养目标是什么？", [unit], [claim], [], [])


def test_coverage_checker_enforces_class_binding_end_to_end() -> None:
    innovation_claim = _claim(
        "农学丁颖创新班培养目标是培养拔尖创新人才。",
        title="农学丁颖创新班培养方案（2024版）",
    )
    wrong = _audit(_unit("培养目标", class_type="agronomy-general"), innovation_claim)
    assert wrong.units[0]["status"] == "generation_missing"
    assert wrong.units[0]["claim_ids"] == []

    right = _audit(_unit("培养目标", class_type="dingying-innovation"), innovation_claim)
    assert right.units[0]["status"] == "answered"
    assert right.units[0]["claim_ids"] == ["c1"]


def test_decompose_splits_registered_class_types_into_separate_units() -> None:
    units = decompose_question(
        "2024级农学丁颖创新班和普通农学专业的培养目标有什么不同？", _lexicon()
    )
    assert {unit.class_type for unit in units} == {
        "dingying-innovation",
        "agronomy-general",
    }
    assert all(unit.class_type for unit in units)
    assert {unit.cohort for unit in units} == {2024}
