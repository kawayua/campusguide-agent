"""流程注册表：加载、按时间/受众选择、流程匹配。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from campusguide.domain.enums import Audience
from campusguide.domain.models import FlowDefinition
from campusguide.flows.dsl import load_flows


@dataclass
class FlowMatch:
    flow: FlowDefinition
    score: int
    matched: str


class FlowRegistry:
    def __init__(self, flows: Iterable[FlowDefinition]) -> None:
        self._flows = list(flows)

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        valid_citations: Iterable[str] | None = None,
        calendar_event_ids: Iterable[str] | None = None,
    ) -> FlowRegistry:
        return cls(
            load_flows(
                directory,
                valid_citations=valid_citations,
                calendar_event_ids=calendar_event_ids,
            )
        )

    @property
    def flows(self) -> list[FlowDefinition]:
        return list(self._flows)

    def active_flows(
        self,
        *,
        as_of: date,
        audience: Audience | None = None,
    ) -> list[FlowDefinition]:
        result = []
        for flow in self._flows:
            if flow.effective_from > as_of:
                continue
            if flow.effective_to is not None and flow.effective_to < as_of:
                continue
            if audience is not None:
                audiences = set(flow.audience)
                if Audience.ALL not in audiences and audience not in audiences:
                    continue
            result.append(flow)
        return sorted(result, key=lambda item: item.flow_id)

    def get_flow(
        self,
        flow_id: str,
        *,
        as_of: date,
        audience: Audience | None = None,
    ) -> FlowDefinition | None:
        candidates = [
            flow
            for flow in self.active_flows(as_of=as_of, audience=audience)
            if flow.flow_id == flow_id
        ]
        if not candidates:
            return None
        if len(candidates) > 1:
            raise ValueError(f"流程存在多个同时生效版本：{flow_id}")
        return candidates[0]


def match_flows(
    message: str,
    flows: Iterable[FlowDefinition],
) -> list[FlowMatch]:
    text = message.strip().lower()
    if not text:
        return []
    matches: list[FlowMatch] = []
    for flow in flows:
        best = 0
        matched = ""
        for alias in [flow.title, *flow.aliases]:
            alias_text = alias.strip().lower()
            if alias_text and alias_text in text and len(alias_text) > best:
                best = len(alias_text)
                matched = alias
        if best:
            matches.append(FlowMatch(flow=flow, score=best, matched=matched))
    matches.sort(key=lambda item: (-item.score, item.flow.flow_id))
    if len(matches) >= 2 and matches[0].score == matches[1].score:
        # 同分视为歧义；调用方应追问，不静默选第一个。
        return [item for item in matches if item.score == matches[0].score]
    return matches[:1] if matches else []


def match_is_ambiguous(matches: list[FlowMatch]) -> bool:
    return len(matches) > 1 and matches[0].score == matches[1].score
