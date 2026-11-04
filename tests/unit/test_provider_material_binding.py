"""Provider targets and material slots stay bound to their own ask (synthetic)."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    CoverageChecker,
    _property_gaps,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_PROVIDER_QUESTION = (
    "我办理英文成绩单时，在校生与毕业生的原始成绩单分别由哪里提供？"
)
_MATERIAL_QUESTION = "申请时应提供什么级别医院的检查证明以及何种盖章？"
_TITLE = "成绩单与证明办理说明"


def _unit(span: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=[span],
    )


def _claim(text: str, claim_id: str = "c1") -> Claim:
    citation = Citation(
        article_id=f"{_TITLE}#第一条",
        part_no=1,
        quote=text,
        quote_start=0,
        quote_end=len(text),
        doc_title=_TITLE,
        article_no="第一条",
        effective_from=date(2026, 1, 1),
        source_url="https://example.edu.cn/synthetic",
    )
    return Claim(claim_id=claim_id, text=text, citations=[citation])


def _audit(unit: QuestionUnit, claims: list[Claim], *, status: str = "answered"):
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": status,
                "claim_ids": [claim.claim_id for claim in claims]
                if status == "answered"
                else [],
                "missing_items": [],
            }
        ],
        "unanswered_questions": [],
    }
    checker = CoverageChecker(
        FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    )
    return checker.check(unit.original_span, [unit], claims, [], [])


def _provider_properties(question: str = _PROVIDER_QUESTION):
    return [
        prop
        for prop in _requested_properties(question)
        if prop.kind == "target" and prop.direction == "provider"
    ]


def _missing_provider_anchors(claims: list[Claim]) -> list[str]:
    missing, _ = _property_gaps(
        _provider_properties(), claims, _unit(_PROVIDER_QUESTION)
    )
    return [prop.required[0] for prop in missing if prop.required]


# --- A. Grammatical provider target -----------------------------------------


def test_provider_question_is_agency_not_location() -> None:
    properties = _requested_properties(_PROVIDER_QUESTION)
    providers = [
        prop
        for prop in properties
        if prop.kind == "target" and prop.direction == "provider"
    ]
    assert len(providers) == 2
    assert {prop.required[0] for prop in providers} == {"在校", "毕业"}
    assert all("由哪里提供" in prop.cue for prop in providers)
    assert all("在校" in prop.core or "毕业" in prop.core for prop in providers)
    assert not any(
        prop.kind == "target" and prop.direction == "location"
        for prop in properties
    )


def test_split_claims_for_each_population_join() -> None:
    unit = _unit(_PROVIDER_QUESTION)
    claims = [
        _claim("在校学生的原始成绩单由各学院提供。", "c1"),
        _claim("已毕业学生的原始成绩单由校档案馆提供。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "answered"
    assert set(audit.units[0]["claim_ids"]) == {"c1", "c2"}
    assert audit.missing == []


def test_one_population_cannot_answer_the_other() -> None:
    unit = _unit(_PROVIDER_QUESTION)
    claims = [_claim("在校学生的原始成绩单由各学院提供。")]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "generation_missing"
    assert any("由哪里提供" in item for item in audit.missing)
    assert _missing_provider_anchors(claims) == ["毕业"]


def test_swapped_population_cannot_mask_the_missing_group() -> None:
    unit = _unit(_PROVIDER_QUESTION)
    claims = [
        _claim("在校学生的原始成绩单由各学院提供。", "c1"),
        _claim("新生的原始成绩单由校档案馆提供。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "generation_missing"
    assert _missing_provider_anchors(claims) == ["毕业"]


def test_reverse_population_order_still_joins() -> None:
    unit = _unit(_PROVIDER_QUESTION)
    claims = [
        _claim(
            "已毕业学生的原始成绩单由校档案馆提供，"
            "在校学生的原始成绩单由各学院提供。"
        )
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_generic_departments_do_not_answer_provider() -> None:
    unit = _unit(_PROVIDER_QUESTION)
    claims = [
        _claim("在校学生的原始成绩单由有关部门提供。", "c1"),
        _claim("已毕业学生的原始成绩单由相关部门提供。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "generation_missing"
    assert set(_missing_provider_anchors(claims)) == {"在校", "毕业"}


def test_where_self_service_printing_stays_a_location() -> None:
    properties = _requested_properties("在校生可以在哪里自助打印成绩单？")
    assert any(
        prop.kind == "target" and prop.direction == "location"
        for prop in properties
    )
    assert not any(
        prop.kind == "target" and prop.direction == "provider"
        for prop in properties
    )


# --- B. Material slots bound to the asked proof -----------------------------


def test_complete_grade_and_stamp_in_one_clause() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claim = _claim(
        "申请时需提供三级甲等医院的检查证明（需盖疾病诊断证明专用章）。"
    )
    audit = _audit(unit, [claim])
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_stamp_for_another_proof_does_not_answer() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claim = _claim(
        "申请时需提供三级甲等医院的检查证明；"
        "诊断证明需盖疾病诊断证明专用章。"
    )
    audit = _audit(unit, [claim])
    assert audit.units[0]["status"] == "generation_missing"
    assert any("盖章" in item for item in audit.missing)
    assert not any("级别" in item for item in audit.missing)


def test_anaphor_carries_stamp_to_the_asked_proof() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claim = _claim(
        "申请时需提供三级甲等医院的检查证明，"
        "并在该证明上盖疾病诊断证明专用章。"
    )
    audit = _audit(unit, [claim])
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_grade_for_another_proof_does_not_answer() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claim = _claim(
        "申请时需提供检查证明（需盖疾病诊断证明专用章）；"
        "诊断证明需由二级甲等医院出具。"
    )
    audit = _audit(unit, [claim])
    assert audit.units[0]["status"] == "generation_missing"
    assert any("级别" in item for item in audit.missing)
    assert not any("盖章" in item for item in audit.missing)


def test_separate_claims_for_the_same_proof_join() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claims = [
        _claim("申请时需提供三级甲等医院的检查证明。", "c1"),
        _claim("检查证明需盖疾病诊断证明专用章。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "answered"
    assert set(audit.units[0]["claim_ids"]) == {"c1", "c2"}
    assert audit.missing == []


def test_separate_claim_with_proper_anaphor_joins() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claims = [
        _claim("申请时需提供三级甲等医院的检查证明。", "c1"),
        _claim("该证明需盖疾病诊断证明专用章。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "answered"
    assert set(audit.units[0]["claim_ids"]) == {"c1", "c2"}
    assert audit.missing == []


def test_generic_stamp_words_still_leave_the_gap() -> None:
    unit = _unit(_MATERIAL_QUESTION)
    claim = _claim(
        "申请时需提供三级甲等医院的检查证明，需要盖章。"
    )
    audit = _audit(unit, [claim])
    assert audit.units[0]["status"] == "generation_missing"
    assert any("盖章" in item for item in audit.missing)
