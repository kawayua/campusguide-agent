from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from campusguide.flows.dsl import FlowLoadError, load_flow, load_flows


def make_flow(**overrides):
    payload = {
        "flow_id": "demo-flow",
        "version": "v2026",
        "title": "示例流程",
        "description": "用于测试",
        "audience": ["undergraduate"],
        "aliases": ["示例"],
        "effective_from": "2026-09-01",
        "effective_to": None,
        "slots": [
            {
                "name": "grade",
                "type": "integer",
                "required": True,
                "question": "几年级？",
                "why_needed": "判断资格",
                "priority": 10,
            }
        ],
        "prerequisites": [],
        "steps": [
            {
                "id": "apply",
                "title": "提交申请",
                "description": "按要求提交",
                "materials": ["申请表"],
                "deadline": "2026-10-10T09:00:00+08:00",
                "responsible_department": "学生工作部",
                "citations": ["doc@v1#第一条"],
                "when": "slots.grade >= 2",
            }
        ],
        "handoff": {"department": "学生工作部", "url": None, "note": None},
    }
    payload.update(overrides)
    return payload


def write_flow(tmp_path: Path, payload: dict, name: str = "flow.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def test_load_valid_flow_with_citations(tmp_path: Path) -> None:
    path = write_flow(tmp_path, make_flow())
    flow = load_flow(path, valid_citations={"doc@v1#第一条"})
    assert flow.flow_id == "demo-flow"
    assert flow.steps[0].deadline_note is None


def test_missing_citation_rejected(tmp_path: Path) -> None:
    payload = make_flow()
    payload["steps"][0]["citations"] = []
    path = write_flow(tmp_path, payload)
    with pytest.raises(FlowLoadError, match="缺少 citations"):
        load_flow(path)


def test_citation_exempt_cannot_carry_material(tmp_path: Path) -> None:
    payload = make_flow()
    payload["steps"][0]["citation_exempt"] = True
    payload["steps"][0]["citations"] = []
    path = write_flow(tmp_path, payload)
    with pytest.raises(FlowLoadError, match="citation_exempt"):
        load_flow(path)


def test_navigation_only_rejects_facts(tmp_path: Path) -> None:
    payload = make_flow()
    payload["navigation_only"] = True
    path = write_flow(tmp_path, payload)
    with pytest.raises(FlowLoadError, match="navigation_only"):
        load_flow(path)


def test_cycle_and_missing_dependency(tmp_path: Path) -> None:
    payload = make_flow()
    payload["steps"][0]["depends_on"] = ["apply"]
    with pytest.raises(FlowLoadError, match="依赖自身"):
        load_flow(write_flow(tmp_path, payload, "self.yaml"))

    payload = make_flow()
    payload["steps"][0]["depends_on"] = ["missing"]
    with pytest.raises(FlowLoadError, match="依赖不存在"):
        load_flow(write_flow(tmp_path, payload, "missing.yaml"))

    payload = make_flow()
    payload["steps"].append(
        {
            "id": "second",
            "title": "第二步",
            "description": "x",
            "depends_on": ["apply"],
            "deadline_note": "随时",
            "responsible_department": "学生工作部",
            "citations": ["doc@v1#第一条"],
        }
    )
    payload["steps"][0]["depends_on"] = ["second"]
    with pytest.raises(FlowLoadError, match="存在环"):
        load_flow(write_flow(tmp_path, payload, "cycle.yaml"))


def test_invalid_predicate_and_unknown_slot(tmp_path: Path) -> None:
    payload = make_flow()
    payload["steps"][0]["when"] = "slots.missing == 1"
    with pytest.raises(FlowLoadError, match="未定义槽位"):
        load_flow(write_flow(tmp_path, payload, "bad_slot.yaml"))

    payload = make_flow()
    payload["steps"][0]["when"] = "slots.grade =="
    with pytest.raises(FlowLoadError, match="谓词非法"):
        load_flow(write_flow(tmp_path, payload, "bad_syntax.yaml"))


def test_overlapping_versions_rejected(tmp_path: Path) -> None:
    first = make_flow()
    second = make_flow(version="v2027", effective_from="2026-12-01")
    write_flow(tmp_path, first, "a.yaml")
    write_flow(tmp_path, second, "b.yaml")
    with pytest.raises(FlowLoadError, match="版本重叠"):
        load_flows(tmp_path)


def test_calendar_reference_checked(tmp_path: Path) -> None:
    payload = make_flow()
    payload["steps"][0]["deadline"] = "calendar:apply-window"
    path = write_flow(tmp_path, payload)
    with pytest.raises(FlowLoadError, match="calendar 事件不存在"):
        load_flow(path, calendar_event_ids={"other"})
    flow = load_flow(path, calendar_event_ids={"apply-window"})
    assert flow.steps[0].deadline == "calendar:apply-window"
