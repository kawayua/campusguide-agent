"""Regression coverage for solitary recognized structured requests."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_TITLE = "高校学生管理规定"
_PROVINCE_Q = "再向省教育厅申诉的期限是多少？"
_PROVINCE_FULL = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起15日内，"
    "可以向广东省教育厅提出书面申诉。"
)
_PROVINCE_NO_NUMBER = (
    "学生对复查决定有异议的，可以向广东省教育厅提出书面申诉。"
)
_PROVINCE_FOREIGN = (
    "学生对复查决定有异议的，在学校复查决定书送达之日起15日内，"
    "可以向学校学生申诉处理委员会提出书面申诉。"
)
_AUTHORITY_Q = "退学须在10日内由哪个部门审批？"
_AUTHORITY_FULL = "退学须在10日内由教务处审批。"
_AUTHORITY_NO_NUMBER = "退学由教务处审批。"


def _unit(span: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=[span],
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
                article_no="第十条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/policy",
            )
        ],
    )


def _audit(question: str, claim: Claim, *, optimistic: bool):
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "answered" if optimistic else "generation_missing",
                "claim_ids": [claim.claim_id] if optimistic else [],
                "missing_items": [] if optimistic else [question],
            }
        ],
        "unanswered_questions": [],
    }
    provider = FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    return CoverageChecker(provider).check(question, [_unit(question)], [claim], [], [])


def _assert_answered(question: str, claim_text: str) -> None:
    claim = _claim(claim_text)
    for optimistic in (True, False):
        audit = _audit(question, claim, optimistic=optimistic)
        row = audit.units[0]
        assert row["status"] == "answered", (optimistic, row, audit.missing)
        assert row["claim_ids"] == ["c1"]
        assert row["missing_items"] == []


def _assert_missing(question: str, claim_text: str) -> None:
    claim = _claim(claim_text)
    for optimistic in (True, False):
        audit = _audit(question, claim, optimistic=optimistic)
        row = audit.units[0]
        assert row["status"] == "generation_missing", (optimistic, row)
        assert row["claim_ids"] == []


def test_single_province_appeal_deadline_is_answered() -> None:
    _assert_answered(_PROVINCE_Q, _PROVINCE_FULL)


def test_single_province_missing_number_or_foreign_stage_is_rejected() -> None:
    for text in (_PROVINCE_NO_NUMBER, _PROVINCE_FOREIGN):
        _assert_missing(_PROVINCE_Q, text)


def test_short_single_authority_request_is_wired() -> None:
    _assert_answered(_AUTHORITY_Q, _AUTHORITY_FULL)
    _assert_missing(_AUTHORITY_Q, _AUTHORITY_NO_NUMBER)
