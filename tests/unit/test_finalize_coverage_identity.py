"""Finalize coverage identity remap regressions (duplicate/dropped claim IDs)."""

from __future__ import annotations

from datetime import date

import campusguide.agents.graph as graph_module
from campusguide.agents.graph import GraphNodes
from campusguide.agents.plan import Clarification, Plan
from campusguide.domain.enums import OutcomeCode, VerificationDecision
from campusguide.domain.models import (
    ChecklistItem,
    Citation,
    Claim,
    Evidence,
    FlowDefinition,
)
from campusguide.qa.merge import MergeNote, MergeResult, merge_claims
from tests.w6_helpers import article as load_article
from tests.w6_helpers import catalog as load_catalog

AS_OF = date(2026, 11, 2)
QUESTION = "国家奖学金金额是多少？"


def _citation(article_id: str, text: str) -> Citation:
    return Citation(
        article_id=article_id,
        quote=text,
        quote_start=0,
        quote_end=len(text),
        doc_title="测试制度",
        article_no="第一条",
        effective_from=date(2026, 1, 1),
        source_url=f"https://www.scau.edu.cn/policy/{article_id}",
    )


def _claim(claim_id: str, text: str, *, article_id: str = "article-amount") -> Claim:
    return Claim(claim_id=claim_id, text=text, citations=[_citation(article_id, text)])


def _row(
    unit_id: str,
    *,
    status: str = "answered",
    claim_ids: tuple[str, ...] = (),
    missing_items: tuple[str, ...] = (),
) -> dict:
    return {
        "unit_id": unit_id,
        "status": status,
        "claim_ids": list(claim_ids),
        "missing_items": list(missing_items),
    }


def _state(
    claims: list[Claim],
    unit_coverage: list[dict],
    *,
    coverage_missing: list[str] | None = None,
    coverage_checked: bool = True,
) -> dict:
    return {
        "question": QUESTION,
        "as_of": AS_OF,
        "claims": list(claims),
        "coverage_checked": coverage_checked,
        "coverage_missing": list(coverage_missing or []),
        "unit_coverage": [dict(row) for row in unit_coverage],
        "verification_decision": VerificationDecision.PASS,
        "trace": [],
        "checklist": [],
        "flow_definition": None,
    }


def _nodes() -> GraphNodes:
    return GraphNodes(
        planner=object(),
        retriever=object(),
        advisor=object(),
        verifier=object(),
        catalog=object(),
        today=AS_OF,
    )


def _flow_fixture() -> tuple[FlowDefinition, list[ChecklistItem], list[Evidence]]:
    flow_catalog = load_catalog()
    flow = flow_catalog.get_flow("enrollment-certificate", as_of=AS_OF)
    assert flow is not None
    evaluation = flow_catalog.evaluate(flow, {}, as_of=AS_OF)
    item = next(
        item
        for item in evaluation.checklist
        if item.status != "not_applicable" and item.citations
    )
    hydrated = [
        Evidence(
            article=load_article("national-aid-undergrad@v2021#第九条").model_copy(
                update={"article_id": citation}
            ),
            source="flow",
        )
        for citation in item.citations
    ]
    return flow, [item], hydrated


def _state_with_flow(
    claims: list[Claim],
    unit_coverage: list[dict],
    *,
    flow: FlowDefinition,
    checklist: list[ChecklistItem],
    evidence: list[Evidence],
) -> dict:
    state = _state(claims, unit_coverage)
    state.update(
        {
            "flow_definition": flow,
            "checklist": list(checklist),
            "evidence": list(evidence),
        }
    )
    return state


def test_duplicate_only_cited_id_is_remapped_to_retained_claim() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    result = _nodes().finalize(
        _state(
            claims=[_claim("c1", text), _claim("zz_duplicate", text)],
            unit_coverage=[_row("u1", claim_ids=("zz_duplicate",))],
        )
    )

    assert result["outcome"]["code"] == "ANSWERED"
    assert result["unit_coverage"] == [_row("u1", claim_ids=("c1",))]
    assert [claim.claim_id for claim in result["claims"]] == ["c1"]
    assert "zz_duplicate" not in result["answer_markdown"]


