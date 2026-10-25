"""Finalize 人工入口解析。

优先级：流程 YAML handoff > handoff.yaml 的 flow/reason > 通用入口。
只使用公开部门与官网页面，不编造电话或个人联系方式。
"""

from __future__ import annotations

from dataclasses import dataclass

from campusguide.config import HandoffConfig, HandoffEntry
from campusguide.domain.enums import OutcomeCode
from campusguide.domain.models import FlowDefinition


@dataclass(frozen=True)
class Handoff:
    department: str
    url: str | None = None
    note: str | None = None
    source: str = "default"

    def as_dict(self) -> dict:
        return {
            "department": self.department,
            "url": self.url,
            "note": self.note,
            "source": self.source,
        }


def _from_entry(entry: HandoffEntry, source: str) -> Handoff:
    return Handoff(
        department=entry.department,
        url=entry.url,
        note=entry.note,
        source=source,
    )


def resolve_handoff(
    config: HandoffConfig | None,
    *,
    flow: FlowDefinition | None = None,
    reason: OutcomeCode | str | None = None,
) -> Handoff:
    if flow is not None and flow.handoff.department:
        return Handoff(
            department=flow.handoff.department,
            url=str(flow.handoff.url) if flow.handoff.url else None,
            note=flow.handoff.note,
            source="flow",
        )
    if config is not None:
        if flow is not None and flow.flow_id in config.flows:
            return _from_entry(config.flows[flow.flow_id], "config:flow")
        key = reason.value if isinstance(reason, OutcomeCode) else reason
        if key and key in config.reasons:
            return _from_entry(config.reasons[key], "config:reason")
        return _from_entry(config.defaults, "config:default")
    return Handoff(department="学校相关负责部门", source="fallback")


__all__ = ["Handoff", "resolve_handoff"]
