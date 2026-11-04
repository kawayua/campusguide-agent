"""Coverage wiring for scoped study-duration fields."""

from __future__ import annotations

import json
from datetime import date

import pytest

from campusguide.agents.coverage import (
    CoverageChecker,
    _drop_property_echoes,
    _property_supported,
    _question_clauses,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatResponse
from campusguide.domain.models import Citation, Claim
from campusguide.retrieval.profile_expand import ProfileLexicon

_YEAR_QUESTION = "三年制与六年制专业通常最长学习几年，休学创业最长年限分别多少？"
_MODE_QUESTION = "全日制、非全日制硕士学制和最长学习年限分别几年？"
_MODAL_YEAR_QUESTION = "三年制与六年制专业通常最长可以学习几年，创业最长年限分别多少"
_LOCAL_UNKNOWN_TEXT = "三年制最长学习7年（创业最长年限尚不明确），六年制最长学习9年。"


class _FakeProvider:
    def __init__(self, content: str):
        self._content = content

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse:
        return ChatResponse(content=self._content)


def _claim(claim_id: str, text: str, *, doc_title: str = "培养方案") -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"a-{claim_id}",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=doc_title,
                article_no="第一条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu/rule",
            )
        ],
    )


def _unit(
    question: str,
    *,
    aspects: list[str] | None = None,
    **overrides: object,
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        requested_aspects=aspects if aspects is not None else [question],
        **overrides,
    )


def _check(
    question: str,
    unit: QuestionUnit,
    claims: list[Claim],
    *,
    status: str = "answered",
    missing: list[str] | None = None,
    unanswered: list[str] | None = None,
    lexicon: ProfileLexicon | None = None,
) -> dict:
    payload = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": [claim.claim_id for claim in claims],
                "missing_items": list(missing or []),
            }
        ],
        "unanswered_questions": list(unanswered or []),
    }
    checker = CoverageChecker(
        _FakeProvider(json.dumps(payload, ensure_ascii=False)),
        profile_lexicon=lexicon,
    )
    audit = checker.check(question, [unit], claims, [], [])
    return audit.units[0]


def _year_claims() -> list[Claim]:
    return [
        _claim("c-year-ordinary-3", "三年制专业最长学习年限为8年。"),
        _claim("c-year-ordinary-6", "六年制专业最长学习年限为10年。"),
        _claim("c-year-startup-3", "三年制专业休学创业最长年限为9年。"),
        _claim("c-year-startup-6", "六年制专业休学创业最长年限为11年。"),
    ]


def _modal_year_claims() -> list[Claim]:
    return [
        _claim("c-modal-ordinary-3", "三年制专业最长学习年限为7年。"),
        _claim("c-modal-ordinary-6", "六年制专业最长学习年限为9年。"),
        _claim("c-modal-startup-3", "三年制专业创业最长年限为10年。"),
        _claim("c-modal-startup-6", "六年制专业创业最长年限为12年。"),
    ]


def _mode_claims() -> list[Claim]:
    return [
        _claim("c-mode-basic", "全日制和非全日制基本学制均为4年。"),
        _claim("c-mode-max", "全日制和非全日制最长学习年限均为6年。"),
    ]


@pytest.mark.parametrize(
    ("question", "conditions"),
    [
        (_YEAR_QUESTION, {"三年制", "六年制"}),
        (_MODE_QUESTION, {"全日制", "非全日制"}),
    ],
)
def test_requested_properties_adds_four_fields_and_removes_legacy_ask(
    question: str, conditions: set[str]
) -> None:
    properties = _requested_properties(question)
    study = [prop for prop in properties if prop.kind == "study_duration"]
    assert len(study) == 4
    assert {prop.core for prop in study} == conditions
    assert not [prop for prop in properties if prop.kind in ("duration", "bound")]


def test_study_only_clauses_are_echoes_but_fee_clause_survives() -> None:
    properties = _requested_properties(_YEAR_QUESTION)
    clauses, _topics = _question_clauses(_YEAR_QUESTION)
    assert clauses
    assert _drop_property_echoes(clauses, properties) == []

    fee_question = _MODE_QUESTION.removesuffix("？") + "，学费怎么收？"
    fee_properties = _requested_properties(fee_question)
    fee_clauses, _topics = _question_clauses(fee_question)
    kept = _drop_property_echoes(fee_clauses, fee_properties)
    assert kept
    assert all("学费" in clause for clause in kept)
    assert not any(
        term in clause for clause in kept for term in ("学制", "年限", "全日制")
    )


