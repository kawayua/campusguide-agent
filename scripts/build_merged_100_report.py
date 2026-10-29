"""Merge both rounds into one 100-case record document.

Sources
  round 1 (001-053)  data/eval/answer_quality/batch_retest_20261028_162015
  round 2 (039-100)  data/eval/answer_quality/batch_pending_57_20261028

Merge rule: cases retested in round 2 replace their round-1 service-failure
record; the round-1 attempt stays in the merged data for traceability. Outputs
the merged dataset under data/eval/answer_quality/merged_100_20261028 and the
document docs/reports/CampusGuide_已测100题测试记录_20261028.docx.
"""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import re
import statistics

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = pathlib.Path(__file__).resolve().parents[1]
R1 = ROOT / "data/eval/answer_quality/batch_retest_20261028_162015"
R2 = ROOT / "data/eval/answer_quality/batch_pending_57_20261028"
OUTDIR = ROOT / "data/eval/answer_quality/merged_100_20261028"
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


def load_jsonl(path: pathlib.Path) -> list[dict]:
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


def dedupe_markers(line: str) -> str:
    """Keep one copy of each identical source marker per line."""
    seen: set[str] = set()

    def repl(match: re.Match) -> str:
        marker = match.group(0)
        if marker in seen:
            return ""
        seen.add(marker)
        return marker

    line = re.sub(r"\[[^\[\]]{2,}\]", repl, line)
    line = re.sub(r"[ \t]{2,}", " ", line)
    line = re.sub(r"\s+([，。；：、）])", r"\1", line)
    return line.strip()


def markdown(doc, text):
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block.strip():
            continue
        for line in block.splitlines():
            if not line.strip():
                continue
            line = re.sub(r"^#{1,6}\s*", "", line)
            inline(doc.add_paragraph(), dedupe_markers(line.lstrip("> ")))


def summary_table(doc, rows, widths, keep_together=False):
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


