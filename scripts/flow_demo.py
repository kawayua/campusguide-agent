"""W5 三条脚本化流程演示（不调用 LLM）。"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from campusguide.conversation.service import ConversationService
from campusguide.flows.calendar import load_calendar
from campusguide.flows.registry import FlowRegistry

ROOT = Path(__file__).resolve().parents[1]


def _citations() -> set[str] | None:
    path = ROOT / "data" / "processed" / "articles.jsonl"
    if not path.exists():
        return None
    citations: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                citations.add(json.loads(line)["article_id"])
    return citations


def service(as_of: date) -> ConversationService:
    registry = FlowRegistry.from_directory(ROOT / "configs" / "flows", valid_citations=_citations())
    calendar = {
        event.event_id: event for event in load_calendar(ROOT / "configs" / "calendar.yaml")
    }
    return ConversationService(registry, calendar=calendar, today_provider=lambda: as_of)


SCENARIOS = {
    "scholarship": (
        date(2026, 10, 25),
        ["我想申请国家奖学金"],
    ),
    "scholarship-open": (
        date(2026, 9, 30),
        ["我想申请国家奖学金", "数信，大二，没受过处分"],
    ),
    "certificate": (
        date(2026, 10, 25),
        ["我要开在读证明", "本科生", "在校生", "出国留学", "自助打印"],
    ),
    "recommendation": (
        date(2026, 10, 25),
        ["我大四，已经拿到推免资格，还需要统考报名吗？"],
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="scholarship")
    args = parser.parse_args()
    as_of, messages = SCENARIOS[args.scenario]
    bot = service(as_of)
    print(f"=== {args.scenario}（as_of={as_of}）===")
    for message in messages:
        reply = bot.handle("demo", message)
        print(f"你：{message}")
        print(f"助手：{reply.text}\n")


if __name__ == "__main__":
    main()
