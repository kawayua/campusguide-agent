from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from campusguide.agents.planner import Planner, PlannerContext
from campusguide.flows.catalog import MCPFlowCatalog
from campusguide.mcp.client import MCPPolicyMetaClient

ROOT = Path(__file__).resolve().parents[2]


def _flow_payload(flow_id: str) -> dict:
    path = ROOT / "configs" / "flows" / f"{flow_id}.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload


class StubMeta:
    def __init__(self) -> None:
        self.list_calls = 0
        self.get_calls = 0

    def list_flows(self, *, audience=None, as_of=None):
        self.list_calls += 1
        return {
            "flows": [
                {"flow_id": "scholarship-application"},
                {"flow_id": "recommendation-eligibility"},
                {"flow_id": "enrollment-certificate"},
            ],
            "count": 3,
        }

    def get_flow(self, flow_id, *, as_of=None):
        self.get_calls += 1
        return _flow_payload(flow_id)

    def get_calendar(self, *, start=None, end=None, audience=None):
        return {"events": [], "count": 0}


def test_mcp_flow_catalog_reads_full_flow_and_caches() -> None:
    meta = StubMeta()
    catalog = MCPFlowCatalog(meta)
    flows = catalog.list_flows(as_of=date(2026, 10, 25))
    assert len(flows) == 3
    assert meta.get_calls == 3
    catalog.list_flows(as_of=date(2026, 10, 25))
    assert meta.get_calls == 3
    assert meta.list_calls == 1
    flow = catalog.get_flow("scholarship-application", as_of=date(2026, 10, 25))
    assert flow is not None
    assert flow.slots


def test_mcp_flow_catalog_unknown_flow_returns_none() -> None:
    meta = StubMeta()

    def get_flow(flow_id, *, as_of=None):
        raise KeyError(flow_id)

    catalog = MCPFlowCatalog(meta)
    meta.get_flow = get_flow  # type: ignore[method-assign]
    assert catalog.get_flow("missing", as_of=date(2026, 10, 25)) is None


def test_planner_works_with_mcp_catalog() -> None:
    catalog = MCPFlowCatalog(StubMeta())
    planner = Planner(catalog, llm_enabled=False)
    plan = planner.plan(PlannerContext(message="数信学院大二申请国奖", as_of=date(2026, 9, 30)))
    assert plan.flow_id == "scholarship-application"
    assert plan.needs_clarification is True


def test_meta_client_flow_methods_send_expected_arguments() -> None:
    client = object.__new__(MCPPolicyMetaClient)
    calls: list[tuple[str, dict]] = []

    def call(tool, arguments=None):
        calls.append((tool, arguments or {}))
        return {"data": {"flows": [], "count": 0, "events": []}}

    client.call = call  # type: ignore[method-assign]
    client.list_flows(as_of="2026-10-25")
    client.get_flow("scholarship-application", as_of="2026-10-25")
    client.get_calendar(start="2027-01-01", end="2027-02-01")
    assert calls[0] == ("list_flows", {"audience": None, "as_of": "2026-10-25"})
    assert calls[1] == (
        "get_flow",
        {"flow_id": "scholarship-application", "as_of": "2026-10-25"},
    )
    assert calls[2] == (
        "get_calendar",
        {"start": "2027-01-01", "end": "2027-02-01", "audience": None},
    )