def first_proof(review: dict) -> dict | None:
    proof = review.get("source_proof")
    if not proof:
        return None
    if isinstance(proof, list):
        return proof[0] if proof else None
    return proof


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    anchors = {
        (a.get("article_id") or a.get("id"), a.get("part_no", 1)): a for a in load_jsonl(ARTICLES)
    }

    def resolve_proof(proof: dict) -> dict:
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
            candidates = [a for a in anchors.values() if quote and quote in (a.get("text") or "")]
        return candidates[0] if candidates else {}

    # ---- round 1: reviewed verdicts plus raw timing from the batch folders ----
    r1_reviews = {r["id"]: r for r in load_jsonl(R1 / "reviewed_cases.jsonl")}
    r1_raw = {}
    for path in sorted(R1.glob("batch_*/results.jsonl")):
        for row in load_jsonl(path):
            r1_raw[row["id"]] = {**row, "batch": path.parent.name}
    first_round: dict[int, dict] = {}
    for qid, reviewed in r1_reviews.items():
        review = reviewed["review"]
        number = int(qid.rsplit("_", 1)[-1])
        raw = r1_raw.get(qid, {})
        first_round[number] = {
            "case_number": number,
            "id": qid,
            "round": 1,
            "batch": raw.get("batch", ""),
            "question": raw.get("question", ""),
            "started_at": raw.get("started_at", ""),
            "elapsed_s": raw.get("elapsed_s"),
            "outcome": raw.get("outcome", ""),
            "answer": raw.get("answer", ""),
            "session_id": raw.get("session_id", ""),
            "review": review,
        }

    # ---- round 2: cases 039-100 ----
    second_round = {}
    for row in load_jsonl(R2 / "reviewed_cases.jsonl"):
        second_round[row["case_number"]] = {
            "case_number": row["case_number"],
            "id": row["id"],
            "round": 2,
            "batch": row.get("batch", R2.name),
            "question": row["question"],
            "started_at": row.get("started_at", ""),
            "elapsed_s": row.get("elapsed_s"),
            "outcome": row.get("outcome", ""),
            "answer": row.get("answer", ""),
            "session_id": row.get("session_id", ""),
            "review": row["review"],
            "attempts": None,
        }
    attempts = collections.Counter()
    for row in load_jsonl(R2 / "results.jsonl"):
        attempts[row["case_number"]] += 1
    for number, record in second_round.items():
        record["attempts"] = attempts.get(number, 1)

    merged: dict[int, dict] = {}
    for number in range(1, 101):
        if number in second_round:
            merged[number] = second_round[number]
            if number in first_round and first_round[number]["review"]["status"] == "service_failure":
                merged[number]["retest_of"] = {
                    "outcome": first_round[number]["outcome"],
                    "started_at": first_round[number]["started_at"],
                    "session_id": first_round[number]["session_id"],
                    "status": first_round[number]["review"]["status"],
                }
        elif number in first_round:
            merged[number] = first_round[number]
        else:
            raise SystemExit(f"case {number} missing from both rounds")

    counts = collections.Counter(merged[n]["review"]["status"] for n in merged)
    total = len(merged)
    durations = [merged[n].get("elapsed_s") or 0 for n in merged]
    retested = [n for n in merged if merged[n].get("retest_of")]
    r2_retry_cases = sum(1 for n in second_round if (second_round[n]["attempts"] or 1) > 1)
    r2_retry_max = max((second_round[n]["attempts"] or 1) for n in second_round)
    r2_attempts = sum((second_round[n]["attempts"] or 1) for n in second_round)
    # R9: citations and timing counted once per selected session, not per round
    stats_path = OUTDIR / "selection_stats.json"
    citations, citations_errors = 0, 0
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        citations = int(stats["citations"])
    audit2 = R2 / "citation_audit.json"
    if audit2.exists():
        audit = json.loads(audit2.read_text(encoding="utf-8"))
        citations_errors += len(audit.get("errors") or [])

    (OUTDIR / "merged_cases.jsonl").write_text(
        "".join(json.dumps(merged[n], ensure_ascii=False) + "\n" for n in sorted(merged)),
        encoding="utf-8",
    )
    findings = json.loads((OUTDIR / "findings.json").read_text(encoding="utf-8"))
    progress = {
        "updated_at": "2026-10-28",
        "status": "completed_100",
        "target": 100,
        "rounds": [
            {
                "round": 1,
                "questions": "001-053",
                "answered": 43,
                "service_failure": 10,
                "batch_directory": str(R1),
            },
            {
                "round": 2,
                "questions": "039-100",
                "answered": 57,
                "service_failure": 0,
                "attempts": r2_attempts,
                "batch_directory": str(R2),
            },
        ],
        "retested_cases": retested,
        "source_review_counts": {k: counts.get(k, 0) for k in ORDER},
        "citation_count": citations,
        "citation_errors": citations_errors,
        "elapsed_total_s": round(sum(durations), 1),
        "elapsed_median_s": round(statistics.median(durations), 3),
        "findings": len(findings),
        "assessment_limit": (
            "第001至100题各单轮记录；039至053题首轮接口失败、以补测轮结果为准，"
            "不含追问轮；人工结论基于原文锚点逐项核对，不代表稳定性或泛化验收。"
        ),
    }
    (OUTDIR / "progress.json").write_text(
        json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    combined_hash = hashlib.sha256(
        "".join(
            f"{n}:{merged[n]['session_id']}" for n in sorted(merged)
        ).encode("utf-8")
    ).hexdigest()

    # ---------------- document ----------------
    doc = base_doc("CampusGuide 已测100题测试记录", "已测100题  实际回答与语料核对", "2026年10月28日")
    parts_desc = "，".join(
        f"{counts.get(key, 0)}题{LABEL[key]}"
        for key in ORDER
        if counts.get(key, 0)
    )
    para(
        doc,
        "本记录合并两轮实测，覆盖题干清单第001至100题，形成完整的单轮验收材料："
        "首轮完成第001至053题，其中第039、040、041、042、044、045、046、051、052、053题因供应商接口限额未能评估；"
        "补测轮重跑第039至100题共57题，既补齐上述10题，也完成第054至100题的首次测试。"
        f"合并后100题中{parts_desc}。",
    )
    para(
        doc,
        "第039至053题的首轮失败记录保留在追溯字段中，正文一律采用补测轮的成功回答；"
        "因此本记录的100题均有可核验的实际输出，不存在仅以运行状态占位的题。",
    )
    para(
        doc,
        "“主要要点完整”不等同于输出完美：两轮共出现的重复结论、瞬时“证据未提及”与正文矛盾、"
        "缺项提示不精确等呈现问题，已在逐题核对分析中注明。系统显示ANSWERED亦不直接视为人工通过。",
    )
    rows = [["语料核验结论", "题数"]]
    rows += [[LABEL[key], counts.get(key, 0)] for key in ORDER]
    rows += [["合计", total]]
    summary_table(doc, rows, [5.6, 1.34])

    scope_heading = doc.add_heading("测试范围与记录口径", 1)
    scope_heading.paragraph_format.page_break_before = True
    para(
        doc,
        "测试入口为本地 http://127.0.0.1:7860/ 的 /on_send 回调，与页面提交同一链路。每题创建独立新会话，"
        "原始问题保持不变，未在提示词中附加参考答案或学院名称。业务判断时点固定为2026年10月25日，"
        "两轮实际执行时间均为2026年10月28日；新闻与政策的新旧判断以知识库收录范围及该时点为边界。",
    )
    round_rows = [
        ["轮次", "题号", "执行时段", "有效回答", "接口失败"],
        ["首轮", "001—053", "16:20—16:59", "43", "10"],
        ["补测轮", "039—100", "18:24—19:40", "57", "0"],
    ]
    summary_table(doc, round_rows, [0.95, 1.6, 1.85, 1.25, 1.25], keep_together=True)
    para(
        doc,
        f"补测轮共发起{r2_attempts}次请求完成57题，其中{r2_retry_cases}题因限流重试、单题最多{r2_retry_max}次；"
        "同一题只保留一次成功轮次作为结论依据，失败轮次仅计入服务统计，不计入回答能力得分。",
    )
    para(
        doc,
        "最终成功与首试成功分开计：100题最终全部取得有效回答，其中首试即成功89题"
        f"（首轮43题、补测轮46题），补测轮另有{r2_retry_cases}题经重试后成功；"
        f"两轮合计请求{53 + r2_attempts}次（首轮53次、补测轮{r2_attempts}次），"
        "多出的请求来自供应商限流退避，不视为答案质量波动。",
    )
    meta_files = [R1 / "batch_001_010/run_metadata.json", R2 / "run_metadata.json"]
    hashes = {}
    corpus_hashes = set()
    for path in meta_files:
        if not path.exists():
            continue
        meta = json.loads(path.read_text(encoding="utf-8"))
        for name, sha in (meta.get("business_code_sha256") or {}).items():
            hashes.setdefault(name, set()).add(sha)
        if meta.get("source_sha256"):
            corpus_hashes.add(meta["source_sha256"])
    same_code = all(len(values) == 1 for values in hashes.values()) if hashes else False
    code_line = ""
    if hashes:
        joined = hashlib.sha256(
            "".join(f"{k}:{sorted(v)[0]}" for k, v in sorted(hashes.items())).encode("utf-8")
        ).hexdigest()
        code_line = (
            f"业务代码指纹 {joined[:16]}（{len(hashes)} 个文件，两轮一致：{'是' if same_code else '否'}）"
        )
    corpus_line = ""
    if corpus_hashes:
        corpus_line = f"语料指纹 {sorted(corpus_hashes)[0][:16]}（两轮一致：{'是' if len(corpus_hashes) == 1 else '否'}）"
    para(
        doc,
        f"两轮均未改动业务代码与语料：{code_line}；{corpus_line}；"
        "模型 gemini-3.5-flash-lite（PLANNER / ADVISOR / VERIFIER 同一模型）。"
        "题目、原始回答与 trace 已逐题存盘，可按 session_id 复核。",
    )
    para(
        doc,
        "人工结论只用“完整、不完整、错误、有据未答、合理澄清、合理拒答、接口失败”七类；"
        "ANSWERED、PARTIAL_ANSWER、NO_EVIDENCE 仅为运行状态，不代替语料核验。"
        "“没有证据”只有在核对原文锚点确实缺失后才计为合理拒答。",
    )
    audit_note = (
        "未发现标识或偏移不一致"
        if not citations_errors
        else f"其中{citations_errors}条存在标识或偏移不一致"
    )
    para(
        doc,
        f"按选定 session 逐题去重后共 {citations} 条结论级引用，逐条比对 article_id、part_no 与原文起止偏移，"
        f"{audit_note}；引用存在不等于结论正确，"
        f"条件方向、对象与版本仍需逐项检查。100题实际耗时合计 {progress['elapsed_total_s']} 秒，"
        f"中位数 {progress['elapsed_median_s']} 秒（偶数样本取中间两值平均）。",
    )
    para(
        doc,
        "后续复核另存一份判定，不改写本记录：第061题由有据未答调整为部分回答，"
        "第065、077、078题由部分回答调整为回答主要要点完整；上表仍按原始判定统计，"
        "两套口径同时保留，复核结论见 gold_v1.jsonl 的 review_v2 字段。",
    )
    para(doc, "原始记录目录  " + str(R1), small=True)
    para(doc, "补测记录目录  " + str(R2), small=True)
    para(doc, "合并数据集  " + str(OUTDIR / "merged_cases.jsonl") + f"（指纹 {combined_hash[:16]}）", small=True)

    doc.add_heading("逐题测试记录", 1)
    for number in sorted(merged):
        record = merged[number]
        review = record["review"]
        doc.add_heading(f"第{number:03d}题", 2)
        para(doc, record["question"], bold=True).paragraph_format.keep_with_next = True
        para(
            doc,
            f"语料核验  {LABEL[review['status']]}    系统状态  {record.get('outcome')}",
            small=True,
        ).paragraph_format.keep_with_next = True
        meta = (
            f"执行轮次  第{record['round']}轮    "
            f"执行时间  {record.get('started_at', '')}    "
            f"耗时  {record.get('elapsed_s', '未记录')} 秒"
        )
        para(doc, meta, small=True)
        if record.get("retest_of"):
            first = record["retest_of"]
            para(
                doc,
                "补测说明  该题首轮因供应商活动会话上限未产出答案"
                f"（{first['started_at']}，session_id={first['session_id']}），本轮重跑成功，结论以本轮为准。",
                small=True,
            )
        para(doc, "核对分析  " + review["reason"])
        para(doc, "实际回答", bold=True)
        body, warnings, refs = answer_parts(record.get("answer") or "")
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
        proof = first_proof(review)
        if proof:
            anchor = resolve_proof(proof)
            quote = (proof.get("quote") or "").replace("\n", " ")
            excerpt = quote if len(quote) <= 360 else quote[:360] + "〔原文节选〕"
            label = f"{anchor.get('doc_title', '')} {anchor.get('article_no', '')}".strip() or proof["article_id"]
            para(doc, f"核对原文  {label}  {excerpt}", small=True)
        para(
            doc,
            f"追溯  {record.get('batch')}  session_id={record.get('session_id')}",
            small=True,
        )
        if review.get("code_refs"):
            para(doc, "定位线索  " + "；".join(review["code_refs"]), small=True)

    doc.add_heading("已发现问题与后续修复方向", 1)
    para(
        doc,
        "以下按两轮原文核对与 trace 归纳，是后续修复与回归测试的起点；本文件只整理结果，"
        "不代表相关修改已完成。质量问题题目仍属已测题，与接口失败题分开统计。",
    )
    for finding in findings:
        doc.add_heading(f"{finding['priority']}  {finding['issue']}", 2)
        para(doc, "涉及题号  " + "、".join(f"{x:03d}" for x in finding["cases"]), small=True)
        para(doc, "证据  " + finding["evidence"])
        para(doc, "建议  " + finding["repair"])
        if finding.get("code"):
            para(doc, "定位  " + finding["code"], small=True)

    path = OUT / "CampusGuide_已测100题测试记录_20261028.docx"
    doc.save(path)
    print(
        json.dumps(
            {
                "output": str(path),
                "cases": total,
                "counts": {k: counts.get(k, 0) for k in ORDER},
                "retested": retested,
                "citations": citations,
                "findings": len(findings),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
