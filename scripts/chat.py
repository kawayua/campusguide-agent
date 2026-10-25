"""W5/W6 交互式 CLI。

默认是 W5 确定性流程（不调用 LLM）；``--graph`` 进入 W6 四角色图。
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from campusguide.conversation.service import ConversationService
from campusguide.flows.calendar import load_calendar
from campusguide.flows.registry import FlowRegistry
from campusguide.qa.runtime import create_graph_runtime

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


def build_service(as_of: date | None) -> ConversationService:
    citations = _citations()
    registry = FlowRegistry.from_directory(ROOT / "configs" / "flows", valid_citations=citations)
    calendar_path = ROOT / "configs" / "calendar.yaml"
    calendar = (
        {event.event_id: event for event in load_calendar(calendar_path)}
        if calendar_path.exists()
        else {}
    )
    return ConversationService(
        registry,
        calendar=calendar,
        today_provider=(lambda: as_of) if as_of else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="固定今天日期，YYYY-MM-DD（便于复现）")
    parser.add_argument(
        "--graph",
        action="store_true",
        help="进入 W6 四角色图（需 LLM/Qdrant/MCP 配置）",
    )
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    runtime = None
    if args.graph:
        runtime = create_graph_runtime(today=as_of)
        service = runtime.service
        print("CampusGuide W6 图对话（vPro）。输入 /reset 重来，/exit 退出。")
    else:
        service = build_service(as_of)
        print("CampusGuide W5 流程对话。输入 /reset 重来，/exit 退出。")
    while True:
        try:
            message = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if message in {"/exit", "exit", "quit"}:
            break
        if message == "/reset":
            service.store.reset("cli")
            print("已重置。")
            continue
        reply = service.handle("cli", message)
        print(f"助手：{reply.text}\n")


if __name__ == "__main__":
    main()
