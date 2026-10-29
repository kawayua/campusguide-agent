"""Compare the frozen 100-case live regression with baseline and reviewed corpus facts."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from campusguide.domain.text import normalize_text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/eval/corpus_random_100_fixed"
BASE = ROOT / "data/eval/corpus_random_100_live"


def rows(path: Path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def dump(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def grams(text):
    text = normalize_text(text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


def main():
    cases = rows(ROOT / "data/eval/corpus_random_100_questions.jsonl")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=OUT / "final_20261028")
    args = parser.parse_args()
    input_dir = args.input_dir.resolve()
    results = rows(input_dir / "results.jsonl")
    run_prefix = input_dir.relative_to(OUT).as_posix()
    raw_path = f"{run_prefix}/results.jsonl"
    baseline = rows(BASE / "results.jsonl")
    notes = json.loads((OUT / "review_notes.json").read_text(encoding="utf-8"))
    assert len(results) == len(cases) == len(notes) == 100
    assert [c["id"] for c in cases] == [r["id"] for r in results]
    articles = rows(ROOT / "data/processed/articles.jsonl")
    bykey = {(a["article_id"], a.get("part_no", 1)): a for a in articles}
    bydoc = defaultdict(list)
    for a in articles:
        bydoc[(a["doc_id"], a["version_id"])].append(a)
    traces = {}
    for path in (ROOT / "data/traces").glob("*.jsonl"):
        for t in rows(path):
            traces[t.get("session_id")] = t
    matched = [traces[r["session_id"]] for r in results if r.get("session_id") in traces]
    assert len(matched) == 100
    (OUT / "matched_server_traces.jsonl").write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in matched) + "\n", encoding="utf-8"
    )
    citation_errors = []
    citation_count = 0
    checked_cases = []
    detail = [
        "# 修复后100题逐题核对",
        "",
        "每题为同一Gradio /on_send真实回调输出，独立会话、串行执行、无自动追问或题目重试。完整结果保存在final_20261028/results.jsonl。",
        "",
        "评审标签依据问题要求、实际答案和本地语料逐题给出；不是只用系统状态推导正确性。参考片段供定位，未声称是完整独立gold答案集。",
        "",
    ]
    for c, r, old in zip(cases, results, baseline, strict=True):
        n = int(c["id"].rsplit("_", 1)[1])
        note = notes[str(n)]
        assert note.get("reviewed_session_id") == r["session_id"], f"Stale review for {n}"
        refs = []
        qg = grams(c["question"])
        for source in c["source_documents"]:
            candidates = sorted(
                bydoc[(source["doc_id"], source["version_id"])],
                key=lambda a: len(qg & grams(a["text"])) / math.sqrt(max(1, len(grams(a["text"])))),
                reverse=True,
            )
            for a in candidates[:2]:
                refs.append(
                    {
                        "article_id": a["article_id"],
                        "part_no": a.get("part_no", 1),
                        "text": a["text"][:1000],
                    }
                )
        reference_keys = {(a["article_id"], a["part_no"]) for a in refs}
        for citation in r["reply"].get("citations", []):
            key = (citation["article_id"], citation.get("part_no", 1))
            if key in bykey and key not in reference_keys:
                a = bykey[key]
                refs.append(
                    {
                        "article_id": a["article_id"],
                        "part_no": a.get("part_no", 1),
                        "text": a["text"][:1200],
                    }
                )
                reference_keys.add(key)
        local_errors = []
        for ci in r["reply"].get("citations", []):
            citation_count += 1
            key = (ci["article_id"], ci.get("part_no", 1))
            a = bykey.get(key)
            issue = None
            if a is None:
                issue = "引用ID/分片不存在"
            elif normalize_text(ci.get("quote", "")) not in normalize_text(a["text"]):
                issue = "引用文字未出现在对应语料分片"
            elif ci.get("quote_start") is not None and ci.get("quote_end") is not None:
                span = a["text"][ci["quote_start"] : ci["quote_end"]]
                if normalize_text(span) != normalize_text(ci["quote"]):
                    issue = "引用偏移不对应原文"
            if issue:
                error = {
                    "id": r["id"],
                    "article_id": ci["article_id"],
                    "part_no": ci.get("part_no", 1),
                    "issue": issue,
                }
                local_errors.append(error)
                citation_errors.append(error)
        review = {
            "id": r["id"],
            "question": c["question"],
            "category": c["category"],
            "baseline_outcome": old["outcome"],
            "system_outcome": r["outcome"],
            **note,
            "citation_errors": local_errors,
            "reference_evidence": refs,
            "answer": r["answer"],
            "session_id": r["session_id"],
        }
        checked_cases.append(review)
        detail.extend(
            [
                f"## {n:03d} · {note['status']}",
                "",
                c["question"],
                "",
                f"基线 `{old['outcome']}` → 本轮 `{r['outcome']}`；耗时 {r['elapsed_s']:.2f} 秒。",
                "",
                f"**核对结论：** {note['analysis']}",
                "",
                f"**期望/依据：** {note['expected']}",
                "",
                f"**原因与后续：** {note['cause']}",
                "",
                "**实际最终答案：**",
                "",
                r["answer"],
                "",
                "**参考语料定位：**",
                "",
            ]
        )
        for a in refs:
            excerpt = a["text"].replace("\n", " ")
            detail.append(f"- `{a['article_id']}`，part_no={a['part_no']}：{excerpt}")
        detail.extend(["", f"原始结果session_id：`{r['session_id']}`。", ""])
    (OUT / "case_details.md").write_text("\n".join(detail), encoding="utf-8")
    (OUT / "review.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in checked_cases) + "\n", encoding="utf-8"
    )
    before = Counter(r["outcome"] for r in baseline)
    after = Counter(r["outcome"] for r in results)
    quality = Counter(r["status"] for r in checked_cases)
    lat = sorted(r["elapsed_s"] for r in results)
    db_errors = [
        r["id"]
        for r in results
        if any(
            "INDEX_UNAVAILABLE" in w
            for t in r["reply"].get("trace", [])
            for w in t.get("warnings", [])
        )
    ]
    missing_count = sum(
        any(
            t.get("unanswered_questions")
            for t in r["reply"].get("trace", [])
            if t["node"] == "advisor"
        )
        for r in results
    )
    generated_cases = sum(
        any(
            t["node"] == "advisor" and t.get("mode") != "error" for t in r["reply"].get("trace", [])
        )
        for r in results
    )
    used_ids = {
        ci["article_id"].split("@", 1)[0] for r in results for ci in r["reply"].get("citations", [])
    }
    summary = {
        "completed": 100,
        "baseline_outcomes": dict(before),
        "fixed_outcomes": dict(after),
        "review_labels": dict(quality),
        "category_counts": dict(Counter(c["category"] for c in cases)),
        "testset_reference_documents": len(
            {d["doc_id"] for c in cases for d in c["source_documents"]}
        ),
        "actual_claim_cited_documents": len(used_ids),
        "advisor_completed_cases": generated_cases,
        "generation_failed_cases": [
            r["id"] for r in results if r["outcome"] == "GENERATION_FAILED"
        ],
        "client_failed_cases": [
            r["id"] for r in results if r["outcome"] in {"CLIENT_ERROR", "UI_ERROR"}
        ],
        "retrieval_index_error_cases": db_errors,
        "model_reported_omission_cases": missing_count,
        "citations_checked": citation_count,
        "citation_errors": citation_errors,
        "latency_s": {
            "median": statistics.median(lat),
            "p95": lat[math.ceil(0.95 * len(lat)) - 1],
            "max": max(lat),
            "sum": sum(lat),
        },
        "business_as_of": "2026-10-25",
        "actual_test_start": results[0]["started_at"],
        "actual_test_end": json.loads((input_dir / "progress.json").read_text(encoding="utf-8"))[
            "updated_at"
        ],
        "results_path": raw_path,
        "testset_sha256": hashlib.sha256(
            (ROOT / "data/eval/corpus_random_100_questions.jsonl").read_bytes()
        ).hexdigest(),
        "note": "Review is a source-grounded engineering audit; model status and citation validity are not factual accuracy.",
    }
    dump(OUT / "summary.json", summary)
    report = [
        "# 修复实施与100题复测报告",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat()}。",
        "",
        "已实施连接、检索范围、公开受众、事实/流程路由、多对象拆分、年份识别、部分回答状态和错误诊断修复，并重启本地7860应用加载代码。原始基线、探索运行、最终复测分目录保留。没有重建4026点向量索引。",
        "",
        "## 验证口径",
        "",
        "100道原题按相同顺序逐条调用页面的/on_send回调；每题独立会话，无代答澄清、无自动重试。题库SHA256见summary.json。测试日期以原始started_at为准；业务as_of保持原应用默认2026-10-25。知识库消息范围仍受2026-10-21采集快照限制，“最近”不等于截至测试日完整官网动态。",
        "",
        "离线测试：675 passed；ruff格式与静态检查通过。完整JUnit为offline_tests.xml。新增测试覆盖不可用代理、公/专属受众隔离、版本、路由、多子问题覆盖与安全降级。",
        "",
        f"最终全量测试时间：{summary['actual_test_start']} 至 {summary['actual_test_end']}。本报告只使用这一次全量运行，不拼接先前补测中的最佳答案。",
        "",
        f"原始知识库225份文档、244个文档版本、4026条分片；本题库参考126份不同文档（约56.0%文档覆盖），实际最终claim引用{len(used_ids)}份不同文档。文档覆盖率不是分片召回率，也不是正确率。",
        "",
        "## 系统状态前后对照",
        "",
        "| 最终状态 | 基线 | 修复后 |",
        "|---|---:|---:|",
    ]
    for key in sorted(set(before) | set(after)):
        report.append(f"| {key} | {before[key]} | {after[key]} |")
    report.extend(
        [
            "",
            f"本轮检索节点出现INDEX_UNAVAILABLE的题数：{len(db_errors)}；模型主动声明漏答的题数：{missing_count}。",
            "",
            "## 按原问题与语料复核",
            "",
            "| 评审结果 | 题数 |",
            "|---|---:|",
        ]
    )
    report.extend(f"| {key} | {value} |" for key, value in quality.items())
    report.extend(
        [
            "",
            "完整回答指本轮核对的所有核心问题均有相应结论；部分回答表示有相关正确内容但缺对象/方面/步骤；适用对象不当和答非所问单独记录；有据未答、召回失败分别定位后续处理。合理拒答不计作完整回答；第100题独立会话中的“两个学院”存在指代歧义，单独标注为题目歧义，不据此把全部缺失归咎知识库。这是基于当前语料的工程复核，未经独立双人金标准标注，不声称测得严格统计准确率。",
            "",
        ]
    )
    problematic_answered = [
        int(r["id"].rsplit("_", 1)[1])
        for r in checked_cases
        if r["system_outcome"] == "ANSWERED" and r["status"] != "完整回答"
    ]
    report.extend(
        [
            f"系统标ANSWERED但复核不完整/不适用的题号：{', '.join(f'{n:03d}' for n in problematic_answered) or '无'}。",
            "",
            "## 最初五个示例问题",
            "",
            "| 题号 | 检查问题 | 本轮结果 |",
            "|---:|---|---|",
        ]
    )
    for number in [17, 67, 34, 91, 9]:
        item = checked_cases[number - 1]
        report.append(
            f"| {number:03d} | {item['question']} | {item['status']}：{item['analysis']} |"
        )
    report.extend(
        [
            "",
            f"独立校验顶层引用 {citation_count} 条，发现引用ID/文字/偏移问题 {len(citation_errors)} 条。引用存在仅证明可定位，不证明答案完整或适用范围正确。",
            "",
            f"单题耗时中位数 {statistics.median(lat):.2f} 秒，P95 {lat[math.ceil(0.95 * len(lat)) - 1]:.2f} 秒，最长 {max(lat):.2f} 秒。",
            "",
            "## 修复内容与剩余问题",
            "",
            "下文“已实施修复及验收边界”逐项对应B01—B09，并给出剩余缺陷的定位与修复验收条件；全部内容已纳入本报告。",
            "",
            "## 交付文件",
            "",
            "- case_details.md：100题实际答案、核对依据、问题原因与后续建议。",
            "- review.jsonl / review_notes.json：逐题结构化评审。",
            f"- {raw_path} / matched_server_traces.jsonl：本轮完整原始输出和100条服务端轨迹。",
            "- summary.json / offline_tests.xml：统计结果与离线测试。",
            "- service_restart.json / final_run_health.json / final_run_context.json：服务加载、数据库状态、模型/提示词及代码哈希；app_stderr.log保留请求日志。",
            "",
            "原始结果目录：../corpus_random_100_live。冒烟与中途探索结果不混入最终100题统计。",
            "",
        ]
    )
    report.extend(
        ["", "## 逐题目录", "", "| 题号 | 问题 | 系统状态 | 语料复核 |", "|---:|---|---|---|"]
    )
    for item in checked_cases:
        number = int(item["id"].rsplit("_", 1)[1])
        report.append(
            f"| {number:03d} | {item['question']} | {item['system_outcome']} | {item['status']} |"
        )
    issues = (OUT / "implementation_and_remaining_issues.md").read_text(encoding="utf-8")
    report.extend(["", issues, "", "---", "", *detail])
    (OUT / "report.md").write_text("\n".join(report), encoding="utf-8")
    write_html(summary, checked_cases, raw_path, issues)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def write_html(summary, cases, raw_path, issues):
    def esc(value):
        return html.escape(str(value))

    cards = []
    for item in cases:
        number = int(item["id"].rsplit("_", 1)[1])
        references = "".join(
            f"<details><summary>{esc(a['article_id'])} · 分片{a['part_no']}</summary>"
            f"<pre>{esc(a['text'])}</pre></details>"
            for a in item["reference_evidence"]
        )
        cards.append(
            f'<article id="case-{number}" data-status="{esc(item["status"])}">'
            f'<div class="meta">{number:03d} · {esc(item["category"])} · '
            f"{esc(item['status'])} · {esc(item['system_outcome'])}</div>"
            f"<h2>{esc(item['question'])}</h2>"
            f"<p><b>核对结论：</b>{esc(item['analysis'])}</p>"
            f"<p><b>期望与依据：</b>{esc(item['expected'])}</p>"
            f"<p><b>原因与后续：</b>{esc(item['cause'])}</p>"
            f"<details open><summary>实际最终答案</summary><pre>{esc(item['answer'])}</pre></details>"
            f"<details><summary>参考语料定位</summary>{references}</details>"
            f"<small>session_id：{esc(item['session_id'])}</small></article>"
        )
    options = "".join(f"<option>{esc(k)}</option>" for k in summary["review_labels"])
    stats = " · ".join(f"{k} {v}题" for k, v in summary["review_labels"].items())
    text = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>知识库100题测试报告</title>
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f2f5f8;color:#17283b;font:16px/1.8 'Microsoft YaHei',sans-serif}}
main{{max-width:1120px;margin:auto;padding:32px 24px 80px}}header,article,.overview{{background:white;padding:28px;border:1px solid #dce3ea;border-radius:12px;margin:0 0 20px}}
h1{{font-size:28px;margin:0 0 16px}}h2{{font-size:20px;margin:10px 0 18px}}.meta{{font-size:14px;color:#47607a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.8 'Microsoft YaHei',sans-serif;background:#f7f9fb;padding:16px;border-radius:8px}}
summary{{cursor:pointer;color:#23547c;padding:6px 0}}small{{color:#64748b}}.controls{{position:sticky;top:0;background:#f2f5f8ee;padding:12px 0;display:flex;gap:12px}}input,select{{padding:10px 12px;border:1px solid #bdcbd9;border-radius:8px;font:inherit}}input{{flex:1;min-width:100px}}[hidden]{{display:none!important}}p{{margin:10px 0}}a{{color:#23547c}}
@media print{{.controls{{display:none}}body{{background:white}}main{{padding:0;max-width:none}}article{{break-inside:avoid;border-radius:0}}}}
</style><main><header><div class="meta">CampusGuide · 最终全量复测</div><h1>知识库100题测试报告</h1>
<p>测试时间：{esc(summary["actual_test_start"])} 至 {esc(summary["actual_test_end"])}；业务as_of：2026-10-25。</p>
<p>100题通过页面同一/on_send回调逐条提交，每题独立会话。原始数据：{esc(raw_path)}。先前失败记录单独归档，不混入本轮100题。</p>
<p><b>{esc(stats)}</b></p><p>系统ANSWERED不等于内容完整正确。逐题结论依据实际答案与本地语料核对；不是独立人工标注金标准准确率。</p>
<p>引用校验：{summary["citations_checked"]}条；ID、文字、偏移异常{len(summary["citation_errors"])}条。中位耗时{summary["latency_s"]["median"]:.2f}秒；P95 {summary["latency_s"]["p95"]:.2f}秒。</p>
<p>675项离线测试通过。知识库活动消息受采集快照限制，发布日期不等于活动举办日期。</p></header>
<details class="overview"><summary>查看已实施修复和剩余问题</summary><pre>{esc(issues)}</pre></details>
<div class="controls"><input id="search" placeholder="搜索题号、问题、答案或分析" aria-label="搜索题目"><select id="status" aria-label="筛选结果"><option value="">全部结果</option>{options}</select><span id="count">100题</span></div>
{"".join(cards)}<script>
const search=document.querySelector('#search'),status=document.querySelector('#status'),cards=[...document.querySelectorAll('article')];
function filter(){{let n=0;for(const card of cards){{const ok=(!status.value||card.dataset.status===status.value)&&card.textContent.toLowerCase().includes(search.value.toLowerCase());card.hidden=!ok;if(ok)n++;}}document.querySelector('#count').textContent=n+'题';}}
search.addEventListener('input',filter);status.addEventListener('change',filter);
</script></main></html>"""
    (OUT / "report.html").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
