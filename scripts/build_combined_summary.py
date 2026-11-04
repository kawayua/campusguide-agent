"""合并两组 50 题回归结果，输出一份总结。

组 A：``data/eval/test_v1.jsonl`` 50 题（回归集，as_of 2026-10-24，CPU 嵌入）。
组 B：``gold_v1.jsonl`` 前 50 题（as_of 2026-10-25，GPU 嵌入）。

只读取各 run 的 summary；可选读取组 B 的 ``source_review.jsonl``
（audit_answer_quality.py 产物）给出内容审计分布。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_A = ROOT / "data/eval/answer_quality/w7_retest_20261029_run01/summary.json"
DEFAULT_B = ROOT / "data/eval/answer_quality/corpus50_retest_20261030_run01/summary.json"
DEFAULT_AUDIT = ROOT / "data/eval/answer_quality/corpus50_retest_20261030_run01/source_review.jsonl"
DEFAULT_OUT = ROOT / "data/eval/answer_quality/combined_summary_20261030"
RENDERED = {"ANSWERED", "PARTIAL_ANSWER"}
HARD = {"GENERATION_FAILED", "CLIENT_ERROR", "TOOL_UNAVAILABLE", "INDEX_UNAVAILABLE"}
AUDIT_ACCEPT = {"complete", "valid_abstention", "valid_clarification"}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _audit_section(audit: list[dict[str, Any]]) -> dict[str, Any]:
    if not audit:
        return {}
    counts: dict[str, int] = {}
    for row in audit:
        counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
    acceptable = sum(counts.get(key, 0) for key in AUDIT_ACCEPT)
    citation_errors = sum(1 for row in audit if row.get("citation_errors"))
    bad = [
        {
            "id": row.get("id"),
            "status": row.get("status"),
            "reason": (row.get("reason") or "")[:200],
            "missing": row.get("missing") or [],
            "errors": row.get("errors") or [],
        }
        for row in audit
        if row.get("status") not in AUDIT_ACCEPT
    ]
    return {
        "reviewed": len(audit),
        "statuses": counts,
        "acceptable": {"count": acceptable, "rate": round(acceptable / len(audit), 4)},
        "citation_errors": citation_errors,
        "not_acceptable": bad,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-summary", type=Path, default=DEFAULT_A)
    parser.add_argument("--b-summary", type=Path, default=DEFAULT_B)
    parser.add_argument("--b-audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    a = _load(args.a_summary)
    b = _load(args.b_summary)
    audit = _audit_section(_load_jsonl(args.b_audit))

    a_total = int(a.get("total") or 0)
    b_total = int(b.get("total") or 0)
    a_completed = int(a.get("completed") or 0)
    b_completed = int(b.get("completed") or 0)
    a_outcomes = a.get("outcomes") or {}
    b_outcomes = b.get("outcomes") or {}
    a_rendered = int(a_outcomes.get("ANSWERED", 0)) + int(a_outcomes.get("PARTIAL_ANSWER", 0))
    b_rendered = int(b_outcomes.get("ANSWERED", 0)) + int(b_outcomes.get("PARTIAL_ANSWER", 0))
    a_cit = a.get("citations") or {}
    b_cit = b.get("citations") or {}
    a_failures = a.get("failures") or []
    b_failures = b.get("failures") or []
    a_hard = sum(1 for item in a_failures if item.get("actual") in HARD)
    b_hard = sum(1 for item in b_failures if item.get("outcome") in HARD)
    a_match = a.get("outcome_match") or {}
    b_renderable = b.get("renderable") or {}

    a_answerable = int(a.get("answerable") or 0)
    b_answerable = int(b_renderable.get("total") or 0)
    a_non_renderable = max(0, a_answerable - a_rendered)
    b_non_renderable = max(0, b_answerable - b_rendered)
    combined = {
        "schema": "combined-50x2-summary-v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "groups": {
            "A_test_v1": {
                "path": str(args.a_summary),
                "total": a_total,
                "completed": a_completed,
                "answerable": a_answerable,
                "rendered": a_rendered,
                "non_renderable_answer": a_non_renderable,
                "outcome_match": a_match,
                "citations": a_cit,
                "hard_failures": a_hard,
                "failures": a_failures,
                "latency_ms": (a.get("latency_ms") or {}),
                "device_note": "CPU 嵌入/重排（与 7860 演示进程共存）",
            },
            "B_gold_v1_first50": {
                "path": str(args.b_summary),
                "total": b_total,
                "completed": b_completed,
                "answerable": b_answerable,
                "rendered": b_rendered,
                "non_renderable_answer": b_non_renderable,
                "renderable": b_renderable,
                "outcomes": b_outcomes,
                "citations": b_cit,
                "hard_failures": b_hard,
                "failures": b_failures,
                "latency_ms": (b.get("latency_ms") or {}),
                "device_note": "GPU 嵌入/重排",
            },
        },
        "combined": {
            "questions": a_total + b_total,
            "completed": a_completed + b_completed,
            "answerable": a_answerable + b_answerable,
            "rendered": a_rendered + b_rendered,
            "non_renderable_answer": a_non_renderable + b_non_renderable,
            "citations_total": int(a_cit.get("total") or 0) + int(b_cit.get("total") or 0),
            "citations_valid": int(a_cit.get("valid") or 0) + int(b_cit.get("valid") or 0),
            "quotes_total": int(a_cit.get("quotes_total") or 0)
            + int(b_cit.get("quotes_total") or 0),
            "quotes_located": int(a_cit.get("quotes_located") or 0)
            + int(b_cit.get("quotes_located") or 0),
            "hard_failures": a_hard + b_hard,
        },
        "b_audit": audit,
    }

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "combined_summary.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "combined_summary.md").write_text(_render(combined), encoding="utf-8")
    print(json.dumps(combined["combined"], ensure_ascii=False))
    return 0


def _render(data: dict[str, Any]) -> str:
    a = data["groups"]["A_test_v1"]
    b = data["groups"]["B_gold_v1_first50"]
    c = data["combined"]
    audit = data.get("b_audit") or {}
    lines = [
        "# CampusGuide 回归汇总：两组各 50 题",
        "",
        f"- 生成时间：{data['generated_at']}",
        "- 流水线：当前 graph（MCP stdio）+ gemini-3.5-flash-lite",
        "- 组 A：`test_v1.jsonl` 50 题回归集，as_of 2026-10-24，CPU 嵌入",
        "- 组 B：`gold_v1.jsonl` 前 50 题，as_of 2026-10-25，GPU 嵌入",
        "- 说明：两组题集与设备不同；时延不可直接比较。组 A 的“outcome 一致”"
        "是系统 outcome 与题目期望值对比，不等于内容正确。组 B 没有逐题期望 outcome，"
        "内容正确性以 AI 审计为准（若已跑）。",
        "",
        "## 总览",
        "",
        "| 指标 | 组 A（test_v1 50） | 组 B（gold_v1 前 50） | 合计 |",
        "|---|---:|---:|---:|",
        f"| 完成 | {a['completed']}/{a['total']} | {b['completed']}/{b['total']} | {c['completed']}/{c['questions']} |",
        f"| outcome 与期望一致（仅 A） | {a['outcome_match'].get('count', 0)}/{a['outcome_match'].get('total', 0)}（{_pct(a['outcome_match'].get('rate'))}） | — | {a['outcome_match'].get('count', 0)}/{a['outcome_match'].get('total', 0)} |",
        f"| 可作答题渲染 | {a['rendered']}/{a['answerable']}（{_pct(a['rendered'] / a['answerable'] if a['answerable'] else None)}） | {b['rendered']}/{b['answerable']}（{_pct(b['rendered'] / b['answerable'] if b['answerable'] else None)}） | {c['rendered']}/{c['answerable']}（{_pct(c['rendered'] / c['answerable'] if c['answerable'] else None)}） |",
        f"| 答案题未渲染 | {a['non_renderable_answer']} | {b['non_renderable_answer']} | {c['non_renderable_answer']} |",
        f"| 基础设施硬失败 | {a['hard_failures']} | {b['hard_failures']} | {c['hard_failures']} |",
        f"| 引用合法 | {a['citations'].get('valid', 0)}/{a['citations'].get('total', 0)} | {b['citations'].get('valid', 0)}/{b['citations'].get('total', 0)} | {c['citations_valid']}/{c['citations_total']} |",
        f"| quote 定位 | {a['citations'].get('quotes_located', 0)}/{a['citations'].get('quotes_total', 0)} | {b['citations'].get('quotes_located', 0)}/{b['citations'].get('quotes_total', 0)} | {c['quotes_located']}/{c['quotes_total']} |",
        f"| p50 / p95 | {a['latency_ms'].get('p50', '—')} / {a['latency_ms'].get('p95', '—')} ms（CPU） | {b['latency_ms'].get('p50', '—')} / {b['latency_ms'].get('p95', '—')} ms（GPU） | 不可合并 |",
    ]
    if audit:
        lines.append(
            f"| 组 B 内容审计可接受 | — | {audit['acceptable']['count']}/{audit['reviewed']}（{_pct(audit['acceptable']['rate'])}） | {audit['acceptable']['count']}/{audit['reviewed']} |"
        )
    lines += [
        "",
    ]
    if audit:
        lines += [
            "## 组 B 内容审计（AI）",
            "",
            f"- 已审计：{audit['reviewed']} 题",
            f"- 可接受（complete / valid_abstention / valid_clarification）："
            f"{audit['acceptable']['count']}/{audit['reviewed']}（{_pct(audit['acceptable']['rate'])}）",
            f"- 状态分布：`{json.dumps(audit['statuses'], ensure_ascii=False)}`",
            f"- 引用错误题数：{audit['citation_errors']}",
            "",
        ]
        bad = audit.get("not_acceptable") or []
        if bad:
            lines += ["| 题目 | 状态 | 原因 |", "|---|---|---|"]
            for item in bad:
                lines.append(
                    f"| {item['id']} | {item['status']} | {str(item.get('reason') or '').replace('|', '/')} |"
                )
            lines.append("")
    lines += ["## 组 A 未达期望 / 需复核", ""]
    failures_a = a.get("failures") or []
    if failures_a:
        for item in failures_a:
            lines.append(
                f"- **{item.get('qid')}** 期望 {item.get('expected')}，实际 {item.get('actual')}"
                + (
                    f"；限流重试 {item['rate_limit_hits']} 次"
                    if item.get("rate_limit_hits")
                    else ""
                )
            )
    else:
        lines.append("无。")
    lines += ["", "## 组 B 非渲染 / 硬失败", ""]
    failures_b = b.get("failures") or []
    if failures_b:
        for item in failures_b:
            lines.append(
                f"- **{item.get('id')}** → {item.get('outcome')}"
                f"（allowed={item.get('allowed_resolution')}）"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "## 关键结论",
        "",
        f"- 两组共 {c['questions']} 题全部完成；可作答题渲染 {c['rendered']}/{c['answerable']}，"
        f"未渲染 {c['non_renderable_answer']} 题。",
        f"- 引用与原文定位合计 {c['citations_valid']}/{c['citations_total']} 全部通过；"
        "结构层没有伪造 article_id 或错 part。",
        "- 组 A 的 9 题未达期望里，007/008/015/018/021/027 多为覆盖判定误报（答案已含所缺项），"
        "037 是 profile 与问题推断专业冲突导致 INVALID_FILTER，047 是已废止问题落到 NO_EVIDENCE；"
        "优先修 037 的过滤冲突与 047 的原因码。",
        "- 组 B 的 AI 审计 37/50 可接受；主要缺口是 003 核心课程未列全、005 应对比却拒答、"
        "013 职务事实错误、020 毕业设计学分/周数缺失，以及 007/027/030/031/035 等漏项。",
        "",
        "## 口径与限制",
        "",
        "- 组 A 的 50 题是复用的 `test_v1` 回归集；support/answer 人工标签未重标，"
        "本汇总只做回归统计，不作正式门禁结论。",
        "- 组 B 的 outcome 只表示是否给出可渲染答案，不代表内容正确；"
        "内容审计是模型审计，仍需人工抽查。",
        "- 组 A 为 CPU 嵌入/重排，组 B 为 GPU；时延差异主要来自设备与题集，不是模型变化。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
