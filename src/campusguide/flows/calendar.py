"""校历 YAML 加载。"""

from __future__ import annotations

from pathlib import Path

import yaml

from campusguide.domain.models import CalendarEvent


class CalendarLoadError(ValueError):
    """校历配置不合法。"""


def load_calendar(path: Path) -> list[CalendarEvent]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CalendarLoadError(f"{path} 不是合法 YAML：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise CalendarLoadError(f"{path} 必须包含 events 列表")
    source_url = payload.get("source_url")
    events: list[CalendarEvent] = []
    seen: set[str] = set()
    for raw in payload["events"]:
        if not isinstance(raw, dict):
            raise CalendarLoadError(f"{path} 的 event 必须是对象")
        item = dict(raw)
        if not item.get("source_url") and source_url:
            item["source_url"] = source_url
        try:
            event = CalendarEvent.model_validate(item)
        except Exception as exc:
            raise CalendarLoadError(f"{path} 的 event 不合法：{exc}") from exc
        if event.event_id in seen:
            raise CalendarLoadError(f"重复 event_id：{event.event_id}")
        seen.add(event.event_id)
        if event.end and event.end < event.start:
            raise CalendarLoadError(f"event {event.event_id} 的 end 早于 start")
        events.append(event)
    return sorted(events, key=lambda item: (item.start, item.event_id))
