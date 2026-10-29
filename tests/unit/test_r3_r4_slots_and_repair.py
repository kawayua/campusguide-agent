"""R3/R4/R8 guard cases: per-object evidence slots, per-unit repair, trace fields."""

from __future__ import annotations

import json

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.graph import MAX_COVERAGE_REPAIR_ROUNDS
from campusguide.agents.plan import Plan
from campusguide.agents.planner import Planner
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.retriever import _Candidate, GraphRetriever
from campusguide.domain.models import ChecklistItem
from campusguide.verification.claims import ClaimValidator
from campusguide.agents.advisor import CitationDraft, ClaimDraft
from tests.w2_helpers import make_article, make_evidence


def _plan_with_two_units() -> Plan:
    from campusguide.config import load_organization
    from campusguide.agents.question_units import decompose_question
    from campusguide.retrieval.profile_expand import ProfileLexicon
    from pathlib import Path

    org = load_organization(Path("configs/organization.yaml"))
    lexicon = ProfileLexicon.from_organization(org)
    question = (
        "2026级农业工程中农业电气化与自动化方向，与2025—2026级工业工程与管理专业的"
        "培养目标和课程设置有什么区别？"
    )
    plan = Plan(queries=[])
    plan.question_units = decompose_question(question, lexicon)
    return plan


def test_planner_protects_a_query_for_each_uncovered_unit() -> None:
    from campusguide.agents.plan import QueryPlanItem

    plan = _plan_with_two_units()
    plan.queries = [
        QueryPlanItem(query_id="q1", text="2026级农业工程 培养目标 课程设置", filters={})
    ]
    planner = Planner.__new__(Planner)
    planner._profile_lexicon = type("L", (), {"major_terms": {}, "college_terms": {}})()
    planner._protect_unit_queries(plan, "培养目标和课程设置有什么区别")
    covered = {unit_id for query in plan.queries for unit_id in query.unit_ids}
    assert {unit.unit_id for unit in plan.question_units} <= covered
    assert len(plan.queries) <= 6


def test_retriever_keeps_two_slots_per_unit_in_comparisons() -> None:
    agro = make_evidence(
        make_article("agro@v2026#a", doc_title="农业工程研究生培养方案", text="农业工程培养目标。"),
        score=0.9,
    )
    agro2 = make_evidence(
        make_article("agro@v2026#b", doc_title="农业工程研究生培养方案", text="农业工程课程设置。"),
        score=0.8,
    )
    industrial = make_evidence(
        make_article("ie@v2026#a", doc_title="工业工程与管理研究生培养方案", text="工业工程培养目标。"),
        score=0.7,
    )
    industrial2 = make_evidence(
        make_article("ie@v2026#b", doc_title="工业工程与管理研究生培养方案", text="工业工程课程设置。"),
        score=0.6,
    )
    retriever = GraphRetriever.__new__(GraphRetriever)
    retriever._max_evidence = 2
    retriever._max_per_doc = 2
    retriever._token_budget = 10_000
    plan = _plan_with_two_units()
    candidates = {
        ("agro@v2026", 0): _Candidate(evidence=agro, queries={"q1"}),
        ("agro@v2026", 1): _Candidate(evidence=agro2, queries={"q1"}),
        ("ie@v2026", 0): _Candidate(evidence=industrial, queries={"q1"}),
        ("ie@v2026", 1): _Candidate(evidence=industrial2, queries={"q1"}),
    }
    selected = retriever._select(candidates, {}, plan=plan)
    titles = " ".join(item.article.doc_title for item in selected)
    assert "农业工程" in titles and "工业工程" in titles


def test_repair_rounds_are_bounded_and_per_unit() -> None:
    assert MAX_COVERAGE_REPAIR_ROUNDS == 2
    text = open("src/campusguide/agents/graph.py", encoding="utf-8").read()
    assert "query_budget = 3 if round_index == 0 else 2" in text
    assert '"coverage_repair_count": round_index + 1' in text


def test_coverage_failure_is_recorded_as_service_error() -> None:
    class Boom(FakeChatProvider):
        def complete(self, messages, *, temperature=0.2, max_tokens=1200):
            from campusguide.agents.providers import ChatProviderError

            raise ChatProviderError("boom", status_code=503, kind="server", attempts=3)

    from campusguide.agents.plan import QuestionUnit

    unit = QuestionUnit(
        unit_id="u1",
        original_span="农学专业实践环节",
        subject="农学",
        objects=["农学"],
        requested_aspects=["实践课程"],
    )
    audit = CoverageChecker(Boom([])).check("农学专业实践环节？", [unit], [], [], [])
    assert audit.unavailable is True
    assert audit.error_kind == "server"
    assert audit.error_attempts == 3
    assert audit.units[0]["status"] == "verification_failed"


def test_graph_trace_carries_provider_fields() -> None:
    text = open("src/campusguide/agents/graph.py", encoding="utf-8").read()
    assert "provider_error=getattr(self._planner, \"last_provider_error\"" in text
    assert '"kind": audit.error_kind' in text
    assert "provider_error=exc.as_dict()" in text

def test_supplement_does_not_reset_the_revise_budget() -> None:
    """049: 补查重置 revision_count 会让“生成→核对”重复到 LangGraph 上限。"""
    text = open("src/campusguide/agents/graph.py", encoding="utf-8").read()
    supplement = text.split("def supplement", 1)[1].split("def ", 1)[0]
    assert '"revision_count": 0' not in supplement