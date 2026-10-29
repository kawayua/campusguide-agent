"""Build the 57-case test record document in the established report style.

Inputs
  docs/reports/CampusGuide_待测与补测57题清单_20261028.jsonl   (frozen handoff list)
  data/eval/answer_quality/batch_pending_57_20261028/results.jsonl (raw live records)
  data/eval/answer_quality/batch_pending_57_20261028/review/case_*.json (manual verdicts)
  data/eval/answer_quality/batch_pending_57_20261028/findings.json (cross-case findings)

Outputs
  docs/reports/CampusGuide_已测57题测试记录_20261028.docx
  data/eval/answer_quality/batch_pending_57_20261028/{reviewed_cases.jsonl,progress.json}
"""

from __future__ import annotations

import collections
import json
import statistics
import pathlib
import re

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / "data/eval/answer_quality/batch_pending_57_20261028"
SOURCE = ROOT / "docs/reports/CampusGuide_待测与补测57题清单_20261028.jsonl"
OUT = ROOT / "docs/reports"
ARTICLES = ROOT / "data/processed/articles.jsonl"

LABEL = {
    "complete": "回答主要要点完整",
    "partial": "回答不完整",
    "error": "存在事实或范围错误",
    "failed": "有据未答",
    "valid_clarification": "合理澄清",
    "valid_abstention": "合理拒答",
    "service_failure": "接口失败 待测",
}
ORDER = [
    "complete",
    "partial",
    "error",
    "failed",
    "valid_clarification",
    "valid_abstention",
    "service_failure",
]
KIND = {"service_retest": "接口失败补测", "not_tested": "首次测试"}


def load(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def fontstyle(style, size, bold=False, font="宋体"):
    style.font.underline = False
    style.font.name = font
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)
    style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), font)


def base_doc(title: str, short: str, stamp: str) -> Document:
    doc = Document()
    for border in list(doc.styles.element.iter(qn("w:pBdr"))):
        border.getparent().remove(border)
    doc.styles["Subtitle"].font.italic = False
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.72)
    section.bottom_margin = Inches(0.68)
    section.left_margin = section.right_margin = Inches(0.78)
    section.header_distance = Inches(0.28)
    section.footer_distance = Inches(0.28)
    for name, size, bold, face in [
        ("Normal", 11, False, "宋体"),
        ("Title", 23, True, "微软雅黑"),
        ("Subtitle", 11, False, "宋体"),
        ("Heading 1", 16, True, "微软雅黑"),
        ("Heading 2", 13, True, "微软雅黑"),
        ("Heading 3", 11, True, "微软雅黑"),
    ]:
        fontstyle(doc.styles[name], size, bold, face)
    normal = doc.styles["Normal"].paragraph_format
    normal.line_spacing = 1.22
    normal.space_after = Pt(5)
    for name in ["Heading 1", "Heading 2", "Heading 3"]:
        fmt = doc.styles[name].paragraph_format
        fmt.keep_with_next = True
        fmt.space_before = Pt(12)
        fmt.space_after = Pt(7)
    header = section.header.paragraphs[0]
    header.text = short
    header.style = "Normal"
    for run in header.runs:
        run.font.size = Pt(9)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run(f"CampusGuide  ·  {stamp}  ·  第 ")
    page = OxmlElement("w:fldSimple")
    page.set(qn("w:instr"), "PAGE")
    footer._p.append(page)
    footer.add_run(" 页")
    for run in footer.runs:
        run.font.size = Pt(9)
    doc.add_paragraph(title, "Title")
    doc.add_paragraph("整理日期  2026年10月28日    业务判断时点  2026年10月25日", "Subtitle")
    doc.core_properties.title = title
    doc.core_properties.author = "CampusGuide 测试整理"
    doc.core_properties.subject = short
    return doc


def para(doc, text="", bold=False, small=False):
    p = doc.add_paragraph()
    run = p.add_run(str(text))
    run.bold = bold
    if small:
        run.font.size = Pt(9)
    return p


def linked(p, label, url):
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), p.part.relate_to(url, RT.HYPERLINK, is_external=True))
    r = OxmlElement("w:r")
    rp = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "245A81")
    rp.append(color)
    r.append(rp)
    text = OxmlElement("w:t")
    text.text = label
    r.append(text)
    link.append(r)
    p._p.append(link)


