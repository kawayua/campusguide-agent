from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import Client

from campusguide.flows.calendar import load_calendar
from campusguide.flows.registry import FlowRegistry
from campusguide.mcp.policy_meta_server import (
    JsonlMetaBackend,
    PolicyMetaService,
    build_meta_server,
)

ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path) -> PolicyMetaService:
    meta = tmp_path / "documents.jsonl"
    meta.write_text("", encoding="utf-8")
    registry = FlowRegistry.from_directory(ROOT / "configs" / "flows")
    calendar = load_calendar(ROOT / "configs" / "calendar.yaml")
    return PolicyMetaService(
        JsonlMetaBackend(meta),
        flow_registry=registry,
        calendar_events=calendar,
    )


def _call(server, tool: str, arguments: dict):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    result = asyncio.run(run())
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    assert isinstance(payload, dict)
    return payload


def test_flow_and_calendar_tools_are_registered(tmp_path: Path) -> None:
    server = build_meta_server(_service(tmp_path))

    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    names = {tool.name for tool in asyncio.run(run())}
    assert {"list_flows", "get_flow", "get_calendar"} <= names


def test_list_flows_and_closed_scholarship(tmp_path: Path) -> None:
    server = build_meta_server(_service(tmp_path))
    listed = _call(server, "list_flows", {"as_of": "2026-10-25"})
    assert listed["ok"] is True
    assert listed["data"]["count"] == 3
    assert {item["flow_id"] for item in listed["data"]["flows"]} == {
        "scholarship-application",
        "recommendation-eligibility",
        "enrollment-certificate",
    }

    flow = _call(
        server,
        "get_flow",
        {"flow_id": "scholarship-application", "as_of": "2026-10-25"},
    )
    assert flow["ok"] is True
    assert flow["data"]["batch_status"] == "closed"
    assert flow["data"]["batch_label"] == "2026 年本科生国家奖助学金批次"
    assert flow["data"]["steps"]


def test_get_flow_open_and_not_found(tmp_path: Path) -> None:
    server = build_meta_server(_service(tmp_path))
    flow = _call(
        server,
        "get_flow",
        {"flow_id": "scholarship-application", "as_of": "2026-09-30"},
    )
    assert flow["data"]["batch_status"] == "open"
    missing = _call(server, "get_flow", {"flow_id": "nope", "as_of": "2026-10-25"})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "NOT_FOUND"


def test_get_calendar_window_and_audience(tmp_path: Path) -> None:
    server = build_meta_server(_service(tmp_path))
    calendar = _call(
        server,
        "get_calendar",
        {"start": "2027-01-01", "end": "2027-02-28", "audience": "undergraduate"},
    )
    assert calendar["ok"] is True
    ids = {item["event_id"] for item in calendar["data"]["events"]}
    assert "final-exam-autumn-2026" in ids
    assert "winter-vacation-2027" in ids
    assert "teaching-spring-2027" in ids
    assert "register-returning-2026" not in ids


def test_calendar_invalid_range(tmp_path: Path) -> None:
    server = build_meta_server(_service(tmp_path))
    result = _call(server, "get_calendar", {"start": "2027-02-01", "end": "2027-01-01"})
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"
