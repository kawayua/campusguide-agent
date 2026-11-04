"""Bounded compatibility regressions for coverage duration and target asks."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    _TARGET_AGENCY_ASK,
    _TARGET_LOCATION_ASK,
    CoverageAudit,
    CoverageChecker,
    _property_supported,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_DOC_TITLE = "示例事项管理办法"


def _duration_props(question: str):
    return [
        prop
        for prop in _requested_properties(question)
        if prop.kind == "duration"
    ]


def _claim(claim_id: str, text: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{_DOC_TITLE}#1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=_DOC_TITLE,
                article_no="第一条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/x",
            )
        ],
    )


def _unit(question: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        subject="",
        objects=[],
        version=None,
        requested_aspects=[question],
    )


def _audit(
    unit: QuestionUnit,
    claims: list[Claim],
    *,
    status: str = "answered",
    claim_ids: list[str] | None = None,
) -> CoverageAudit:
    verdict = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": (
                    [claim.claim_id for claim in claims]
                    if claim_ids is None
                    else claim_ids
                ),
                "missing_items": [],
            }
        ],
        "unanswered_questions": [],
    }
    provider = FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    return CoverageChecker(provider).check(
        unit.original_span, [unit], claims, [], []
    )


def test_duration_anchor_keeps_attached_level_identity() -> None:
    props = _duration_props("本科生入学多久可以转专业？")
    assert len(props) == 1
    assert props[0].core == "本科生入学"
    assert props[0].action == "转专业"
    assert _property_supported(props[0], "本科生入学满一学期。")
    assert not _property_supported(props[0], "研究生入学满一学期。")


def test_generic_student_prefix_still_drops_without_event_anchor() -> None:
    props = _duration_props("学生考前提前多久到场？")
    assert len(props) == 1
    assert props[0].core == "考前"
    assert props[0].action == "到场"
    assert _property_supported(props[0], "考前提前30分钟到场。")
    assert not _property_supported(props[0], "考前提前30分钟离场。")


def test_handling_method_is_not_a_location_ask() -> None:
    method = "逾期如何处理？"
    assert _TARGET_LOCATION_ASK.search(method) is None
    assert _TARGET_AGENCY_ASK.search(method) is None

    props = _requested_properties(
        "示例事项又应在开学几周内备案，逾期如何处理？"
    )
    assert [prop.kind for prop in props] == ["duration"]


def test_real_where_asks_keep_location_and_agency_support() -> None:
    assert _TARGET_LOCATION_ASK.search("补办学生证在何处办理？") is not None
    assert _TARGET_LOCATION_ASK.search("校园卡在哪里办理？") is not None
    assert _TARGET_AGENCY_ASK.search("材料报送哪里？") is not None
    assert _TARGET_AGENCY_ASK.search("材料报送何处？") is not None

    location = _requested_properties("补办学生证在何处办理？")
    assert any(
        prop.kind == "target" and prop.direction == "location"
        for prop in location
    )
    agency = _requested_properties("材料报送哪里？")
    assert any(
        prop.kind == "target" and prop.direction == "agency"
        for prop in agency
    )
    reported = _requested_properties("材料报送何处？")
    assert any(prop.kind == "target" for prop in reported)


def test_complete_handling_answer_is_required_not_dropped() -> None:
    question = "示例事项又应在开学几周内备案，逾期如何处理？"
    unit = _unit(question)

    complete = _audit(
        unit,
        [_claim("c1", "开学3周内备案，逾期未备案的按规定处理。")],
        status="generation_missing",
        claim_ids=[],
    )
    assert complete.units[0]["status"] == "answered"
    assert complete.units[0]["claim_ids"] == ["c1"]

    deleted = _audit(
        unit,
        [_claim("c1", "开学3周内备案。")],
        status="generation_missing",
        claim_ids=[],
    )
    assert deleted.units[0]["status"] == "generation_missing"
    assert any("逾期" in item for item in deleted.missing)