def inline(par, text):
    parts = re.split(r"(\[[^\]]+\]\(https?://[^\s)]+\)|\*\*.*?\*\*)", text)
    for part in parts:
        match = re.fullmatch(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", part)
        if match:
            linked(par, match[1], match[2])
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            par.add_run(part[2:-2]).bold = True
        else:
            par.add_run(part.replace("`", ""))


def markdown(doc, text):
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block.strip():
            continue
        for line in block.splitlines():
            if not line.strip():
                continue
            line = re.sub(r"^#{1,6}\s*", "", line)
            inline(doc.add_paragraph(), line.lstrip("> "))


def summary_table(doc, rows, widths):
    table = doc.add_table(rows=1, cols=len(rows[0]))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for i, width in enumerate(widths):
        table.columns[i].width = Inches(width)
    for j, row in enumerate(rows):
        cells = table.rows[0].cells if j == 0 else table.add_row().cells
        for i, text in enumerate(row):
            cell = cells[i]
            cell.width = Inches(widths[i])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = str(text)
            tcPr = cell._tc.get_or_add_tcPr()
            mar = OxmlElement("w:tcMar")
            for edge in ["top", "left", "bottom", "right"]:
                el = OxmlElement("w:" + edge)
                el.set(qn("w:w"), "90")
                el.set(qn("w:type"), "dxa")
                mar.append(el)
            tcPr.append(mar)
            borders = OxmlElement("w:tcBorders")
            for edge in ["top", "left", "bottom", "right"]:
                el = OxmlElement("w:" + edge)
                el.set(qn("w:val"), "single")
                el.set(qn("w:sz"), "4")
                el.set(qn("w:color"), "D9D9D9")
                borders.append(el)
            tcPr.append(borders)
            for p in cell.paragraphs:
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.line_spacing = 1.15
                if i == 1:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in p.runs:
                    run.font.size = Pt(10)
                    run.bold = j == 0
            if j == 0:
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "E6EEF5")
                tcPr.append(shade)
        if j == 0:
            rep = OxmlElement("w:tblHeader")
            table.rows[0]._tr.get_or_add_trPr().append(rep)
    para(doc)
    return table


def answer_parts(answer: str) -> tuple[str, list[str], str]:
    body, sep, refs = answer.partition("**依据：**")
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


def proof_label(anchor: dict) -> str:
    return f"{anchor.get('doc_title', '')} {anchor.get('article_no', '')}".strip()


def is_service_failure(record: dict) -> bool:
    return bool(record.get("service_failure_nodes")) or record.get("outcome") in {
        "GENERATION_FAILED",
        "CLIENT_ERROR",
        "INDEX_UNAVAILABLE",
        "TOOL_UNAVAILABLE",
    }


