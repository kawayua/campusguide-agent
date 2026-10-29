"""Build compact per-case review packets from the live run records.

Each packet contains the original input, the handoff rubric (expected output,
acceptance checks, focus), the actual answer, the reply's own citations, and the
frozen source anchors with their text, so a human can verify claims line by line.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/reports/CampusGuide_待测与补测57题清单_20261028.jsonl"
RESULTS = ROOT / "data/eval/answer_quality/batch_pending_57_20261028/results.jsonl"
OUT = ROOT / ".dsh_tmp/review_packets"


def answer_parts(text: str) -> tuple[str, list[str], str]:
    body, sep, refs = text.partition("**依据：**")
    warnings = []
    if sep:
        for line in refs.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("> 尚缺") or stripped.startswith("- 尚缺"):
                warnings.append(stripped.lstrip("> ").lstrip("- ").strip())
    else:
        refs = ""
    body = body.split("\n---\n")[0].strip()
    return body, warnings, refs


def is_service_failure(record: dict) -> bool:
    return bool(record.get("service_failure_nodes")) or record.get("outcome") in {
        "GENERATION_FAILED",
        "CLIENT_ERROR",
        "INDEX_UNAVAILABLE",
        "TOOL_UNAVAILABLE",
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = {
        json.loads(line)["case_number"]: json.loads(line)
        for line in SOURCE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    attempts: dict[int, list[dict]] = {}
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            attempts.setdefault(row["case_number"], []).append(row)
    latest: dict[int, dict] = {}
    for number, rows in attempts.items():
        usable = [r for r in rows if not is_service_failure(r)]
        latest[number] = (usable or rows)[-1]

    index = []
    for number in sorted(latest):
        case, row = cases[number], latest[number]
        reply = row.get("reply") or {}
        body, warnings, _refs = answer_parts(row.get("answer") or "")
        lines = [
            f"第{number:03d}题  [{case['test_type']}]",
            f"输入: {case['input']}",
            "",
            f"系统状态: {row.get('outcome')}   耗时: {row.get('elapsed_s')}s",
            f"服务故障节点: {row.get('service_failure_nodes')}",
            "",
            "期望输出与验收要点:",
            f"  {case['expected_output']}",
        ]
        for check in case.get("acceptance_checks") or []:
            lines.append(f"  - 检查项: {check}")
        lines += [f"重点检查: {case.get('focus', '')}", "", "实际回答正文:"]
        lines.append(body or "(空)")
        if warnings:
            lines += ["", "回答自带尚缺提示:"]
            lines += [f"  {w}" for w in warnings]
        citations = reply.get("citations") or []
        if citations:
            lines += ["", f"结论级引用 {len(citations)} 条:"]
            for cite in citations:
                quote = (cite.get("quote") or "").replace("\n", " ")
                lines.append(
                    f"  [{cite.get('article_id')} p{cite.get('part_no')} "
                    f"{cite.get('quote_start')}-{cite.get('quote_end')}] {quote[:400]}"
                )
        trace = reply.get("trace") or []
        unit_lines = []
        for node in trace:
            if node.get("node") == "coverage":
                for unit in node.get("units") or []:
                    unit_lines.append(
                        f"  {unit.get('unit_id')} {unit.get('status')} {unit.get('missing_items')}"
                    )
                if node.get("missing"):
                    unit_lines.append(f"  节点缺项: {node['missing']}")
            if node.get("node") == "planner":
                for unit in (node.get("plan") or {}).get("question_units") or []:
                    unit_lines.append(
                        f"  {unit.get('unit_id')} aspects={unit.get('requested_aspects')} "
                        f"cohort={unit.get('cohort')} version={unit.get('version')}"
                    )
            if node.get("node") == "verifier":
                unit_lines.append(f"  语义核对 decision={node.get('decision')}")
        if unit_lines:
            lines += ["", "coverage/单元:", *unit_lines]
        lines += ["", "冻结原文锚点:"]
        for anchor in case.get("source_anchors") or []:
            text = (anchor.get("text") or "").replace("\n", " ")
            lines.append(
                f"  <{anchor.get('doc_title')} {anchor.get('article_no')} "
                f"{anchor.get('article_id')} p{anchor.get('part_no')}> {text[:600]}"
            )
        path = OUT / f"case_{number:03d}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        index.append((number, row.get("outcome"), len(body), path.name))

    print(f"packets: {len(index)}")
    for number, outcome, length, name in index:
        print(f"{number:03d} {outcome:<18} body={length:<5} {name}")


if __name__ == "__main__":
    main()