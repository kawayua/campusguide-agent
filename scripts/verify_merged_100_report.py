"""Cross-check the merged 100-case document against merged_cases.jsonl."""

from __future__ import annotations

import json
import re
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

ROOT = Path(__file__).resolve().parents[1]
MERGED = ROOT / "data/eval/answer_quality/merged_100_20261028/merged_cases.jsonl"
DOC = ROOT / "docs/reports/CampusGuide_已测100题测试记录_20261028.docx"
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
    merged = {
        row["case_number"]: row
        for row in (json.loads(line) for line in MERGED.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    doc = Document(DOC)
    headings, fields, current = [], {}, None
    tables = []
    for block in blocks(doc):
        if isinstance(block, Table):
            tables.append(block)
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
        if "系统状态" in text:
            fields[current]["系统状态"] = text
        if "执行时间" in text:
            fields[current]["执行时间"] = text
        if text.startswith("补测说明"):
            fields[current]["补测说明"] = text
        for key in ("核对分析", "追溯", "定位线索", "核对原文"):
            if text.startswith(key):
                fields[current][key] = text
        if text == "实际回答":
            fields[current]["实际回答"] = True

    problems = []
    if headings != list(range(1, 101)):
        problems.append(f"headings: got {len(headings)} entries, expected 100 in order")
    for n in range(1, 101):
        row = merged.get(n)
        if row is None:
            problems.append(f"{n}: missing merged record")
            continue
        got = fields.get(n, {})
        status = row["review"]["status"]
        if LABEL[status] not in got.get("语料核验", ""):
            problems.append(f"{n}: label mismatch in {got.get('语料核验')!r}")
        outcome = re.sub(r"^.*?系统状态\s*", "", got.get("系统状态", "")).strip()
        if outcome != (row.get("outcome") or ""):
            problems.append(f"{n}: outcome {outcome!r} != {row.get('outcome')!r}")
        if (row.get("started_at") or "") not in got.get("执行时间", ""):
            problems.append(f"{n}: started_at missing")
        if (row.get("session_id") or "") not in got.get("追溯", ""):
            problems.append(f"{n}: session_id missing")
        if row["review"]["reason"][:18] not in got.get("核对分析", ""):
            problems.append(f"{n}: reason mismatch")
        if "实际回答" not in got:
            problems.append(f"{n}: missing 实际回答")
        if row.get("retest_of") and "补测说明" not in got:
            problems.append(f"{n}: retested case lacks 补测说明")
        if not row.get("retest_of") and "补测说明" in got:
            problems.append(f"{n}: unexpected 补测说明")

    counts = {}
    for n in range(1, 101):
        status = merged[n]["review"]["status"]
        counts[status] = counts.get(status, 0) + 1
    summary = {row.cells[0].text.strip(): row.cells[1].text.strip() for row in tables[0].rows[1:]}
    for key, label in LABEL.items():
        if summary.get(label) != str(counts.get(key, 0)):
            problems.append(f"summary {label}: {summary.get(label)} != {counts.get(key, 0)}")
    if summary.get("合计") != "100":
        problems.append("summary total mismatch")

    print(json.dumps({"cases": len(headings), "counts": counts, "problems": problems}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()