def test_two_units_referencing_same_duplicate_keep_both_answered() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    result = _nodes().finalize(
        _state(
            claims=[_claim("c1", text), _claim("zz_duplicate", text)],
            unit_coverage=[
                _row("u1", claim_ids=("zz_duplicate",)),
                _row("u2", claim_ids=("c1",)),
            ],
        )
    )

    assert result["outcome"]["code"] == "ANSWERED"
    assert [row["claim_ids"] for row in result["unit_coverage"]] == [["c1"], ["c1"]]
    assert [row["status"] for row in result["unit_coverage"]] == ["answered", "answered"]


def test_unknown_claim_id_is_not_kept_as_support() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    result = _nodes().finalize(
        _state(
            claims=[_claim("c1", text)],
            unit_coverage=[_row("u1", claim_ids=("ghost",))],
        )
    )

    row = result["unit_coverage"][0]
    assert row["claim_ids"] == []
    assert row["status"] == "generation_missing"
    assert result["outcome"]["code"] == "PARTIAL_ANSWER"
    assert [claim.claim_id for claim in result["claims"]] == ["c1"]


def test_real_missing_unit_forces_partial_even_when_missing_list_empty() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    state = _state(
        claims=[_claim("c1", text)],
        unit_coverage=[
            _row("u1", claim_ids=("c1",)),
            _row("u2", status="missing_evidence"),
        ],
        coverage_missing=[],
    )

    result = _nodes().finalize(state)

    assert result["outcome"]["code"] == "PARTIAL_ANSWER"
    assert result["unit_coverage"][1]["status"] == "missing_evidence"
    # Coverage can make the answer partial without rewriting the verifier decision.
    assert state["verification_decision"] is VerificationDecision.PASS


def test_stale_and_table_fragment_drops_are_never_mapped(monkeypatch) -> None:
    text = "国家奖学金金额为每人每年8000元。"

    def fake_merge(claims: list[Claim]) -> MergeResult:
        return MergeResult(
            claims=[claims[0]],
            notes=[
                MergeNote("stale_missing", "旧缺口表述已删除"),
                MergeNote("table_fragment", "表格碎片已删除"),
            ],
            dropped_claim_ids=["c_stale", "c_table"],
            replacement_claim_ids={},
        )

    monkeypatch.setattr(graph_module, "merge_claims", fake_merge)
    result = _nodes().finalize(
        _state(
            claims=[_claim("c1", text)],
            unit_coverage=[
                _row("u_stale", claim_ids=("c_stale",)),
                _row("u_table", claim_ids=("c_table",)),
                _row("u_ok", claim_ids=("c1",)),
            ],
        )
    )

    rows = {row["unit_id"]: row for row in result["unit_coverage"]}
    assert rows["u_stale"]["claim_ids"] == []
    assert rows["u_table"]["claim_ids"] == []
    assert rows["u_stale"]["status"] == "generation_missing"
    assert rows["u_table"]["status"] == "generation_missing"
    assert rows["u_ok"]["claim_ids"] == ["c1"]
    assert result["outcome"]["code"] == "PARTIAL_ANSWER"


def test_validated_flow_id_is_preserved() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    flow, checklist, evidence = _flow_fixture()
    flow_id = f"flow:{checklist[0].step_id}"
    result = _nodes().finalize(
        _state_with_flow(
            [_claim("c1", text)],
            [_row("u1", claim_ids=(flow_id,))],
            flow=flow,
            checklist=checklist,
            evidence=evidence,
        )
    )

    row = result["unit_coverage"][0]
    assert row["claim_ids"] == [flow_id]
    assert row["status"] == "answered"
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["checklist"] == checklist


