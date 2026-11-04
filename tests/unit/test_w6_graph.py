from __future__ import annotations

from datetime import date

from campusguide.agents.graph import (
    build_graph,
    route_after_advisor,
    route_after_flow,
    route_after_planner,
    route_after_retriever,
    route_after_verifier,
)
from campusguide.agents.plan import Plan, QueryPlanItem
from campusguide.agents.providers import ChatProviderError
from campusguide.domain.enums import BlockType, GraphDecision, Intent
from tests.w6_helpers import (
    FakeSearchClient,
    article,
    claims_id_json,
    claims_json,
    make_runner,
    supported_json,
)


def quote() -> str:
    return article("national-aid-undergrad@v2021#第九条").text[:70]


def nodes_of(state) -> list[str]:
    return [item["node"] for item in state.get("trace") or []]


def test_curriculum_question_uses_section_block_types() -> None:
    runner = make_runner()
    state, _ = runner._prepare(
        "block-types-curriculum",
        "对于2024年入学的计算机科学与技术的学生来讲 我们的人工智能选修组有什么科目可以进行选择",
    )
    assert state["block_types"] == [BlockType.ARTICLE, BlockType.SECTION]

    state, _ = runner._prepare(
        "block-types-policy",
        "申请国家奖学金需要满足哪些条件？",
    )
    assert state["block_types"] == [BlockType.ARTICLE, BlockType.SECTION]


