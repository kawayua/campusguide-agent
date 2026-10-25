from __future__ import annotations

import json
from datetime import date

from campusguide.agents.plan import Plan
from campusguide.agents.planner import Planner, PlannerContext, build_planner_messages
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import Audience, Intent, OutcomeCode
from tests.w6_helpers import catalog


def plan_for(
    message: str,
    *,
    as_of: date = date(2026, 10, 25),
    planner: Planner | None = None,
    **context,
) -> Plan:
    planner = planner or Planner(catalog(), llm_enabled=False)
    return planner.plan(PlannerContext(message=message, as_of=as_of, **context))


def test_guard_privacy_out_of_scope_historical_and_empty() -> None:
    assert plan_for("帮我查一下我的学号和成绩排名").outcome is OutcomeCode.PRIVACY_REQUEST
    assert plan_for("隔壁学校的奖学金政策是什么？").outcome is OutcomeCode.OUT_OF_SCOPE
    assert plan_for("去年的转专业政策是什么？").outcome is OutcomeCode.HISTORICAL_NOT_SUPPORTED
    assert plan_for("   ").outcome is OutcomeCode.INVALID_REQUEST


def test_policy_fact_does_not_start_flow() -> None:
    plan = plan_for("申请国家奖学金需要满足哪些条件？")
    assert plan.intent is Intent.POLICY_QA
    assert plan.flow_id is None
    assert [item.text for item in plan.queries] == ["申请国家奖学金需要满足哪些条件"]


def test_procedure_starts_flow_and_closed_skips_slots() -> None:
    plan = plan_for("国家奖学金怎么申请")
    assert plan.intent is Intent.PROCEDURE_GUIDANCE
    assert plan.flow_id == "scholarship-application"
    assert plan.flow_status == "closed"
    assert plan.needs_clarification is False


def test_mixed_splits_policy_subquestion() -> None:
    plan = plan_for("国家奖学金怎么申请，金额是多少")
    assert plan.intent is Intent.MIXED
    assert plan.flow_id == "scholarship-application"
    assert [item.text for item in plan.queries] == ["金额是多少"]


def test_multi_question_split_and_limit_three() -> None:
    plan = plan_for("国家奖学金条件是什么？励志奖学金条件是什么？助学金条件是什么？还有别的吗？")
    assert plan.intent is Intent.POLICY_QA
    assert len(plan.queries) == 3
    assert plan.dropped_queries == ["还有别的吗"]
    assert [item.query_id for item in plan.queries] == ["q1", "q2", "q3"]