def test_all_four_fields_recover_from_pessimistic_generation_missing() -> None:
    claims = _year_claims()
    row = _check(
        _YEAR_QUESTION,
        _unit(_YEAR_QUESTION),
        claims,
        status="generation_missing",
        missing=["休学创业最长年限未提供", _YEAR_QUESTION],
        unanswered=[_YEAR_QUESTION],
    )
    assert row["status"] == "answered"
    assert set(row["claim_ids"]) == {claim.claim_id for claim in claims}
    assert row["missing_items"] == []


@pytest.mark.parametrize(
    "missing_id",
    [
        "c-year-ordinary-3",
        "c-year-ordinary-6",
        "c-year-startup-3",
        "c-year-startup-6",
    ],
)
def test_each_missing_year_field_stays_generation_missing(missing_id: str) -> None:
    claims = [claim for claim in _year_claims() if claim.claim_id != missing_id]
    row = _check(_YEAR_QUESTION, _unit(_YEAR_QUESTION), claims)
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert row["missing_items"]


def test_study_field_keeps_local_uncertainty_and_rejects_unknown_sibling() -> None:
    properties = _requested_properties(_YEAR_QUESTION)
    ordinary = next(
        prop
        for prop in properties
        if prop.kind == "study_duration"
        and prop.core == "三年制"
        and prop.action == "ordinary_max"
    )
    startup = next(
        prop
        for prop in properties
        if prop.kind == "study_duration"
        and prop.core == "三年制"
        and prop.action == "startup_max"
    )
    assert _property_supported(ordinary, _LOCAL_UNKNOWN_TEXT)
    assert not _property_supported(startup, _LOCAL_UNKNOWN_TEXT)


def test_study_field_rejects_outer_question_echo() -> None:
    ordinary = next(
        prop
        for prop in _requested_properties(_YEAR_QUESTION)
        if prop.kind == "study_duration"
        and prop.core == "三年制"
        and prop.action == "ordinary_max"
    )
    assert not _property_supported(ordinary, _YEAR_QUESTION)


def test_study_values_are_not_lent_from_citation_metadata() -> None:
    claim = _claim(
        "c-metadata-only",
        "详见附件。",
        doc_title=(
            "三年制最长学习7年，六年制最长学习9年，"
            "三年制创业最长10年，六年制创业最长12年"
        ),
    )
    row = _check(_MODAL_YEAR_QUESTION, _unit(_MODAL_YEAR_QUESTION), [claim])
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert {
        "三年制最长学习年限",
        "六年制最长学习年限",
        "三年制创业最长学习年限",
        "六年制创业最长学习年限",
    } <= set(row["missing_items"])


def test_modal_study_clauses_are_echoes() -> None:
    properties = _requested_properties(_MODAL_YEAR_QUESTION)
    study = [prop for prop in properties if prop.kind == "study_duration"]
    assert len(study) == 4
    clauses, _topics = _question_clauses(_MODAL_YEAR_QUESTION)
    assert clauses
    assert _drop_property_echoes(clauses, properties) == []


def test_modal_study_question_recovers_all_four_fields() -> None:
    claims = _modal_year_claims()
    row = _check(
        _MODAL_YEAR_QUESTION,
        _unit(_MODAL_YEAR_QUESTION),
        claims,
        status="generation_missing",
        missing=[_MODAL_YEAR_QUESTION],
        unanswered=[_MODAL_YEAR_QUESTION],
    )
    assert row["status"] == "answered"
    assert set(row["claim_ids"]) == {claim.claim_id for claim in claims}
    assert row["missing_items"] == []


@pytest.mark.parametrize(
    ("missing_id", "expected"),
    [
        ("c-modal-ordinary-3", "三年制最长学习年限"),
        ("c-modal-ordinary-6", "六年制最长学习年限"),
        ("c-modal-startup-3", "三年制创业最长学习年限"),
        ("c-modal-startup-6", "六年制创业最长学习年限"),
    ],
)
def test_missing_modal_year_field_stays_generation_missing(
    missing_id: str, expected: str
) -> None:
    claims = [
        claim for claim in _modal_year_claims() if claim.claim_id != missing_id
    ]
    row = _check(_MODAL_YEAR_QUESTION, _unit(_MODAL_YEAR_QUESTION), claims)
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert expected in row["missing_items"]


