"""Regression coverage for appeal deadlines split across a temporal comma."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.coverage_actions import requirement_answered
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_TITLE = "高校学生申诉处理办法"
_QUESTION = (
    "本科生不服退学处理，向校内委员会申诉、校内复查、"
    "再向省教育厅申诉的期限各是多少？"
)
_UNITS = [
    QuestionUnit(
        unit_id="u1",
        original_span="向校内委员会申诉的期限是多少？",
        subject="",
        objects=[],
        requested_aspects=["向校内委员会申诉的期限是多少？"],
    ),
    QuestionUnit(
        unit_id="u2",
        original_span="校内复查的期限是多少？",
        subject="",
        objects=[],
        requested_aspects=["校内复查的期限是多少？"],
    ),
    QuestionUnit(
        unit_id="u3",
        original_span="再向省教育厅申诉的期限是多少？",
        subject="",
        objects=[],
        requested_aspects=["再向省教育厅申诉的期限是多少？"],
    ),
]

_FULL_CLAIM = (
    "学生应在退学决定书送达之日起8日内向学校学生申诉处理委员会提出书面申诉；"
    "学生申诉处理委员会在接到书面申诉之日起16日内作出复查结论并告知申诉人；"
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内，"
    "可以向广东省教育厅提出书面申诉。"
)
_VERIFIED_FULL = (
    "学生应在退学决定书送达之日起10日内向学校学生申诉处理委员会提出书面申诉；"
    "学生申诉处理委员会在接到书面申诉之日起15日内作出复查结论并告知申诉人；"
    "学生对复查决定有异议的，在学校复查决定书送达之日起15日内，"
    "可以向广东省教育厅提出书面申诉。"
)
_NO_PROVINCE_TIME = (
    "学生应在退学决定书送达之日起8日内向学校学生申诉处理委员会提出书面申诉；"
    "学生申诉处理委员会在接到书面申诉之日起16日内作出复查结论并告知申诉人；"
    "学生对复查决定有异议的，可以向广东省教育厅提出书面申诉。"
)
_WRONG_STAGE = _FULL_CLAIM.replace(
    "可以向广东省教育厅提出书面申诉",
    "可以向学校学生申诉处理委员会提出书面申诉",
)
_BORROWED = (
    "学生应在退学决定书送达之日起8日内向学校学生申诉处理委员会提出书面申诉；"
    "学校在接到申诉之日起16日内作出复查结论，学生可以向广东省教育厅提出书面申诉。"
)
_PERIOD = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内。"
    "可以向广东省教育厅提出书面申诉。"
)
_SEMICOLON = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内；"
    "可以向广东省教育厅提出书面申诉。"
)
_NONADJACENT = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内，"
    "学生可以补办手续，向广东省教育厅提出书面申诉。"
)
_REVERSE = (
    "学生对复查决定有异议的，可以向广东省教育厅提出书面申诉，"
    "在学校复查决定书送达之日起21日内。"
)
_SAME_CLAUSE = (
    "学生对复查决定有异议的，可以在学校复查决定书送达之日起21日内"
    "向广东省教育厅提出书面申诉。"
)
_UNCERTAIN = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内，"
    "可以向广东省教育厅提出书面申诉但期限尚不明确。"
)
_NEGATED = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内，"
    "不得向广东省教育厅提出书面申诉。"
)
_NO_NUMBER = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起，"
    "可以向广东省教育厅提出书面申诉。"
)
_WRONG_ACTION = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起21日内，"
    "向广东省教育厅提出复查申请。"
)


def _claim(text: str) -> Claim:
    return Claim(
        claim_id="c1",
        text=text,
        citations=[
            Citation(
                article_id="a1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=_TITLE,
                article_no="第二十条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/policy",
            )
        ],
    )


def _audit(units: list[QuestionUnit], claim: Claim, *, optimistic: bool):
    units = list(units)
    verdict_units = []
    for unit in units:
        verdict_units.append(
            {
                "unit_id": unit.unit_id,
                "status": "answered" if optimistic else "generation_missing",
                "claim_ids": [claim.claim_id] if optimistic else [],
                "missing_items": [] if optimistic else [unit.original_span],
            }
        )
    provider = FakeChatProvider(
        [json.dumps({"units": verdict_units, "unanswered_questions": []}, ensure_ascii=False)]
    )
    return CoverageChecker(provider).check(_QUESTION, units, [claim], [], [])


def _entry(audit, unit_id: str) -> dict:
    return next(entry for entry in audit.units if entry["unit_id"] == unit_id)


def _assert_answered(audit, unit_id: str) -> None:
    entry = _entry(audit, unit_id)
    assert entry["status"] == "answered"
    assert entry["claim_ids"] == ["c1"]
    assert entry["missing_items"] == []


def test_full_claim_answers_all_three_units() -> None:
    claim = _claim(_FULL_CLAIM)

    for optimistic in (True, False):
        audit = _audit(_UNITS, claim, optimistic=optimistic)
        for unit_id in ("u1", "u2", "u3"):
            _assert_answered(audit, unit_id)


def test_actual_verified_claim_answers_school_and_province_units() -> None:
    assert requirement_answered(_UNITS[0].original_span, _VERIFIED_FULL) is True
    assert requirement_answered(_UNITS[1].original_span, _VERIFIED_FULL) is True
    assert requirement_answered(_UNITS[2].original_span, _VERIFIED_FULL) is True
    assert requirement_answered(_QUESTION, _VERIFIED_FULL) is True


def test_deleting_province_deadline_leaves_only_that_unit_missing() -> None:
    claim = _claim(_NO_PROVINCE_TIME)

    for optimistic in (True, False):
        audit = _audit(_UNITS, claim, optimistic=optimistic)
        _assert_answered(audit, "u1")
        _assert_answered(audit, "u2")
        entry = _entry(audit, "u3")
        assert entry["status"] == "generation_missing"
        assert entry["claim_ids"] == []


def test_wrong_stage_and_school_review_time_are_not_borrowed() -> None:
    for text in (_WRONG_STAGE, _BORROWED):
        claim = _claim(text)
        for optimistic in (True, False):
            audit = _audit(_UNITS, claim, optimistic=optimistic)
            entry = _entry(audit, "u3")
            assert entry["status"] == "generation_missing"
            assert entry["claim_ids"] == []


def test_strong_or_nonadjacent_boundaries_do_not_lend_deadlines() -> None:
    for text in (_PERIOD, _SEMICOLON, _NONADJACENT):
        assert requirement_answered(_UNITS[2].original_span, text) is False


def test_reverse_and_same_clause_temporal_relations_are_accepted() -> None:
    for text in (_REVERSE, _SAME_CLAUSE):
        assert requirement_answered(_UNITS[2].original_span, text) is True


def test_uncertain_negated_numberless_wrong_or_title_only_are_rejected() -> None:
    for text in (_UNCERTAIN, _NEGATED, _NO_NUMBER, _WRONG_ACTION, _TITLE):
        assert requirement_answered(_UNITS[2].original_span, text) is False
