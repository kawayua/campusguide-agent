"""W6 图门面下的 W5 三流程回归（closed/partial/槽位更正）。"""

from __future__ import annotations

from datetime import date

from tests.w6_helpers import make_runner


def test_three_flows_are_routed_through_graph() -> None:
    runner = make_runner(advisor_responses=[])
    scholarship = runner.invoke("rf1", "国家奖学金怎么申请", as_of=date(2026, 10, 25))
    assert scholarship["flow_id"] == "scholarship-application"
    assert scholarship["closed"] is True

    recommendation = runner.invoke("rf2", "推免资格怎么申请", as_of=date(2026, 10, 25))
    assert recommendation["flow_id"] == "recommendation-eligibility"

    certificate = runner.invoke("rf3", "在读证明怎么开", as_of=date(2026, 10, 25))
    assert certificate["flow_id"] == "enrollment-certificate"
    assert certificate["outcome"]["code"] == "CLARIFY"
    assert certificate["pending_slot"] == "level"


def test_partial_flow_and_slot_fill() -> None:
    runner = make_runner(advisor_responses=[])
    first = runner.invoke("rf4", "我想申请奖学金", as_of=date(2026, 10, 18))
    assert first["flow_id"] == "scholarship-application"
    assert first["flow_status"] == "partial"
    assert first["outcome"]["code"] == "CLARIFY"
    filled = runner.invoke(
        "rf4",
        "数信，大二，没受过处分，国家励志奖学金，已认定困难",
        as_of=date(2026, 10, 18),
    )
    assert filled["flow_status"] == "open"
    assert filled["outcome"]["code"] == "ANSWERED"
    assert "国家励志奖学金申请" in filled["final_answer"]


def test_graph_slot_correction_does_not_repeat_answered_slots() -> None:
    runner = make_runner(advisor_responses=[])
    runner.invoke("rf5", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    filled = runner.invoke("rf5", "没有处分", as_of=date(2026, 9, 30))
    assert filled["outcome"]["code"] == "ANSWERED"
    corrected = runner.invoke("rf5", "我现在大三了", as_of=date(2026, 9, 30))
    assert corrected["slots"]["grade"].value == 3
    assert "是否受过纪律处分" not in corrected["final_answer"]