def test_shared_joint_scope_proves_all_mode_fields() -> None:
    claims = _mode_claims()
    row = _check(
        _MODE_QUESTION,
        _unit(_MODE_QUESTION, aspects=["学制"]),
        claims,
        status="generation_missing",
        missing=[_MODE_QUESTION],
        unanswered=[_MODE_QUESTION],
    )
    assert row["status"] == "answered"
    assert set(row["claim_ids"]) == {claim.claim_id for claim in claims}


def test_missing_shared_maximum_leaves_both_maximum_fields_missing() -> None:
    claims = [_mode_claims()[0]]
    row = _check(_MODE_QUESTION, _unit(_MODE_QUESTION, aspects=["学制"]), claims)
    assert row["status"] == "generation_missing"
    assert "全日制最长学习年限" in row["missing_items"]
    assert "非全日制最长学习年限" in row["missing_items"]
    assert not any("学制" in item for item in row["missing_items"])


def test_part_time_substring_never_answers_full_time_fields() -> None:
    claims = [
        _claim("c-part-time", "非全日制基本学制为4年，非全日制最长学习年限为6年。")
    ]
    row = _check(_MODE_QUESTION, _unit(_MODE_QUESTION, aspects=["学制"]), claims)
    assert row["status"] == "generation_missing"
    assert "全日制学制" in row["missing_items"]
    assert "全日制最长学习年限" in row["missing_items"]
    assert not any("非全日制" in item for item in row["missing_items"])


def test_quoted_and_title_numbers_do_not_fill_study_fields() -> None:
    claims = [
        _claim(
            "c-quoted",
            "“全日制和非全日制基本学制均为4年”",
            doc_title="全日制和非全日制基本学制均为4年",
        ),
        _claim(
            "c-title-only",
            "详见附件。",
            doc_title="全日制和非全日制最长学习年限均为6年",
        ),
    ]
    row = _check(_MODE_QUESTION, _unit(_MODE_QUESTION, aspects=["学制"]), claims)
    assert row["status"] == "generation_missing"
    assert "全日制学制" in row["missing_items"]
    assert "非全日制最长学习年限" in row["missing_items"]


@pytest.mark.parametrize("scope", ["year", "program", "class"])
def test_existing_scope_gates_reject_study_fields(scope: str) -> None:
    question = _MODE_QUESTION
    overrides: dict[str, object] = {}
    lexicon = None
    if scope == "year":
        claims = [
            _claim("c-scope", "2024级全日制和非全日制基本学制均为4年。"),
            _claim("c-scope-max", "2024级全日制和非全日制最长学习年限均为6年。"),
        ]
        overrides["version"] = "2025级"
    elif scope == "program":
        claims = [
            _claim("c-scope", "软件工程专业全日制和非全日制基本学制均为4年。"),
            _claim("c-scope-max", "软件工程专业全日制和非全日制最长学习年限均为6年。"),
        ]
        overrides["objects"] = ["计算机科学与技术"]
    else:
        claims = [
            _claim("c-scope", "普通班全日制和非全日制基本学制均为4年。"),
            _claim("c-scope-max", "普通班全日制和非全日制最长学习年限均为6年。"),
        ]
        overrides["class_type"] = "innovation"
        lexicon = ProfileLexicon(
            college_terms={},
            major_terms={},
            class_terms={
                "innovation": ("创新班",),
                "ordinary": ("普通班",),
            },
        )
    row = _check(
        question,
        _unit(question, aspects=["学制"], **overrides),
        claims,
        lexicon=lexicon,
    )
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []


def test_mixed_mode_and_missing_fee_keeps_fee_gap() -> None:
    question = _MODE_QUESTION.removesuffix("？") + "，学费怎么收？"
    row = _check(question, _unit(question), _mode_claims())
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert "学费" in row["missing_items"]
    assert not any("学制" in item or "年限" in item for item in row["missing_items"])