def test_flow_prefix_without_backing_step_is_not_kept_as_support() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    flow, _, evidence = _flow_fixture()
    result = _nodes().finalize(
        _state_with_flow(
            [_claim("c1", text)],
            [_row("u1", claim_ids=("flow:ghost",))],
            flow=flow,
            checklist=[],
            evidence=evidence,
        )
    )

    row = result["unit_coverage"][0]
    assert row["claim_ids"] == []
    assert row["status"] == "generation_missing"
    assert result["outcome"]["code"] == "PARTIAL_ANSWER"
    assert result["checklist"] == []
    assert [claim.claim_id for claim in result["claims"]] == ["c1"]


def test_flow_support_requires_definition_rendering_citations_and_status() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    flow, checklist, evidence = _flow_fixture()
    item = checklist[0]
    variants = [
        item.model_copy(update={"status": "not_applicable"}),
        item.model_copy(update={"citations": []}),
        item.model_copy(update={"citations": ["missing-flow-citation@v1#x"]}),
        item.model_copy(update={"step_id": "ghost"}),
    ]

    for variant in variants:
        flow_id = f"flow:{variant.step_id}"
        result = _nodes().finalize(
            _state_with_flow(
                [_claim("c1", text)],
                [_row("u1", claim_ids=(flow_id,))],
                flow=flow,
                checklist=[variant],
                evidence=evidence,
            )
        )
        row = result["unit_coverage"][0]
        assert row["claim_ids"] == []
        assert row["status"] == "generation_missing"
        assert result["outcome"]["code"] == "PARTIAL_ANSWER"


def test_complementary_facts_are_both_retained_and_cited() -> None:
    amount = _claim("c1", "国家奖学金金额为每人每年8000元。")
    quota = _claim(
        "c2",
        "国家奖学金名额为每年10人。",
        article_id="article-quota",
    )
    result = _nodes().finalize(
        _state(
            claims=[amount, quota],
            unit_coverage=[
                _row("u1", claim_ids=("c1",)),
                _row("u2", claim_ids=("c2",)),
            ],
        )
    )

    assert result["outcome"]["code"] == "ANSWERED"
    assert [claim.claim_id for claim in result["claims"]] == ["c1", "c2"]
    assert [row["claim_ids"] for row in result["unit_coverage"]] == [["c1"], ["c2"]]


def test_duplicate_merge_never_records_self_replacement() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    merged = merge_claims([_claim("c1", text), _claim("c1", text)])

    assert merged.replacement_claim_ids == {}
    assert [claim.claim_id for claim in merged.claims] == ["c1"]


def test_guard_abstain_renders_no_claims() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    state = _state(
        claims=[_claim("c1", text)],
        unit_coverage=[_row("u1", claim_ids=("c1",))],
    )
    state["plan"] = Plan(outcome=OutcomeCode.OUT_OF_SCOPE, guard_message="只回答本校公开制度。")

    result = _nodes().finalize(state)

    assert result["outcome"]["code"] == "OUT_OF_SCOPE"
    assert result["claims"] == []
    assert result["checklist"] == []
    assert text not in result["answer_markdown"]
    row = result["unit_coverage"][0]
    assert row["claim_ids"] == []
    assert row["status"] == "verification_failed"


def test_pure_clarify_renders_no_claims_or_checklist_support() -> None:
    text = "国家奖学金金额为每人每年8000元。"
    flow, checklist, evidence = _flow_fixture()
    state = _state_with_flow(
        [_claim("c1", text)],
        [_row("u1", claim_ids=("c1",))],
        flow=flow,
        checklist=checklist,
        evidence=evidence,
    )
    state["plan"] = Plan(
        needs_clarification=True,
        clarification=Clarification(reason="SLOT_MISSING", question="你是本科生还是研究生？"),
    )

    result = _nodes().finalize(state)

    assert result["outcome"]["code"] == "CLARIFY"
    assert result["claims"] == []
    assert result["checklist"] == []
    row = result["unit_coverage"][0]
    assert row["claim_ids"] == []
    assert row["status"] == "verification_failed"