def test_open_flow_asks_missing_slot_and_slot_fill_recomputes() -> None:
    plan = plan_for("数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert plan.flow_id == "scholarship-application"
    assert plan.needs_clarification is True
    assert plan.clarification is not None
    assert plan.clarification.reason == "SLOT_MISSING"
    assert plan.pending_slot == "has_record"

    filled = plan_for(
        "没有处分",
        as_of=date(2026, 9, 30),
        active_flow_id=plan.flow_id,
        slots=plan.slots,
        pending_slot="has_record",
        asked_slots=plan.asked_slots,
    )
    assert filled.needs_clarification is False
    assert filled.flow_status == "open"


def test_clarify_limit_stops_asking() -> None:
    plan = plan_for(
        "你好",
        as_of=date(2026, 9, 30),
        active_flow_id="scholarship-application",
        clarify_count=3,
    )
    assert plan.flow_id == "scholarship-application"
    assert plan.needs_clarification is False


def test_declining_pending_slot_does_not_ask_again() -> None:
    plan = plan_for("我要开在读证明", as_of=date(2026, 10, 25))
    assert plan.needs_clarification is True
    declined = plan_for(
        "跳过",
        as_of=date(2026, 10, 25),
        active_flow_id=plan.flow_id,
        slots=plan.slots,
        pending_slot=plan.pending_slot,
        asked_slots=plan.asked_slots,
    )
    assert declined.needs_clarification is False
    assert "level" in declined.declined_slots


def test_topic_change_leaves_flow() -> None:
    plan = plan_for(
        "国家奖学金的金额是多少？",
        active_flow_id="scholarship-application",
        pending_flow_ids=[],
    )
    assert plan.flow_id is None
    assert plan.intent is Intent.POLICY_QA


def test_single_flow_match_asks_slot_not_ambiguity() -> None:
    plan = plan_for("我要申请奖学金", as_of=date(2026, 9, 30))
    assert plan.flow_id == "scholarship-application"
    assert plan.needs_clarification is True
    assert plan.clarification is not None
    assert plan.clarification.reason == "SLOT_MISSING"


def test_pending_flow_selection_by_index_or_name() -> None:
    flow_catalog = catalog()
    flows = flow_catalog.list_flows(as_of=date(2026, 9, 30))
    explicit = Planner(flow_catalog, llm_enabled=False).plan(
        PlannerContext(
            message="2",
            as_of=date(2026, 9, 30),
            pending_flow_ids=[flow.flow_id for flow in flows],
        )
    )
    assert explicit.flow_id == flows[1].flow_id
    assert explicit.intent is Intent.PROCEDURE_GUIDANCE


def test_audience_both_without_profile_clarifies() -> None:
    plan = plan_for("本科生和研究生在奖学金评定上有什么区别？")
    assert plan.needs_clarification is True
    assert plan.clarification is not None
    assert plan.clarification.reason == "AUDIENCE_UNKNOWN"
    assert plan.clarification.allowed_values == ["本科生", "研究生"]


def test_audience_clarify_follow_up_uses_original_question() -> None:
    flow_catalog = catalog()
    planner = Planner(flow_catalog, llm_enabled=False)
    first = planner.plan(
        PlannerContext(
            message="本科生和研究生在奖学金评定上有什么区别？",
            as_of=date(2026, 10, 25),
        )
    )
    assert first.needs_clarification is True
    second = planner.plan(
        PlannerContext(
            message="本科生",
            as_of=date(2026, 10, 25),
            pending_clarification=(
                first.clarification.model_dump(mode="json") if first.clarification else None
            ),
            pending_question="本科生和研究生在奖学金评定上有什么区别？",
        )
    )
    assert second.needs_clarification is False
    assert second.queries
    assert second.queries[0].filters.get("audience") == ["undergraduate"]


def test_llm_planner_rewrites_grounded_query() -> None:
    response = json.dumps(
        {
            "intent": "policy_qa",
            "flow_id": None,
            "queries": [
                {"text": "国奖金额", "route": "policy", "filters": {"version_mode": "current"}}
            ],
        },
        ensure_ascii=False,
    )
    planner = Planner(catalog(), provider=FakeChatProvider([response]), llm_enabled=True)
    plan = planner.plan(
        PlannerContext(message="它和国奖的金额分别是多少？", as_of=date(2026, 10, 25))
    )
    assert plan.planner_source == "llm"
    assert [item.text for item in plan.queries] == ["国奖金额"]


def test_llm_planner_rejects_hallucinated_year_and_unknown_flow() -> None:
    response = json.dumps(
        {
            "intent": "procedure_guidance",
            "flow_id": "not-a-flow",
            "queries": [
                {"text": "2023 年国奖条件", "route": "policy", "filters": {}},
            ],
        },
        ensure_ascii=False,
    )
    planner = Planner(catalog(), provider=FakeChatProvider([response]), llm_enabled=True)
    plan = planner.plan(PlannerContext(message="它和国奖的条件是什么？", as_of=date(2026, 10, 25)))
    # 未知 flow 被丢弃；加了原话没有的年份的 query 被拒；LLM 无可用 query 时回退规则。
    assert plan.flow_id is None
    assert plan.planner_source == "fallback"
    assert all("2023" not in item.text for item in plan.queries)


def test_llm_planner_invalid_json_repairs_once_then_falls_back() -> None:
    provider = FakeChatProvider(["not json", "still not json"])
    planner = Planner(catalog(), provider=provider, llm_enabled=True)
    plan = planner.plan(PlannerContext(message="它和国奖的条件是什么？", as_of=date(2026, 10, 25)))
    assert plan.planner_source == "fallback"
    assert plan.queries


def test_llm_planner_filters_unknown_values_dropped() -> None:
    response = json.dumps(
        {
            "intent": "policy_qa",
            "flow_id": None,
            "queries": [
                {
                    "text": "国奖条件",
                    "route": "policy",
                    "filters": {
                        "audience": ["undergraduate", "alien"],
                        "status": ["active", "burned"],
                        "version_mode": "future",
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    planner = Planner(catalog(), provider=FakeChatProvider([response]), llm_enabled=True)
    plan = planner.plan(PlannerContext(message="它和国奖的条件是什么？", as_of=date(2026, 10, 25)))
    assert plan.planner_source == "llm"
    filters = plan.queries[0].filters
    assert filters["audience"] == ["undergraduate"]
    assert filters["status"] == ["active"]
    assert filters["version_mode"] == "current"
    assert any(item.startswith("audience:alien") for item in plan.dropped_filters)


def test_planner_messages_include_flow_whitelist() -> None:
    flow_catalog = catalog()
    plan = plan_for("它和国奖的条件是什么？")
    messages = build_planner_messages(
        "它和国奖的条件是什么？",
        PlannerContext(message="它和国奖的条件是什么？", as_of=date(2026, 10, 25)),
        plan,
        flow_catalog.list_flows(as_of=date(2026, 10, 25)),
        Audience.UNDERGRADUATE,
        max_queries=3,
    )
    assert "scholarship-application" in messages[1]["content"]
    assert "只输出 JSON" in messages[1]["content"]
