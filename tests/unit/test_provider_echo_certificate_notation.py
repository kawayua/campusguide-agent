"""Provider population echoes and certificate value notation stay semantic."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    CoverageChecker,
    _property_gaps,
    _question_clauses,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_PROVIDER_QUESTION = (
    "我办理英文成绩单时，在校生与毕业生的原始成绩单分别由哪里提供？"
)
_CERT_TYPE_QUESTION = "微专业完成后发什么证？"
_CERT_YESNO_QUESTION = "微专业完成后会发证吗？"
_PROVIDER_TITLE = "英文成绩单办理说明"
_CERT_TITLE = "微专业证书管理办法"


def _fallback_unit(span: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=[span],
    )


def _certificate_unit(span: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=["证书"],
    )


def _claim(text: str, claim_id: str = "c1", title: str = _PROVIDER_TITLE) -> Claim:
    citation = Citation(
        article_id=f"{title}#第一条",
        part_no=1,
        quote=text,
        quote_start=0,
        quote_end=len(text),
        doc_title=title,
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
        _provider_properties(), claims, _fallback_unit(_PROVIDER_QUESTION)
    )
    return [prop.required[0] for prop in missing if prop.required]


# --- Population prefix survives clause normalization ------------------------


def test_clause_normalization_keeps_the_population_prefix() -> None:
    clauses, topics = _question_clauses(_PROVIDER_QUESTION)
    assert "在校生" in clauses
    assert "校生" not in clauses
    assert topics == ["我办理英文成绩单时"]


def test_split_population_claims_still_join() -> None:
    unit = _fallback_unit(_PROVIDER_QUESTION)
    claims = [
        _claim("在校学生的原始成绩单由各学院提供。", "c1"),
        _claim("已毕业学生的原始成绩单由校档案馆提供。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "answered"
    assert set(audit.units[0]["claim_ids"]) == {"c1", "c2"}
    assert audit.missing == []


def test_deleting_one_population_claim_keeps_the_gap() -> None:
    unit = _fallback_unit(_PROVIDER_QUESTION)
    claims = [_claim("在校学生的原始成绩单由各学院提供。")]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "generation_missing"
    assert any("由哪里提供" in item for item in audit.missing)
    assert _missing_provider_anchors(claims) == ["毕业"]


def test_reversed_population_claim_still_joins() -> None:
    unit = _fallback_unit(_PROVIDER_QUESTION)
    claims = [
        _claim(
            "已毕业学生的原始成绩单由校档案馆提供，"
            "在校学生的原始成绩单由各学院提供。"
        )
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_swapped_population_cannot_mask_the_missing_group() -> None:
    unit = _fallback_unit(_PROVIDER_QUESTION)
    claims = [
        _claim("在校学生的原始成绩单由各学院提供。", "c1"),
        _claim("新生的原始成绩单由校档案馆提供。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "generation_missing"
    assert _missing_provider_anchors(claims) == ["毕业"]


def test_generic_departments_still_leave_the_provider_gap() -> None:
    unit = _fallback_unit(_PROVIDER_QUESTION)
    claims = [
        _claim("在校学生的原始成绩单由有关部门提供。", "c1"),
        _claim("已毕业学生的原始成绩单由相关部门提供。", "c2"),
    ]
    audit = _audit(unit, claims)
    assert audit.units[0]["status"] == "generation_missing"
    assert set(_missing_provider_anchors(claims)) == {"在校", "毕业"}


# --- Certificate type names, modifiers and absence --------------------------


def _cert_type_audit(text: str):
    return _audit(
        _certificate_unit(_CERT_TYPE_QUESTION),
        [_claim(text, title=_CERT_TITLE)],
    )


def test_named_certificate_answers_type() -> None:
    audit = _cert_type_audit("微专业完成后颁发结业证书。")
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_quoted_named_certificate_answers_type() -> None:
    audit = _cert_type_audit("微专业完成后颁发《微专业结业证书》。")
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_governance_heading_is_not_an_awarded_name() -> None:
    audit = _cert_type_audit("《结业证书管理办法》规定，完成后发放证书。")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("证书" in item for item in audit.missing)


def test_generic_certificate_modifiers_do_not_name_a_type() -> None:
    templates = (
        "微专业完成后发放相应证书。",
        "微专业完成后发放相关证书。",
        "微专业完成后发放一种证书。",
        "微专业完成后发放某种证书。",
        "微专业完成后发放上述证书。",
        "微专业完成后发放学校统一制作的证书。",
    )
    for text in templates:
        audit = _cert_type_audit(text)
        assert audit.units[0]["status"] == "generation_missing", text
        assert audit.units[0]["claim_ids"] == [], text


def test_definite_absence_answers_type_as_none() -> None:
    audit = _cert_type_audit("微专业完成后不颁发任何证书。")
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_pending_or_unknown_absence_does_not_answer_type() -> None:
    templates = (
        "微专业完成后证书尚未发放。",
        "微专业完成后证书未公布。",
        "微专业完成后所发证书不详。",
    )
    for text in templates:
        audit = _cert_type_audit(text)
        assert audit.units[0]["status"] == "generation_missing", text
        assert audit.units[0]["claim_ids"] == [], text


def test_type_ask_rejects_generic_issuance_without_a_name() -> None:
    audit = _cert_type_audit("微专业完成后会发证。")
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []


def test_yes_no_ask_accepts_generic_definite_issuance() -> None:
    templates = (
        "微专业完成后会发证。",
        "微专业完成后不发证。",
        "微专业完成后颁发证书。",
    )
    for text in templates:
        audit = _audit(
            _certificate_unit(_CERT_YESNO_QUESTION),
            [_claim(text, title=_CERT_TITLE)],
        )
        assert audit.units[0]["status"] == "answered", text
        assert audit.missing == [], text