def test_policy_answer_has_complete_trace() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke("p1", "申请国家奖学金需要满足哪些条件？", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "ANSWERED"
    assert nodes_of(result) == ["planner", "retriever", "advisor", "verifier", "finalize"]
    assert result["claims"]
    assert "结论要点" in result["final_answer"]


def test_multi_query_reports_uncovered_subquestion() -> None:
    client = FakeSearchClient(
        by_query={
            "申请国家奖学金需要满足哪些条件": ["national-aid-undergrad@v2021#第九条"],
            "申请国家奖学金 奖励标准是多少": [],
        }
    )
    runner = make_runner(
        search_client=client,
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke(
        "p2", "申请国家奖学金需要满足哪些条件？奖励标准是多少？", as_of=date(2026, 10, 25)
    )
    assert "奖励标准是多少" in result["final_answer"]
    assert "未检索到" in result["final_answer"]
    assert result["coverage"]["q2"] == []


def test_revise_loop_only_rewrites_failed_claim() -> None:
    item = article("national-aid-undergrad@v2021#第九条")
    bad = claims_json(item.article_id, "不在原文中的连续片段但是够长")
    good = claims_id_json("source_1_body")
    runner = make_runner(
        advisor_responses=[bad, good],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke("p3", "申请国家奖学金需要满足哪些条件？", as_of=date(2026, 10, 25))
    assert result["revision_count"] == 1
    assert result["outcome"]["code"] == "ANSWERED"
    assert [item["node"] for item in result["trace"]].count("advisor") == 2
    assert [item["node"] for item in result["trace"]].count("verifier") == 2


def test_semantic_contradicted_after_max_revise_abstains() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[
            '{"labels":[{"claim_id":"c1","label":"contradicted","detail":"范围相反"}]}'
        ],
        max_revise=0,
    )
    result = runner.invoke("p4", "申请国家奖学金需要满足哪些条件？", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "UNVERIFIABLE"
    assert "语义" in result["final_answer"] or "核对" in result["final_answer"]


def test_semantic_unavailable_strict_abstains_with_handoff() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[ChatProviderError("down")],
        semantic_mode="strict",
    )
    result = runner.invoke("p5", "申请国家奖学金需要满足哪些条件？", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "UNVERIFIABLE"
    assert result["handoff"]["department"]
    assert "学校" in result["final_answer"]


def test_semantic_unavailable_allow_with_warning_renders_notice() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[ChatProviderError("down")],
        semantic_mode="allow_with_warning",
    )
    result = runner.invoke("p6", "申请国家奖学金需要满足哪些条件？", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["semantic_unchecked"] is True
    assert "未完成语义核对" in result["final_answer"]


def test_flow_only_closed_does_not_call_advisor() -> None:
    runner = make_runner(advisor_responses=[])
    result = runner.invoke("f1", "国家奖学金怎么申请", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["closed"] is True
    assert result["checklist"]
    assert "已截止" in result["final_answer"]
    assert "advisor" not in nodes_of(result)


def test_mixed_answer_has_checklist_and_policy_sections() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke("m1", "国家奖学金怎么申请，金额是多少", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["checklist"]
    assert "**办理路径" in result["final_answer"]
    assert "**政策说明" in result["final_answer"]


def test_clarify_then_fill_slot_renders_checklist() -> None:
    runner = make_runner(advisor_responses=[])
    first = runner.invoke("c1", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert first["outcome"]["code"] == "CLARIFY"
    assert first["pending_slot"] == "has_record"
    second = runner.invoke("c1", "没有处分", as_of=date(2026, 9, 30))
    assert second["outcome"]["code"] == "ANSWERED"
    assert second["flow_status"] == "open"
    assert "数信学院：国家奖学金申请" in second["final_answer"]


def test_declining_slot_moves_to_generic_path_with_handoff() -> None:
    runner = make_runner(advisor_responses=[])
    first = runner.invoke("d1", "我要开在读证明", as_of=date(2026, 10, 25))
    assert first["outcome"]["code"] == "CLARIFY"
    second = runner.invoke("d1", "跳过", as_of=date(2026, 10, 25))
    assert second["outcome"]["code"] in {"ANSWERED", "PARTIAL_ANSWER"}
    assert "人工入口" in second["final_answer"]
    assert second["pending_slot"] is None


def test_topic_change_while_slot_pending_answers_policy() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    first = runner.invoke("t1", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert first["outcome"]["code"] == "CLARIFY"
    second = runner.invoke("t1", "国家奖学金的金额是多少？", as_of=date(2026, 9, 30))
    assert second["flow_id"] is None
    assert "结论要点" in second["final_answer"]


def test_session_isolation() -> None:
    runner = make_runner(advisor_responses=[])
    runner.invoke("iso-a", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    other = runner.invoke("iso-b", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert other["outcome"]["code"] == "CLARIFY"
    assert other["slots"]
    assert other["pending_slot"] == "has_record"


def test_ttl_expires_old_thread() -> None:
    from datetime import UTC, datetime, timedelta

    runner = make_runner(advisor_responses=[])
    runner.invoke("ttl", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    runner._last_seen["ttl"] = datetime.now(UTC) - timedelta(hours=25)
    fresh = runner.invoke("ttl", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert fresh["turn_id"] == 1
    assert fresh["outcome"]["code"] == "CLARIFY"


def test_reset_clears_thread() -> None:
    runner = make_runner(advisor_responses=[])
    runner.invoke("r1", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    runner.reset("r1")
    fresh = runner.invoke("r1", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert fresh["turn_id"] == 1
    assert fresh["outcome"]["code"] == "CLARIFY"


def test_guard_privacy_skips_retrieval() -> None:
    client = FakeSearchClient()
    runner = make_runner(search_client=client, advisor_responses=[])
    result = runner.invoke("g1", "帮我查一下我的学号和成绩排名", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "PRIVACY_REQUEST"
    assert client.calls == []
    assert nodes_of(result) == ["planner", "finalize"]


def test_no_evidence_abstains() -> None:
    runner = make_runner(search_client=FakeSearchClient(default=[]), advisor_responses=[])
    result = runner.invoke("n1", "火星殖民有什么规定？", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "NO_EVIDENCE"
    assert "暂时无法作答" in result["final_answer"]


def test_tool_failure_maps_to_tool_unavailable() -> None:
    client = FakeSearchClient(fail_queries={"国奖条件"}, default=[])
    runner = make_runner(search_client=client, advisor_responses=[])
    result = runner.invoke("e1", "国奖条件", as_of=date(2026, 10, 25))
    assert result["outcome"]["code"] == "TOOL_UNAVAILABLE"
    assert "检索工具不可用" in result["final_answer"]


class FakeNodes:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.verify_index = 0

    def planner(self, state):
        self.calls.append("planner")
        plan = Plan(
            intent=Intent.POLICY_QA,
            queries=[QueryPlanItem(query_id="q1", text="国奖条件")],
        )
        return {"plan": plan, "trace": [{"node": "planner"}], "clarification": None}

    def flow(self, state):  # pragma: no cover - policy 路径不经过
        self.calls.append("flow")
        return {}

    def retriever(self, state):
        self.calls.append("retriever")
        return {"evidence": [object()], "coverage": {}, "trace": [{"node": "retriever"}]}

    def advisor(self, state):
        self.calls.append("advisor")
        return {"drafts": [object()], "trace": [{"node": "advisor"}]}

    def verifier(self, state):
        self.calls.append("verifier")
        self.verify_index += 1
        if self.verify_index == 1:
            return {
                "decision": GraphDecision.REVISE,
                "failed_claim_ids": ["c1"],
                "trace": [{"node": "verifier"}],
            }
        return {
            "decision": GraphDecision.PASS,
            "claims": [],
            "verification_decision": None,
            "trace": [{"node": "verifier"}],
        }

    def finalize(self, state):
        self.calls.append("finalize")
        return {
            "final_answer": "ok",
            "outcome": {"code": "ANSWERED"},
            "trace": [{"node": "finalize"}],
        }


def test_build_graph_revise_loop_and_routing_helpers() -> None:
    fake = FakeNodes()
    graph = build_graph(fake)
    result = graph.invoke(
        {
            "session_id": "x",
            "question": "国奖条件",
            "as_of": date(2026, 10, 25),
            "trace": [],
        }
    )
    assert result["final_answer"] == "ok"
    assert fake.calls == [
        "planner",
        "retriever",
        "advisor",
        "verifier",
        "advisor",
        "verifier",
        "finalize",
    ]
    plan = Plan(intent=Intent.POLICY_QA, queries=[QueryPlanItem(query_id="q1", text="x")])
    assert route_after_planner({"plan": plan}) == "retriever"
    assert route_after_planner({"plan": Plan(needs_clarification=True)}) == "finalize"
    assert route_after_flow({"plan": plan}) == "retriever"
    assert route_after_retriever({"evidence": [object()]}) == "advisor"
    assert route_after_retriever({"decision": GraphDecision.ABSTAIN}) == "finalize"
    assert route_after_advisor({"decision": GraphDecision.ABSTAIN}) == "finalize"
    assert route_after_verifier({"decision": GraphDecision.REVISE}) == "advisor"
    assert route_after_verifier({"decision": GraphDecision.CLARIFY}) == "planner"
    assert route_after_verifier({"decision": GraphDecision.PASS}) == "finalize"
