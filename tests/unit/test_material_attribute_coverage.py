"""Material-attribute coverage: hospital grade and named stamp are required fields."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker, _requested_properties
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_QUESTION = "本科生因患病申请转出学校，应提供什么级别医院的检查证明以及何种盖章？"
_TITLE = "本科学生转学工作细则"
_NO = "第三条"
_COMPLETE = (
    "本科生因患病申请转出学校，需提供二级甲等医院的检查证明"
    "（需盖病情诊断专用章）及相关病例。"
)


def _verdict(
    status: str,
    claim_ids: list[str] | None = None,
    missing: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "units": [
                {
                    "unit_id": "u1",
                    "status": status,
                    "claim_ids": claim_ids or [],
                    "missing_items": list(missing or []),
                }
            ],
            "unanswered_questions": [],
        },
        ensure_ascii=False,
    )


def _unit(span: str = _QUESTION) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=[span],
    )


def _claim(text: str, claim_id: str = "c1") -> Claim:
    citation = Citation(
        article_id=f"{_TITLE}#{_NO}",
        part_no=1,
        quote=text,
        quote_start=0,
        quote_end=len(text),
        doc_title=_TITLE,
        article_no=_NO,
        effective_from=date(2024, 1, 1),
        source_url="https://example.edu.cn/synthetic",
    )
    return Claim(claim_id=claim_id, text=text, citations=[citation])


def _audit(
    unit: QuestionUnit,
    claim: Claim,
    *,
    status: str = "generation_missing",
    missing: list[str] | None = None,
):
    checker = CoverageChecker(
        FakeChatProvider(
            [
                _verdict(
                    status,
                    [claim.claim_id] if status == "answered" else [],
                    missing,
                )
            ]
        )
    )
    return checker.check(unit.original_span, [unit], [claim], [], [])


def test_material_attributes_answer_with_paraphrased_provide() -> None:
    audit = _audit(_unit(), _claim(_COMPLETE), status="answered")
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_material_scaffold_gap_recovers_to_answered() -> None:
    audit = _audit(
        _unit(), _claim(_COMPLETE), status="generation_missing", missing=["应提供"]
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_missing_hospital_grade_keeps_the_gap() -> None:
    claim = _claim(
        "本科生因患病申请转出学校，需提供检查证明"
        "（需盖病情诊断专用章）及相关病例。"
    )
    audit = _audit(_unit(), claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("级别" in item for item in audit.missing)


def test_missing_named_stamp_keeps_the_gap() -> None:
    claim = _claim(
        "本科生因患病申请转出学校，需提供二级甲等医院的检查证明及相关病例。"
    )
    audit = _audit(_unit(), claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("盖章" in item for item in audit.missing)


def test_generic_proof_and_stamp_words_do_not_answer() -> None:
    claim = _claim("本科生因患病申请转出学校，需要检查证明，需要盖章。")
    audit = _audit(_unit(), claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("级别" in item for item in audit.missing)
    assert any("盖章" in item for item in audit.missing)


def test_material_values_are_generic_not_hardcoded() -> None:
    claim = _claim(
        "本科生因患病申请转出学校，应提供一级甲等医院的检查证明"
        "（需盖教务专用章）及相关病例。"
    )
    audit = _audit(_unit(), claim, status="answered")
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_grade_only_question_does_not_require_a_stamp() -> None:
    span = "本科生因患病申请转出学校，应提供什么级别医院的检查证明？"
    claim = _claim("本科生因患病申请转出学校，需提供二级甲等医院的检查证明。")
    audit = _audit(_unit(span), claim, status="answered")
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_stamp_only_question_does_not_require_a_grade() -> None:
    span = "本科生因患病申请转出学校，检查证明需要何种盖章？"
    claim = _claim(
        "本科生因患病申请转出学校，需提供检查证明（需盖病情诊断专用章）。"
    )
    audit = _audit(_unit(span), claim, status="answered")
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_material_attribute_binds_the_asked_document() -> None:
    claim = _claim(
        "本科生因患病申请转出学校，需提供二级甲等医院的诊断证明"
        "（需盖病情诊断专用章）及相关病例。"
    )
    audit = _audit(_unit(), claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.missing


def test_true_yes_no_provision_asks_create_no_material_attribute() -> None:
    for question in (
        "本科生因患病申请转出学校，是否需要提供检查证明？",
        "本科生因患病申请转出学校，需要盖章吗？",
    ):
        properties = _requested_properties(question)
        assert not any(
            prop.kind == "material_attribute" for prop in properties
        ), question