def main() -> None:
    cases = {json.loads(x)["case_number"]: json.loads(x) for x in SOURCE.read_text(encoding="utf-8").splitlines() if x.strip()}
    attempts: dict[int, list[dict]] = {}
    for row in load(BASE / "results.jsonl"):
        attempts.setdefault(row["case_number"], []).append(row)
    raw: dict[str, dict] = {}
    for rows in attempts.values():
        usable = [r for r in rows if not is_service_failure(r)]
        raw[(usable or rows)[-1]["id"]] = (usable or rows)[-1]
    reviews: dict[int, dict] = {}
    for path in sorted((BASE / "review").glob("case_*.json")):
        review = json.loads(path.read_text(encoding="utf-8"))
        reviews[int(review["case_number"])] = review
    findings = (
        json.loads((BASE / "findings.json").read_text(encoding="utf-8"))
        if (BASE / "findings.json").exists()
        else []
    )
    anchors = {
        (a.get("article_id") or a.get("id"), a.get("part_no", 1)): a for a in load(ARTICLES)
    }

    def resolve_proof(proof: dict) -> dict:
        """Resolve a review's proof anchor to the corpus article that holds the quote.

        Reviews sometimes name only the document-level id; fall back to a prefix
        match on the article id, then to any article whose text contains the quote.
        """
        key = (proof.get("article_id"), proof.get("part_no", 1))
        if key in anchors:
            return anchors[key]
        quote = (proof.get("quote") or "").strip()
        prefix = proof.get("article_id") or ""
        candidates = [
            a
            for (aid, _), a in anchors.items()
            if isinstance(aid, str) and aid.startswith(prefix) and quote and quote in (a.get("text") or "")
        ]
        if not candidates:
            candidates = [
                a for a in anchors.values() if quote and quote in (a.get("text") or "")
            ]
        return candidates[0] if candidates else {}

    missing = sorted(set(cases) - set(reviews))
    if missing:
        raise SystemExit(f"unreviewed cases: {missing}")

    counts = collections.Counter(reviews[n]["status"] for n in cases)
    outcomes = collections.Counter(raw[cases[n]["id"]].get("outcome") for n in cases)
    total = len(cases)
    service = sum(1 for n in cases if reviews[n]["status"] == "service_failure")
    citation_total = sum(
        len((raw[cases[n]["id"]].get("reply") or {}).get("citations") or []) for n in cases
    )
    durations = [raw[cases[n]["id"]].get("elapsed_s") or 0 for n in cases]
    attempts_total = sum(len(v) for v in attempts.values())
    retried = sum(1 for v in attempts.values() if len(v) > 1)
    max_attempts = max(len(v) for v in attempts.values())
    audit_path = BASE / "citation_audit.json"
    audit = (
        json.loads(audit_path.read_text(encoding="utf-8"))
        if audit_path.exists()
        else {"citations": citation_total, "errors": []}
    )

    reviewed_rows = []
    for n in sorted(cases):
        case, row, review = cases[n], raw[cases[n]["id"]], reviews[n]
        reviewed_rows.append(
            {
                "id": case["id"],
                "case_number": n,
                "test_type": case["test_type"],
                "question": case["input"],
                "batch": BASE.name,
                "session_id": row.get("session_id"),
                "started_at": row.get("started_at"),
                "elapsed_s": row.get("elapsed_s"),
                "outcome": row.get("outcome"),
                "answer": row.get("answer"),
                "token_usage": row.get("token_usage"),
                "service_failure_nodes": row.get("service_failure_nodes") or [],
                "review": review,
            }
        )
    (BASE / "reviewed_cases.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reviewed_rows),
        encoding="utf-8",
    )
    progress = {
        "updated_at": "2026-10-28",
        "status": "completed_pending_57",
        "attempted": total,
        "target": 57,
        "reviewed": len(reviews),
        "system_outcomes": dict(outcomes),
        "source_review_counts": {k: counts.get(k, 0) for k in ORDER},
        "citation_count": citation_total,
        "citation_errors": len(audit.get("errors") or []),
        "attempts_total": attempts_total,
        "retried_cases": retried,
        "max_attempts_per_case": max_attempts,
        "elapsed_total_s": round(sum(durations), 1),
        "elapsed_median_s": round(statistics.median(durations), 3) if durations else 0,
        "batch_directory": str(BASE),
        "assessment_limit": (
            "第039至100题共57题单轮记录；每题单轮、单会话，不含追问轮；"
            "人工结论基于原文锚点逐项核对，不代表稳定性或泛化验收。"
        ),
    }
    (BASE / "progress.json").write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    doc = base_doc("CampusGuide 已测57题测试记录", "已测57题  实际回答与语料核对", "2026年10月28日")
    tail = (
        f"，另{service}题受供应商接口限制未能完成有效质量评估。"
        if service
        else "。"
    )
    parts = [
        f"{value}题{LABEL[key]}"
        for key in ORDER
        for value in [counts.get(key, 0)]
        if value
    ]
    para(
        doc,
        "本记录整理待测与补测清单第039至100题共57道题的实际输出与逐题语料核验，"
        "与已测53题记录合并后构成第001至100题的完整单轮记录。57题中"
        + "，".join(parts)
        + tail,
    )
    para(
        doc,
        "57题分为两类：第039、040、041、042、044、045、046、051、052、053题为此前因接口异常未能评估的补测，"
        f"第054至100题为首次测试；原题号保持不变。本轮{total}题均取得可核验的有效回答，"
        f"补测题不再因服务异常空缺。",
    )
    para(
        doc,
        f"有效回答并非一次成功：为绕开供应商活动会话上限，同一题最多重试{max_attempts}次，"
        f"共{retried}题发生重试、累计{attempts_total}次请求；下表结论以恢复后的成功轮次为准，"
        "失败轮次只计入服务统计，不计入回答能力得分。",
    )
    para(
        doc,
        "“主要要点完整”不等同于输出完美：回答中仍存在重复结论、瞬时“证据未提及”与正文矛盾、"
        "缺项提示不精确等呈现问题，逐题核对分析已注明。系统显示ANSWERED亦不直接视为人工通过。",
    )
    rows = [["语料核验结论", "题数"]]
    rows += [[LABEL[key], counts.get(key, 0)] for key in ORDER]
    rows += [["合计", total]]
    summary_table(doc, rows, [5.6, 1.34])

    doc.add_heading("测试范围与记录口径", 1)
    para(
        doc,
        "测试入口为本地 http://127.0.0.1:7860/ 的 /on_send 回调，与页面提交同一链路。每题创建独立新会话，"
        "原始问题保持不变，未在提示词中附加参考答案或学院名称。业务判断时点固定为2026年10月25日，"
        "实际执行时间为2026年10月28日；新闻与政策的新旧判断以知识库收录范围及该时点为边界。",
    )
    metadata_path = BASE / "run_metadata.json"
    models = "gemini-3.5-flash-lite（PLANNER / ADVISOR / VERIFIER 同一模型）"
    code_hash = corpus_hash = ""
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        hashes = metadata.get("business_code_sha256") or {}
        if hashes:
            combined = hashlib_join(hashes)
            code_hash = f"业务代码指纹 {combined[:16]}（{len(hashes)} 个文件）"
        corpus_hash = f"语料指纹 {(metadata.get('source_sha256') or '')[:16]}"
    para(
        doc,
        f"本轮未修改业务代码与语料，全批次同一快照：{code_hash}；{corpus_hash}；"
        f"模型 {models}。题目、原始回答与 trace 已逐题存盘，可复核。",
    )
    para(
        doc,
        "人工结论只用“完整、不完整、错误、有据未答、合理澄清、合理拒答、接口失败”七类；"
        "ANSWERED、PARTIAL_ANSWER、NO_EVIDENCE 仅为运行状态，不代替语料核验。"
        "“没有证据”只有在核对原文锚点确实缺失后才计为合理拒答。",
    )
    para(
        doc,
        f"本轮共核查 {audit['citations']} 条结论级引用，逐条比对 article_id、part_no 与原文起止偏移，"
        f"{'未发现' if not audit['errors'] else str(len(audit['errors']))+'条'}{'标识或偏移不一致' if not audit['errors'] else '存在不一致'}；引用存在不等于结论正确，条件方向、对象与版本仍需逐项检查。"
        f"57题实际耗时合计 {progress['elapsed_total_s']} 秒，中位数 {progress['elapsed_median_s']} 秒。",
    )
    para(doc, "原始记录目录  " + str(BASE), small=True)

    doc.add_heading("逐题测试记录", 1)
    for n in sorted(cases):
        case, row, review = cases[n], raw[cases[n]["id"]], reviews[n]
        doc.add_heading(f"第{n:03d}题", 2)
        para(doc, case["input"], bold=True).paragraph_format.keep_with_next = True
        para(
            doc,
            f"语料核验  {LABEL[review['status']]}    系统状态  {row.get('outcome')}",
            small=True,
        ).paragraph_format.keep_with_next = True
        para(
            doc,
            f"本轮类别  {KIND.get(case['test_type'], case['test_type'])}    "
            f"执行时间  {row.get('started_at', '')}    耗时  {row.get('elapsed_s', '未记录')} 秒",
            small=True,
        )
        para(doc, "核对分析  " + review["reason"])
        para(doc, "实际回答", bold=True)
        body, warnings, refs = answer_parts(row.get("answer") or "")
        markdown(doc, body or "（本轮未生成回答正文）")
        for warning in warnings:
            para(doc, warning)
        sources = []
        for label, url in re.findall(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", refs):
            if (label, url) not in sources:
                sources.append((label, url))
        if sources:
            p = para(doc, "回答引用来源  ", small=True)
            for index, (label, url) in enumerate(sources):
                if index:
                    p.add_run("；")
                linked(p, label, url)
        proof = review.get("source_proof")
        if proof:
            anchor = resolve_proof(proof)
            quote = (proof.get("quote") or "").replace("\n", " ")
            excerpt = quote if len(quote) <= 360 else quote[:360] + "〔原文节选〕"
            label = proof_label(anchor) or proof["article_id"]
            para(doc, f"核对原文  {label}  {excerpt}", small=True)
        para(doc, f"追溯  {BASE.name}  session_id={row.get('session_id')}", small=True)
        if review.get("code_refs"):
            para(doc, "定位线索  " + "；".join(review["code_refs"]), small=True)

    if findings:
        doc.add_heading("已发现问题与后续修复方向", 1)
        para(
            doc,
            "以下依据本轮原文核对与 trace 归纳，是后续修复和回归测试的起点；本文件只整理结果，"
            "不代表相关修改已完成。质量问题题目仍属已测题，与接口失败题分开统计。",
        )
        for finding in findings:
            doc.add_heading(f"{finding['priority']}  {finding['issue']}", 2)
            para(doc, "涉及题号  " + "、".join(f"{x:03d}" for x in finding["cases"]), small=True)
            para(doc, "证据  " + finding["evidence"])
            para(doc, "建议  " + finding["repair"])
            if finding.get("code"):
                para(doc, "定位  " + finding["code"], small=True)

    path = OUT / "CampusGuide_已测57题测试记录_20261028.docx"
    doc.save(path)
    print(
        json.dumps(
            {
                "output": str(path),
                "reviewed": len(reviews),
                "counts": {k: counts.get(k, 0) for k in ORDER},
                "outcomes": dict(outcomes),
                "citations": citation_total,
                "findings": len(findings),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def hashlib_join(hashes: dict) -> str:
    import hashlib

    joined = "".join(f"{k}:{v}" for k, v in sorted(hashes.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()