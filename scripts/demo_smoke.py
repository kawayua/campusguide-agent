"""W8 真实图 5 场景 live smoke。

只走 ``ConversationService.handle_stream``；不启动 Gradio、不读 UI 状态。
前置：Qdrant 已启动、data/ 已建索引、.env 已配置模型。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from campusguide.env import load_dotenv
from campusguide.qa.runtime import create_graph_runtime

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "data" / "eval" / "w8-live-smoke.json"
OUT_MD = ROOT / "data" / "eval" / "w8-live-smoke.md"

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "policy",
        "name": "政策问答：国奖条件",
        "turns": ["申请国家奖学金需要满足哪些条件？"],
    },
    {
        "id": "privacy",
        "name": "隐私拒答：绩点与排名",
        "turns": ["查一下我的绩点和排名"],
    },
    {
        "id": "closed",
        "name": "流程 closed：国奖怎么申请",
        "turns": ["国家奖学金怎么申请"],
    },
    {
        "id": "certificate",
        "name": "多轮澄清：在读证明",
        "turns": ["我要开在读证明", "本科生", "在校生", "升学"],
    },
    {
        "id": "mixed",
        "name": "混合问题：怎么申请 + 金额",
        "turns": ["国家奖学金怎么申请，金额是多少"],
    },
]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return float(ordered[index])


def run_scenario(service: Any, scenario: dict[str, Any], as_of: date) -> dict[str, Any]:
    session_id = f"w8-smoke-{scenario['id']}-{datetime.now().strftime('%H%M%S%f')}"
    turns: list[dict[str, Any]] = []
    for message in scenario["turns"]:
        started = time.perf_counter()
        progress: list[str] = []
        reply = None
        for event in service.handle_stream(session_id, message, as_of=as_of):
            if event.kind == "progress":
                progress.append(event.label)
            elif event.kind == "final" and event.reply is not None:
                reply = event.reply
        duration_ms = int((time.perf_counter() - started) * 1000)
        if reply is None:
            raise RuntimeError(f"{scenario['id']} 未返回 final reply")
        turns.append(
            {
                "message": message,
                "outcome": reply.outcome,
                "decision": reply.decision,
                "closed": reply.closed,
                "flow_id": reply.flow_id,
                "claims": len(reply.claims),
                "citations": len(reply.citations),
                "checklist": len(reply.checklist),
                "clarification": reply.clarification,
                "handoff": reply.handoff,
                "progress": progress,
                "duration_ms": duration_ms,
                "answer_chars": len(reply.text),
                "answer_head": reply.text[:180],
            }
        )
    return {
        "id": scenario["id"],
        "name": scenario["name"],
        "session_id": session_id,
        "turns": turns,
        "final_outcome": turns[-1]["outcome"],
        "pass": bool(turns[-1]["outcome"] != "GENERATION_FAILED"),
    }


def _expectations(result: dict[str, Any]) -> list[str]:
    scenario = result["id"]
    failures: list[str] = []
    if result["final_outcome"] == "GENERATION_FAILED":
        failures.append("final outcome GENERATION_FAILED")
    if not any(turn["progress"] for turn in result["turns"]):
        failures.append("no progress events")
    if scenario == "privacy" and result["final_outcome"] != "PRIVACY_REQUEST":
        failures.append(f"expected PRIVACY_REQUEST, got {result['final_outcome']}")
    if scenario == "closed" and not any(turn["closed"] for turn in result["turns"]):
        failures.append("expected closed flow")
    if scenario == "closed" and result["final_outcome"] not in {"ANSWERED", "PARTIAL_ANSWER"}:
        failures.append(f"unexpected final outcome {result['final_outcome']}")
    if scenario == "certificate" and not any(
        turn["outcome"] == "CLARIFY" for turn in result["turns"]
    ):
        failures.append("expected at least one CLARIFY turn")
    if scenario == "mixed":
        last = result["turns"][-1]
        if last["claims"] < 1:
            failures.append("expected claims in mixed answer")
        if last["checklist"] < 1:
            failures.append("expected checklist in mixed answer")
    if scenario == "policy" and result["final_outcome"] not in {"ANSWERED"}:
        failures.append(f"expected ANSWERED, got {result['final_outcome']}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=None, help="2026-10-25；默认 DEMO_AS_OF")
    args = parser.parse_args()
    load_dotenv()
    as_of = (
        date.fromisoformat(args.as_of)
        if args.as_of
        else date.fromisoformat(os.getenv("DEMO_AS_OF", "2026-10-25"))
    )
    runtime = create_graph_runtime(today=as_of)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    try:
        for scenario in SCENARIOS:
            print(f"[{scenario['id']}] {scenario['name']}", flush=True)
            result = run_scenario(runtime.service, scenario, as_of)
            failures = _expectations(result)
            result["failures"] = failures
            if failures:
                result["pass"] = False
            results.append(result)
            for turn in result["turns"]:
                print(
                    f"  {turn['message']} -> {turn['outcome']} "
                    f"progress={turn['progress']} {turn['duration_ms']}ms",
                    flush=True,
                )
            print(f"  pass={result['pass']} failures={failures}", flush=True)
    finally:
        runtime.close()

    durations = [turn["duration_ms"] for result in results for turn in result["turns"]]
    total_ms = int((time.perf_counter() - started) * 1000)
    passed = sum(1 for result in results if result["pass"])
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "graph_version": "w6-2026-10-25",
        "model_profile": runtime.model_profile,
        "models": runtime.models,
        "prompt_versions": runtime.prompt_versions,
        "index_version": runtime.index_version,
        "scenarios": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": [result["id"] for result in results if not result["pass"]],
            "turns": len(durations),
            "total_ms": total_ms,
            "p50_turn_ms": int(statistics.median(durations)) if durations else 0,
            "p95_turn_ms": int(_p95([float(v) for v in durations])),
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# W8 真实图 5 场景 live smoke",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- as_of：{as_of.isoformat()}",
        f"- profile：`{runtime.model_profile}`",
        f"- index：`{runtime.index_version}`",
        f"- 结果：**{passed}/{len(results)} PASS**",
        f"- 单轮 p50：{payload['summary']['p50_turn_ms']}ms；p95：{payload['summary']['p95_turn_ms']}ms",
        "",
        "| 场景 | 轮次 | 最终 outcome | progress | 耗时 | 结果 |",
        "|---|---:|---|---|---:|---|",
    ]
    for result in results:
        turns = result["turns"]
        progress = " → ".join(turns[-1]["progress"])
        lines.append(
            f"| {result['name']} | {len(turns)} | {result['final_outcome']} | "
            f"{progress or '—'} | {turns[-1]['duration_ms']}ms | "
            f"{'PASS' if result['pass'] else 'FAIL'} |"
        )
    lines.extend(["", "## 失败原因", ""])
    failed = [result for result in results if result["failures"]]
    if failed:
        for result in failed:
            lines.append(f"- `{result['id']}`：{'；'.join(result['failures'])}")
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 本 smoke 只验证 `handle_stream` 与 5 个固定场景，不替代 W6 20 题/W7 冻结集报告。",
            "- 不修改 W6 图、W7 标签/报告；耗时按单轮记录，不混入 W6 指标。",
        ]
    )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"RESULTS {passed}/{len(results)} PASS")
    print(f"JSON {OUT_JSON}")
    print(f"MD   {OUT_MD}")


if __name__ == "__main__":
    main()
