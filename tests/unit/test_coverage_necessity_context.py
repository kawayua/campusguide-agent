"""ND11: necessity/optionality asks must not become false coverage gaps."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    CoverageAudit,
    CoverageChecker,
    _clause_content_ok,
    _necessity_action,
)
from campusguide.agents.providers import ChatResponse
from campusguide.agents.question_units import decompose_question
from campusguide.domain.models import Citation, Claim

QUESTION = "本科新生第一学期课程由谁安排，需要自己选课吗？"
COMPLETE = (
    "本科新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划"
    "直接安排，不需要选课。"
)
EQUIVALENT_COMPLETE = (
    "新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划"
    "直接安排，不需要选课。"
)
ARRANGEMENT_ONLY = (
    "本科新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划直接安排。"
)


class _PessimisticProvider:
    def __init__(self, unit_id: str, missing: tuple[str, ...]):
        self.unit_id = unit_id
        self.missing = missing

    def complete(self, messages, *, temperature: float = 0.2, max_tokens: int = 1200):
        rows = [
            {
                "unit_id": self.unit_id,
                "status": "missing_evidence",
                "claim_ids": [],
                "missing_items": list(self.missing),
            }
        ]
        return ChatResponse(
            content=json.dumps(
                {"units": rows, "unanswered_questions": []}, ensure_ascii=False
            )
        )


def _claim(text: str, *, doc_title: str = "新生入学须知") -> Claim:
    return Claim(
        claim_id="c1",
        text=text,
        citations=[
            Citation(
                article_id="a1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=doc_title,
                article_no="第一条",
                effective_from=date(2025, 9, 1),
                source_url="https://example.edu/notice",
            )
        ],
    )


def _audit(
    text: str,
    *,
    doc_title: str = "新生入学须知",
    missing: tuple[str, ...] = ("需要自己选课",),
) -> CoverageAudit:
    units = decompose_question(QUESTION)
    assert len(units) == 1
    provider = _PessimisticProvider(units[0].unit_id, missing)
    audit = CoverageChecker(provider).check(
        QUESTION, units, [_claim(text, doc_title=doc_title)], [], []
    )
    assert not audit.unavailable
    return audit


def test_necessity_request_shapes() -> None:
    assert _necessity_action("需要自己选课吗") == "选课"
    # 归一化后的实际子句已经丢掉“吗”，仍要识别同一动作。
    assert _necessity_action("需要自己选课") == "选课"
    assert _necessity_action("是否需要自己选课") == "选课"
    assert _necessity_action("需不需要自己选课") == "选课"
    assert _necessity_action("要不要自己选课") == "选课"
    assert _necessity_action("用不用自己选课") == "选课"
    assert _necessity_action(QUESTION) == "选课"
    assert _necessity_action("由谁安排") is None
    assert _necessity_action("需要多长时间") is None
    assert _necessity_action("需要准备的材料") is None
    assert _necessity_action("需要自己提供什么") is None
    assert _necessity_action("需要自己提供") is None


def test_payment_and_submission_keep_their_existing_properties() -> None:
    assert _necessity_action("是否需要缴费") is None
    assert _necessity_action("需要提交材料吗") is None


def test_need_and_no_need_branches_answer_the_same_action() -> None:
    assert _clause_content_ok("需要自己选课吗", "需要自己选课")
    assert _clause_content_ok("需要自己选课吗", "不需要选课")
    assert _clause_content_ok("需要自己选课吗", "无需自行选课")
    assert _clause_content_ok("需要自己选课吗", "不需要学生自己选课")
    assert _clause_content_ok("需要自己选课吗", "新生入学后需要自己选课")


def test_echo_unknown_and_wrong_action_do_not_answer() -> None:
    assert not _clause_content_ok("需要自己选课吗", "学校说明需要自己选课吗")
    assert not _clause_content_ok("需要自己选课吗", "是否需要选课，尚未明确")
    assert not _clause_content_ok("需要自己选课吗", "不需要退选课")
    assert not _clause_content_ok("需要自己选课吗", "不需要自己听课")
    assert not _clause_content_ok("需要自己选课吗", "选课")


def test_other_businesses_are_parsed_without_hardcoded_objects() -> None:
    assert _clause_content_ok("需要自己选宿舍吗", "不需要自己选宿舍")
    assert _clause_content_ok("是否需要自行申请", "无需自行申请")
    assert not _clause_content_ok("需要自己选宿舍吗", "不需要选课")
    assert not _clause_content_ok("需要自己选课吗", "不需要选宿舍")


def test_full_checker_recovers_verified_necessity_context() -> None:
    audit = _audit(COMPLETE)
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.units[0]["missing_items"] == []
    assert audit.missing == []


def test_full_checker_recovers_equivalent_short_subject() -> None:
    audit = _audit(EQUIVALENT_COMPLETE)
    assert audit.units[0]["status"] == "answered"


def test_missing_arrangement_fact_is_not_blanket_answered() -> None:
    audit = _audit("第一学期的课程不需要选课。")
    assert audit.units[0]["status"] != "answered"
    assert audit.missing


def test_missing_no_need_fact_is_not_blanket_answered() -> None:
    audit = _audit(ARRANGEMENT_ONLY)
    assert audit.units[0]["status"] != "answered"
    assert audit.missing


def test_source_title_metadata_cannot_supply_the_necessity_answer() -> None:
    audit = _audit(ARRANGEMENT_ONLY, doc_title="新生入学须知：不需要选课")
    assert audit.units[0]["status"] != "answered"
    assert audit.missing


def test_wrong_operation_or_question_echo_in_claim_is_not_answered() -> None:
    wrong_action = _audit(
        "本科新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划"
        "直接安排，不需要自己听课。"
    )
    assert wrong_action.units[0]["status"] != "answered"

    question_echo = _audit(
        "本科新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划"
        "直接安排，学校说明需要自己选课吗。"
    )
    assert question_echo.units[0]["status"] != "answered"
