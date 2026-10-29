"""Offline report for a completed run affected by provider failures."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from audit_answer_quality import citation_errors, load

ROOT = Path(__file__).resolve().parents[1]
LABELS = {
    "provider_session_limit": "接口会话限流",
    "output_format_error": "模型输出格式错误",
    "answer_returned": "返回答案，尚未验收内容",
    "clarification": "请求澄清",
    "no_evidence": "未检索到证据",
    "other_failure": "其他失败",
}


def errors_for(record):
    errors = []
    for node in (record.get("reply") or {}).get("trace") or []:
        error = node.get("error")
        if error:
            errors.append({"stage": node.get("node"), "error": str(error)})
        semantic = node.get("semantic")
        if isinstance(semantic, dict) and semantic.get("error"):
            errors.append({"stage": node.get("node"), "error": semantic["error"]})
    return errors


def classification(record, errors):
    if any("active session limit (200)" in e["error"] for e in errors):
        return "provider_session_limit"
    if any("JSON" in e["error"] for e in errors):
        return "output_format_error"
    return {
        "ANSWERED": "answer_returned",
        "CLARIFY": "clarification",
        "NO_EVIDENCE": "no_evidence",
    }.get(record["outcome"], "other_failure")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    args = parser.parse_args()
    folder = args.input_dir.resolve()
    records = load(folder / "results.jsonl")
    questions = load(ROOT / "data/eval/corpus_random_100_questions.jsonl")
    assert len(records) == 100 and len({r["id"] for r in records}) == 100
    assert [r["id"] for r in records] == [q["id"] for q in questions]
    gold = {r["id"]: r for r in load(ROOT / "data/eval/answer_quality/gold_v1.jsonl")}
    articles = {
        (r["article_id"], r.get("part_no", 1)): r
        for r in load(ROOT / "data/processed/articles.jsonl")
    }
    annotated = []
    for r in records:
        errors = errors_for(r)
        label = classification(r, errors)
        annotated.append(
            {
                "id": r["id"],
                "session_id": r.get("session_id"),
                "technical_classification": label,
                "errors": errors,
                "citation_errors": citation_errors(r, articles),
            }
        )
    metadata = json.loads((folder / "run_metadata.json").read_text(encoding="utf-8"))
    changed = [
        name
        for name, digest in metadata["business_code_sha256"].items()
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest
    ]
    counts = Counter(a["technical_classification"] for a in annotated)
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "completed": 100,
        "unique_questions": 100,
        "started_at": records[0]["started_at"],
        "finished_at": json.loads((folder / "progress.json").read_text(encoding="utf-8"))[
            "updated_at"
        ],
        "technical_counts": dict(counts),
        "system_outcomes": dict(Counter(r["outcome"] for r in records)),
        "session_limit_events_by_stage": dict(
            Counter(
                e["stage"]
                for a in annotated
                for e in a["errors"]
                if "active session limit (200)" in e["error"]
            )
        ),
        "citation_errors": [
            {"id": a["id"], "error": e} for a in annotated for e in a["citation_errors"]
        ],
        "business_files_changed_after_start": changed,
        "corpus_unchanged": hashlib.sha256(
            (ROOT / "data/processed/articles.jsonl").read_bytes()
        ).hexdigest()
        == metadata["source_sha256"],
        "testset_unchanged": hashlib.sha256(
            (ROOT / "data/eval/corpus_random_100_questions.jsonl").read_bytes()
        ).hexdigest()
        == metadata["testset_sha256"],
        "quality_acceptance_valid": False,
        "limitation": "100题均已实际提交；93题遭接口会话限流，1题有输出格式错误。本轮不能用于认定答案质量通过率或与历史27题完整回答直接比较。ANSWERED仅为系统状态。",
    }
    (folder / "technical_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "technical_review.jsonl").write_text(
        "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in annotated), encoding="utf-8"
    )
    lines = [
        "# 原100题完整技术复测记录",
        "",
        f"实际测试：{summary['started_at']} 至 {summary['finished_at']}。业务日期固定为2026-10-25。",
        "",
        "100题已按原顺序通过本地页面相同的 /on_send 回调逐题提交，每题使用独立会话。",
        "",
        "## 结果与限制",
        "",
        summary["limitation"],
        "",
        "| 技术分类 | 题数 |",
        "|---|---:|",
    ]
    lines += [f"| {LABELS[k]} | {counts.get(k, 0)} |" for k in LABELS if counts.get(k)]
    lines += [
        "",
        "典型接口错误：HTTP 429，user active session limit (200) reached, wait for an existing session to expire。",
        "",
        "该响应表示调用链路报告活动会话限额；不是余额不足的错误。仅凭客户端响应，无法区分当前网关自身限制与上游透传限制，也无法确认供应商如何计算或回收会话。",
        "",
        "第80题另有 JSON 修复失败（Extra data）。第54题虽遭限流，仍显示了流程清单，实际输出完整保留。第9题和第43题同为NO_EVIDENCE，但前者涉及知识库缺少完整实验室总量，后者有相关导师制度语料；两题不能仅凭状态作相同的内容评分。",
        "",
        "冻结检查项仅用于对照，原文优先；例如第14题的实际审批部门以第四条的本科生院教学运行科为准。",
        "",
        f"运行开始后业务文件变更：{len(changed)}；语料未变：{summary['corpus_unchanged']}；题库未变：{summary['testset_unchanged']}；引用硬校验错误：{len(summary['citation_errors'])}。",
        "",
        "## 逐题实际结果",
        "",
        "以下统一列出第1—100题，不拼接历史轮次的最佳答案。后续诊断重试单独保留，不替换本轮原始记录。",
        "",
    ]
    cards = []
    for i, (r, a) in enumerate(zip(records, annotated, strict=True), 1):
        label = LABELS[a["technical_classification"]]
        issues = (
            "；".join(f"{e['stage']}：{e['error']}" for e in a["errors"])
            or "未记录模型调用错误；内容状态不等同于独立验收结果。"
        )
        checks = "；".join(gold[r["id"]]["required_checks"])
        lines += [
            f"### {i:03d} {r['question']}",
            "",
            f"技术分类：{label}；系统状态：{r['outcome']}；耗时：{r['elapsed_s']}秒；会话：{r.get('session_id')}。",
            "",
            "冻结检查项：" + checks,
            "",
            "**实际最终答案：**",
            "",
            r.get("answer", ""),
            "",
            "**调用诊断：** " + issues,
            "",
        ]
        citations = (r.get("reply") or {}).get("citations") or []
        for c in citations:
            lines += [
                f"- 引用：{c.get('article_id')}，part={c.get('part_no', 1)}，偏移[{c.get('quote_start')}, {c.get('quote_end')})。"
            ]
        lines += [""]
        search = html.escape(" ".join([r["question"], r.get("answer", ""), issues]), quote=True)
        cards.append(
            f'<article data-status="{a["technical_classification"]}" data-search="{search}"><h2>{i:03d} {html.escape(r["question"])}</h2>'
            f"<p>{label} · 系统状态 {r['outcome']} · {r['elapsed_s']}秒</p>"
            f"<p><b>冻结检查项：</b>{html.escape(checks)}</p>"
            f"<details open><summary>实际最终答案</summary><pre>{html.escape(r.get('answer', ''))}</pre></details>"
            f"<p><b>调用诊断：</b>{html.escape(issues)}</p><small>会话：{html.escape(str(r.get('session_id')))}</small></article>"
        )
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    webpage = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>100题完整技术复测记录</title><style>body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;max-width:1100px;margin:auto;padding:24px;background:#f4f6f8;color:#172b3a}header,article{padding:24px;margin:18px 0;background:white;border-radius:10px}h1{font-size:26px}h2{font-size:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;background:#f8fafc;padding:16px}input,select{padding:10px;font:inherit;max-width:90%;margin:5px}article[hidden]{display:none}small{color:#576777}</style><header><h1>100题完整技术复测记录</h1>"""
    webpage += '<p>2026-10-28已实际提交100/100题，93题遭接口会话限流，1题出现输出格式错误。本轮不能用于认定答案质量通过率。</p><p>第1—100题统一展示，保留真实最终答案和错误，不拼接历史最佳结果。</p><input id="q" aria-label="搜索" placeholder="搜索问题、答案或错误"><select id="s" aria-label="技术分类"><option value="">全部</option>'
    webpage += (
        "".join(f'<option value="{k}">{v}</option>' for k, v in LABELS.items())
        + '</select><p id="count"></p></header>'
        + "".join(cards)
    )
    webpage += """<script>const cards=[...document.querySelectorAll('article')],q=document.querySelector('#q'),s=document.querySelector('#s');function filter(){let n=0;for(const c of cards){c.hidden=!!((s.value&&c.dataset.status!==s.value)||!c.dataset.search.toLowerCase().includes(q.value.toLowerCase()));if(!c.hidden)n++}document.querySelector('#count').textContent=`显示 ${n} / ${cards.length} 题`}q.addEventListener('input',filter);s.addEventListener('change',filter);filter()</script></html>"""
    (folder / "report.html").write_text(webpage, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
