"""W6-B 生产 MCP 流程/混合固定场景。

dev_v0 没有办理类问题；本脚本通过生产 stdio MCP（policy-search +
policy-meta）跑 5 个固定场景，补足 flow / mixed / clarify / decline 证据。
输出独立文件，不覆盖 20 题 dev_v0 run。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from campusguide.agents.graph import GRAPH_VERSION
from campusguide.env import load_dotenv
from campusguide.eval.w3 import git_info, hash_configs, sha256_file, vector_manifest_info
from campusguide.eval.w6b import build_manifest
from campusguide.qa.runtime import create_graph_runtime

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "data" / "eval" / "w6b-mcp-scenarios.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w6b-mcp-scenarios.md"
CONFIG_FILES = [
    ROOT / "configs" / "graph.yaml",
    ROOT / "configs" / "handoff.yaml",
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "abstention.yaml",
    ROOT / "configs" / "aliases.yaml",
]

SCENARIOS = [
    {
        "case_id": "mcp-closed-flow",
        "name": "政策流程：国奖 2026 批次已截止",
        "as_of": "2026-10-25",
        "messages": ["国家奖学金怎么申请"],
        "expect": {"outcome": "ANSWERED", "flow_id": "scholarship-application", "closed": True},
    },
    {
        "case_id": "mcp-open-flow",
        "name": "政策流程：数信大二申请国奖（开放期）",
        "as_of": "2026-09-30",
        "messages": ["数信学院大二申请国奖", "没有处分"],
        "expect": {"first_outcome": "CLARIFY", "outcome": "ANSWERED", "flow_status": "open"},
    },
    {
        "case_id": "mcp-certificate-clarify",
        "name": "在读证明：本科/研究生必须先澄清",
        "as_of": "2026-10-25",
        "messages": ["我要开在读证明"],
        "expect": {"outcome": "CLARIFY", "flow_id": "enrollment-certificate", "next_slot": "level"},
    },
    {
        "case_id": "mcp-mixed",
        "name": "混合：国奖怎么申请 + 金额是多少",
        "as_of": "2026-10-25",
        "messages": ["国家奖学金怎么申请，金额是多少"],
        "expect": {"outcome": "ANSWERED", "intent": "mixed", "checklist_min": 1, "claims_min": 1},
    },
    {
        "case_id": "mcp-decline",
        "name": "拒答槽位：在读证明转通用路径 + handoff",
        "as_of": "2026-10-25",
        "messages": ["我要开在读证明", "跳过"],
        "expect": {"first_outcome": "CLARIFY", "handoff": True},
    },
]


def _summary(turn: dict[str, Any]) -> dict[str, Any]:
    plan = turn.get("plan")
    clarification = turn.get("clarification") or {}
    claims = turn.get("claims") or []
    checklist = turn.get("checklist") or []
    trace = turn.get("trace") or []
    outcome = (turn.get("outcome") or {}).get("code", "")
    return {
        "outcome": outcome,
        "decision": (
            turn.get("decision").value if getattr(turn.get("decision"), "value", None) else None
        ),
        "flow_id": turn.get("flow_id"),
        "flow_status": turn.get("flow_status"),
        "closed": bool(turn.get("closed")),
        "intent": plan.intent.value if plan else None,
        "next_slot": turn.get("pending_slot"),
        "clarification": clarification,
        "claims": len(claims),
        "checklist": len(checklist),
        "citations": len(turn.get("citations") or []),
        "handoff": turn.get("handoff") or {},
        "semantic": (turn.get("semantic") or {}).get("labels") if turn.get("semantic") else None,
        "trace": [
            {
                "node": item.get("node"),
                "duration_ms": item.get("duration_ms"),
                "decision": item.get("decision"),
            }
            for item in trace
        ],
        "duration_ms": turn.get("duration_ms", 0),
        "final_answer": turn.get("final_answer", ""),
    }


def _check(case: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
    expect = case["expect"]
    summaries = [_summary(turn) for turn in turns]
    failures: list[str] = []
    if "first_outcome" in expect and summaries[0]["outcome"] != expect["first_outcome"]:
        failures.append(f"first outcome={summaries[0]['outcome']} != {expect['first_outcome']}")
    last = summaries[-1]
    for key in ("outcome", "flow_id", "flow_status"):
        if key in expect and last[key] != expect[key]:
            failures.append(f"{key}={last[key]!r} != {expect[key]!r}")
    if expect.get("closed") is not None and last["closed"] != expect["closed"]:
        failures.append(f"closed={last['closed']} != {expect['closed']}")
    if "next_slot" in expect and last["next_slot"] != expect["next_slot"]:
        failures.append(f"next_slot={last['next_slot']!r} != {expect['next_slot']!r}")
    if "intent" in expect and last["intent"] != expect["intent"]:
        failures.append(f"intent={last['intent']!r} != {expect['intent']!r}")
    if expect.get("checklist_min") and last["checklist"] < expect["checklist_min"]:
        failures.append("checklist 为空")
    if expect.get("claims_min") and last["claims"] < expect["claims_min"]:
        failures.append("claims 为空")
    if expect.get("handoff") and not last["handoff"].get("department"):
        failures.append("handoff 缺失")
    if "first_outcome" in expect and last["outcome"] == "CLARIFY":
        failures.append("多轮后仍在 clarify")
    return {
        "case_id": case["case_id"],
        "name": case["name"],
        "as_of": case["as_of"],
        "messages": case["messages"],
        "expect": expect,
        "pass": not failures,
        "failures": failures,
        "turns": summaries,
    }


def _md(report: dict[str, Any]) -> str:
    lines = [
        "# CampusGuide W6-B 生产 MCP 固定场景",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- graph：`{report['manifest']['graph_version']}`",
        f"- commit：`{report['manifest']['git_commit']}`（dirty={report['manifest']['git_dirty']}）",
        f"- index：`{report['manifest']['index_version']}`",
        f"- 结果：**{report['summary']['passed']}/{report['summary']['total']} PASS**",
        "",
        "| case | 结果 | outcome | flow | closed/status | claims | checklist | handoff |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        last = case["turns"][-1]
        first = case["turns"][0]
        outcome = last["outcome"]
        if len(case["turns"]) > 1 and first["outcome"] == "CLARIFY":
            outcome = f"{first['outcome']}→{outcome}"
        lines.append(
            f"| {case['case_id']} | {'PASS' if case['pass'] else 'FAIL'} | {outcome} | "
            f"{last['flow_id'] or '-'} | {last['flow_status'] or '-'} | "
            f"{last['claims']} | {last['checklist']} | "
            f"{'是' if last['handoff'].get('department') else '否'} |"
        )
    lines.extend(["", "## 逐场景", ""])
    for case in report["cases"]:
        lines.append(f"### {case['case_id']}：{case['name']}")
        lines.append("")
        for index, turn in enumerate(case["turns"], start=1):
            lines.append(
                f"- 第 {index} 轮：`{turn['outcome']}`，flow=`{turn['flow_id'] or '-'}`，"
                f"status=`{turn['flow_status'] or '-'}`，claims={turn['claims']}，"
                f"checklist={turn['checklist']}，next_slot=`{turn['next_slot'] or '-'}`，"
                f"{turn['duration_ms']}ms；trace="
                + "→".join(item["node"] or "-" for item in turn["trace"])
            )
        if case["failures"]:
            lines.append(f"- FAIL：{'; '.join(case['failures'])}")
        lines.append("")
    lines.extend(
        [
            "## 边界",
            "",
            "- 本文件是 dev_v0 之外的流程/混合补充证据，不替代 20 题 dev_v0 主 run；",
            "- 生产 MCP 路径为 policy-search + policy-meta stdio；本地 flow registry 仅用于离线评测；",
            "- W6 不修改 W7 冻结集/标签/报告，W7 仍为 failed/tagged。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    runtime = create_graph_runtime(today=date(2026, 10, 25))
    cases: list[dict[str, Any]] = []
    try:
        for case in SCENARIOS:
            as_of = date.fromisoformat(case["as_of"])
            turns = []
            session = f"w6b-{case['case_id']}"
            for message in case["messages"]:
                state = runtime.runner.invoke(session, message, as_of=as_of)
                turns.append(state)
                print(
                    f"[{case['case_id']}] {message} -> {(state.get('outcome') or {}).get('code')}",
                    flush=True,
                )
            cases.append(_check(case, turns))
    finally:
        runtime.close()
    vector = vector_manifest_info(ROOT / "data" / "vector_manifest.json")
    manifest = build_manifest(
        graph_version=GRAPH_VERSION,
        git=git_info(ROOT),
        model_profile=runtime.model_profile,
        models=runtime.models,
        prompt_versions=runtime.prompt_versions,
        index_version=runtime.index_version,
        vector_manifest_sha256=vector["sha256"],
        test_set_sha256=sha256_file(ROOT / "data" / "eval" / "dev_v0.jsonl"),
        config_sha256=hash_configs(CONFIG_FILES),
        retrieval_mode="mcp-hybrid",
        flow_source="mcp:policy-meta",
        started_at=started_at,
    )
    report = {
        "schema_version": "w6b-mcp-scenarios-v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "manifest": manifest,
        "summary": {
            "total": len(cases),
            "passed": sum(1 for case in cases if case["pass"]),
            "failed": [case["case_id"] for case in cases if not case["pass"]],
        },
        "cases": cases,
    }
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_md(report), encoding="utf-8")
    print(
        f"scenarios: {report['summary']['passed']}/{report['summary']['total']} PASS; "
        f"{json_path}; {md_path}"
    )
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
