"""Cross-check the generated report against the run records and review verdicts."""

from __future__ import annotations

import json
import re
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data/eval/answer_quality/batch_pending_57_20261028"
DOC = ROOT / "docs/reports/CampusGuide_已测57题测试记录_20261028.docx"
SOURCE = ROOT / "docs/reports/CampusGuide_待测与补测57题清单_20261028.jsonl"
LABEL = {
    "complete": "回答主要要点完整",
    "partial": "回答不完整",
    "error": "存在事实或范围错误",
    "failed": "有据未答",
    "valid_clarification": "合理澄清",
    "valid_abstention": "合理拒答",
    "service_failure": "接口失败 待测",
}


def blocks(doc):
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def main() -> None:
    cases = {
        json.loads(x)["case_number"]: json.loads(x)
        for x in SOURCE.read_text(encoding="utf-8").splitlines()
        if x.strip()
    }
    attempts = {}
    for line in (BASE / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            attempts.setdefault(row["case_number"], []).append(row)

    def bad(record):
        return bool(record.get("service_failure_nodes")) or record.get("outcome") in {
            "GENERATION_FAILED",
            "CLIENT_ERROR",
            "INDEX_UNAVAILABLE",
            "TOOL_UNAVAILABLE",
        }

    selected = {n: (( [r for r in rows if not bad(r)] or rows)[-1]) for n, rows in attempts.items()}
    reviews = {
        json.loads(p.read_text(encoding="utf-8"))["case_number"]: json.loads(p.read_text(encoding="utf-8"))
        for p in (BASE / "review").glob("case_*.json")
    }

    doc = Document(DOC)
    headings = []
    table = None
    current = None
    fields = {}
    for block in blocks(doc):
        if isinstance(block, Table):
            if table is None:
                table = block
            continue
        text = block.text.strip()
        match = re.fullmatch(r"第(\d{3})题", text)
        if match and block.style.name == "Heading 2":
            current = int(match.group(1))
            headings.append(current)
            fields[current] = {}
            continue
        if current is None:
            continue
        if text.startswith("语料核验"):
            fields[current]["语料核验"] = text
        if text.startswith("审核") or text.startswith("本轮类别") or "执行时间" in text:
            fields[current]["执行时间"] = text
        if "系统状态" in text:
            fields[current]["系统状态"] = text
        for key in ("核对分析", "追溯", "定位线索"):
            if text.startswith(key):
                fields[current][key] = text
        if text == "实际回答":
            fields[current]["实际回答"] = True

    problems = []
    expected = sorted(cases)
    if headings != expected:
        problems.append(f"headings mismatch: {len(headings)} vs {len(expected)}")
    for n in expected:
        row = selected.get(n)
        if row is None:
            problems.append(f"{n}: no run record")
            continue
        got = fields.get(n, {})
        if "实际回答" not in got:
            problems.append(f"{n}: missing 实际回答 block")
        status = got.get("语料核验", "")
        want = LABEL[reviews[n]["status"]]
        if want not in status:
            problems.append(f"{n}: label {status!r} != {want!r}")
        outcome = re.sub(r"^.*?系统状态\s*", "", got.get("系统状态", "")).strip()
        if outcome != (row.get("outcome") or ""):
            problems.append(f"{n}: outcome {outcome!r} != {row.get('outcome')!r}")
        started = got.get("执行时间", "")
        if row.get("started_at") not in started:
            problems.append(f"{n}: started_at not in {started!r}")
        if row.get("session_id") not in got.get("追溯", ""):
            problems.append(f"{n}: session_id missing in 追溯")
        reason = got.get("核对分析", "")
        if reviews[n]["reason"][:18] not in reason:
            problems.append(f"{n}: 核对分析 does not start with review reason")

    counts = {}
    for n in expected:
        counts[reviews[n]["status"]] = counts.get(reviews[n]["status"], 0) + 1
    if table is not None:
        rendered = {row.cells[0].text.strip(): row.cells[1].text.strip() for row in table.rows[1:]}
        for key, label in LABEL.items():
            if rendered.get(label) != str(counts.get(key, 0)):
                problems.append(f"table {label}: {rendered.get(label)} != {counts.get(key, 0)}")
        if rendered.get("合计") != str(len(expected)):
            problems.append("table 合计 mismatch")

    print(json.dumps({"cases": len(expected), "headings": len(headings), "problems": problems}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()