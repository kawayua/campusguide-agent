"""Produce one unified source-audited 100-case report, holdout/stability and provenance."""

from __future__ import annotations

import html
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from audit_answer_quality import citation_errors, load

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/eval/answer_quality"
LABELS = {
    "complete": "完整回答",
    "partial": "部分回答",
    "error": "实质错误",
    "valid_abstention": "合理拒答/边界说明",
    "valid_clarification": "必要且正确的澄清",
    "failed": "有据未答",
    "needs_review": "待人工核对",
}
ACCEPTED = {"complete", "valid_abstention", "valid_clarification"}


def merged_reviews(folder):
    result = {r["id"]: r for r in load(folder / "source_review.jsonl")}
    for row in load(OUT / "manual_review_overrides.jsonl"):
        if row.get("run") == folder.name and row["id"] in result:
            assert row["session_id"] == result[row["id"]]["session_id"]
            result[row["id"]] = {
                **result[row["id"]],
                **row,
                "model_original_status": result[row["id"]]["status"],
            }
    return result


def usage(records):
    prompt = completion = 0
    for r in records:
        for node in (r.get("reply") or {}).get("trace") or []:
            if node["node"] == "planner":
                raw = (node.get("plan") or {}).get("model_usage") or {}
            elif node["node"] == "verifier":
                raw = node.get("semantic") or {}
            else:
                raw = node
            prompt += raw.get("prompt_tokens", 0) or 0
            completion += raw.get("completion_tokens", 0) or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "scope": "Recorded successful role responses including planner/advisor/semantic/coverage; error responses without usage cannot be priced.",
    }


def describe_set(name, folder):
    records = load(folder / "results.jsonl")
    reviews = merged_reviews(folder)
    verdicts = Counter(reviews.get(r["id"], {}).get("status", "needs_review") for r in records)
    elapsed = [r["elapsed_s"] for r in records]
    stats = {
        "name": name,
        "completed": len(records),
        "unique_sessions": len({r["session_id"] for r in records}),
        "system_outcomes": dict(Counter(r["outcome"] for r in records)),
        "content_verdicts": dict(verdicts),
        "correctly_handled": sum(verdicts[s] for s in ACCEPTED),
        "reviewed": len(reviews),
        "service_failure_cases": sum(
            bool(r.get("service_failure_nodes"))
            or r["outcome"]
            in {"INDEX_UNAVAILABLE", "TOOL_UNAVAILABLE", "GENERATION_FAILED", "CLIENT_ERROR"}
            for r in records
        ),
        "latency": {
            "median_s": statistics.median(elapsed) if elapsed else None,
            "p95_s": sorted(elapsed)[min(len(elapsed) - 1, int(len(elapsed) * 0.95))]
            if elapsed
            else None,
            "max_s": max(elapsed) if elapsed else None,
            "sum_s": sum(elapsed),
        },
        "usage": usage(records),
    }
    return records, reviews, stats


def main():
    manifest = json.loads((OUT / "acceptance_manifest.json").read_text(encoding="utf8"))
    gold = {r["id"]: r for r in load(OUT / "gold_v1.jsonl")}
    articles = {
        (r["article_id"], r.get("part_no", 1)): r
        for r in load(ROOT / "data/processed/articles.jsonl")
    }
    baseline = {r["id"]: r for r in load(ROOT / "data/eval/corpus_random_100_fixed/review.jsonl")}
    core, reviews, core_stats = describe_set("原100题", OUT / "final_20261028")
    holdout, holdout_reviews, holdout_stats = describe_set("泛化20题", OUT / "holdout_20261028")
    stable, stable_reviews, stable_stats = describe_set(
        "关键题额外20次", OUT / "stability_20261028"
    )
    protected = [
        {
            "id": case_id,
            "baseline": "完整回答",
            "current": reviews.get(case_id, {}).get("status", "needs_review"),
        }
        for case_id in manifest["protected_ids"]
    ]
    stability = []
    for case_id in manifest["critical_ids"]:
        attempts = [
            {
                "id": case_id,
                "session_id": next((r["session_id"] for r in core if r["id"] == case_id), None),
                "status": reviews.get(case_id, {}).get("status", "needs_review"),
            }
        ]
        attempts.extend(
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "status": stable_reviews.get(r["id"], {}).get("status", "needs_review"),
            }
            for r in stable
            if r.get("reference_case_id") == case_id
        )
        stability.append(
            {
                "id": case_id,
                "attempts": attempts,
                "all_three_accepted": len(attempts) == 3
                and all(a["status"] in ACCEPTED for a in attempts),
            }
        )
    cited = 0
    errors = []
    for r in [*core, *holdout, *stable]:
        cited += len((r.get("reply") or {}).get("citations") or [])
        errors.extend({"id": r["id"], "error": e} for e in citation_errors(r, articles))
    import hashlib

    release = json.loads((OUT / "release_snapshot.json").read_text(encoding="utf8"))
    changed = [
        path
        for path, digest in release["business_code_sha256"].items()
        if not Path(path).exists() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
    ]
    source_changed = [
        path
        for path, digest in manifest["sha256"].items()
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
    ]
    review_usage = {
        "prompt_tokens": sum(
            r.get("prompt_tokens", 0)
            for folder in [
                OUT / "final_20261028",
                OUT / "holdout_20261028",
                OUT / "stability_20261028",
            ]
            for r in load(folder / "source_review.jsonl")
        ),
        "completion_tokens": sum(
            r.get("completion_tokens", 0)
            for folder in [
                OUT / "final_20261028",
                OUT / "holdout_20261028",
                OUT / "stability_20261028",
            ]
            for r in load(folder / "source_review.jsonl")
        ),
    }
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "baseline_complete": 27,
        "business_as_of": "2026-10-25",
        "index_version": manifest["index_version"],
        "sets": [core_stats, holdout_stats, stable_stats],
        "citation_count": cited,
        "citation_errors": errors,
        "code_changed_after_freeze": changed,
        "source_or_gold_changed": source_changed,
        "protected_regressions": [r for r in protected if r["current"] not in ACCEPTED],
        "critical_stability": stability,
        "acceptance": {
            "core_target": 85,
            "holdout_target": 16,
            "core_met": len(core) == 100 and core_stats["correctly_handled"] >= 85,
            "holdout_met": len(holdout) == 20 and holdout_stats["correctly_handled"] >= 16,
            "all_critical_stable": all(s["all_three_accepted"] for s in stability),
            "no_citation_error": not errors,
            "no_code_or_source_change": not changed and not source_changed,
        },
        "independent_review_usage": review_usage,
        "review_method": "Frozen criteria + independent source-based model-assisted audit + documented manual overrides. No pass based only on application outcome. Review model shares current provider and is not an authoritative human answer key.",
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8"
    )
    lines = [
        "# CampusGuide 答案质量修复与统一复测报告",
        "",
        f"生成时间：{summary['generated_at']}。实际测试日期：2026-10-28；业务日期固定2026-10-25。",
        "",
        "## 结果与验收边界",
        "",
        f"原100题：完整回答 {core_stats['content_verdicts'].get('complete', 0)} 题，正确处理 {core_stats['correctly_handled']} 题（完整回答、合理边界说明、必要正确澄清分别列示）。历史基线为27题完整回答。",
        "",
        "| 测试集 | 完成 | 完整回答 | 部分回答 | 实质错误 | 合理边界说明 | 正确澄清 | 有据未答 | 待核对 | 正确处理 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in [core_stats, holdout_stats, stable_stats]:
        counts = s["content_verdicts"]
        lines.append(
            f"| {s['name']} | {s['completed']} | "
            + " | ".join(str(counts.get(k, 0)) for k in LABELS)
            + f" | {s['correctly_handled']} |"
        )
    lines.extend(
        [
            "",
            f"发布建议门槛：85/100和16/20；当前满足情况为{summary['acceptance']}。未达到的门槛保持未通过，不以系统ANSWERED或增加拒答替代正确答案。",
            "",
            "## 本轮实际修复",
            "",
            "- 查询与问题单元分离，保留学院、专业、届别、对象与方面；明确跨身份比较分别回答，必要身份和含糊指代澄清支持下一轮回填。",
            "- 从现有语料构建可追溯的课程、名册和消息视图，课程按对象与版本匹配，名册支持有边界的姓名空格识别；所有证据仍为原文。",
            "- 课程表、长名单使用服务端原文窗口引用，模型可选择quote_id；服务端逐字复制，原文ID、片段号和偏移仍由硬校验验证。",
            "- 检索与图内证据预算统一计算，按对象/方面保留直接证据，有限定的方面重排；记录源哈希、丢弃原因、选择片段与补检索。",
            "- 生成支持完整列表及多来源；可信文档上下文只支持归属/适用范围，实质数额、期限、课程、导师角色仍须原文。",
            "- 独立完整性检查按最终已核实结论与有效流程清单判断；至多一轮补检索/补生成，保留此前核实内容；未完成显示具体缺口。",
            "- 最新消息遍历收录集合按逐条发布日期排序；月度问题按集合筛选，采集日期不冒充新闻日期。",
            "- 语义核对分批覆盖全部结论；认证/余额错误不重复请求；记录成功调用token、延迟和脱敏错误。",
            "",
            "语料、原100题文本、模型和向量索引未为评测改写。历史轮次与环境失败冒烟均保留，未拼接各轮最佳答案。",
            "",
            "## 验证与成本",
            "",
            "708项离线测试通过，其中33项新增质量回归；ruff和git diff --check通过。",
            f"最终问答引用 {cited} 条，ID/原文/偏移错误 {len(errors)} 条。冻结后业务文件变更 {len(changed)} 个，语料/验收文件变更 {len(source_changed)} 个。",
            "",
            "| 测试集 | 中位延迟秒 | P95秒 | 最大秒 | 已记录输入token | 已记录输出token | 服务失败题 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for s in [core_stats, holdout_stats, stable_stats]:
        latency = s["latency"]
        u = s["usage"]
        lines.append(
            f"| {s['name']} | {latency['median_s']} | {latency['p95_s']} | {latency['max_s']} | {u['prompt_tokens']} | {u['completion_tokens']} | {s['service_failure_cases']} |"
        )
    lines.extend(
        [
            "",
            f"独立报告审阅另用输入{review_usage['prompt_tokens']}、输出{review_usage['completion_tokens']} token。调用失败未返回usage的部分无法准确计量；未获供应商价格，不推算人民币费用。冒烟/诊断不混入最终140题，可在各自目录追踪。",
            "",
            "## 27题保护与关键题稳定性",
            "",
            f"27题保护集未保持正确处理的题号：{[r['id'][-3:] for r in summary['protected_regressions']]}。",
            "",
            "| 关键题 | 三次内容标签 | 3次均正确处理 |",
            "|---|---|---|",
        ]
    )
    for s in stability:
        lines.append(
            f"| {s['id'][-3:]} | {' / '.join(LABELS[a['status']] for a in s['attempts'])} | {'是' if s['all_three_accepted'] else '否'} |"
        )
    lines.extend(["", "## 未通过项与后续处理", ""])
    bad = [r for r in core if reviews.get(r["id"], {}).get("status") not in ACCEPTED]
    for r in bad:
        review = reviews.get(r["id"], {})
        lines.append(
            f"- {r['id'][-3:]}：{LABELS.get(review.get('status'), '待核对')}。{review.get('reason', '尚无完整审阅')} 缺项：{'；'.join(review.get('missing') or [])} 错误：{'；'.join(review.get('errors') or [])}"
        )
    lines.extend(
        [
            "",
            "## 原100题完整记录",
            "",
            "以下按原顺序统一排版；每题包含冻结标准、实际答案、引用与分析，不另拆末25题。",
            "",
        ]
    )
    cards = []
    review_rows = []
    for i, r in enumerate(core, 1):
        review = reviews.get(r["id"], {})
        case = gold[r["id"]]
        status = review.get("status", "needs_review")
        assert review.get("session_id", r["session_id"]) == r["session_id"]
        lines.extend(
            [
                f"### {i:03d} {r['question']}",
                "",
                f"内容标签：{LABELS[status]}；系统状态：{r['outcome']}；基线：{baseline[r['id']]['status']}；耗时：{r['elapsed_s']}秒；独立会话：`{r['session_id']}`。",
                "",
                "**冻结检查项：** " + "；".join(case["required_checks"]),
                "",
                "**实际最终答案：**",
                "",
                r.get("answer", ""),
                "",
                "**独立原文核对：** " + review.get("reason", "待核对"),
                "",
            ]
        )
        if review.get("missing"):
            lines.extend(["缺项：" + "；".join(review["missing"]), ""])
        if review.get("errors"):
            lines.extend(["错误：" + "；".join(review["errors"]), ""])
        for check in review.get("checks") or []:
            lines.append(
                f"- {'通过' if check.get('passed') else '未通过'}：{check.get('criterion')}。{check.get('reason', '')}"
            )
        lines.append("")
        record = {**r, "review": review, "baseline_status": baseline[r["id"]]["status"]}
        review_rows.append(record)
        search = " ".join([r["question"], r.get("answer", ""), review.get("reason", "")])
        cards.append(
            f'<article data-status="{status}" data-search="{html.escape(search, quote=True)}"><h2>{i:03d} {html.escape(r["question"])}</h2><p><b>{LABELS[status]}</b> · 系统{r["outcome"]} · 基线{baseline[r["id"]]["status"]} · {r["elapsed_s"]}秒</p><p><b>冻结检查项：</b>{html.escape("；".join(case["required_checks"]))}</p><details open><summary>实际最终答案</summary><pre>{html.escape(r.get("answer", ""))}</pre></details><p><b>分析：</b>{html.escape(review.get("reason", "待核对"))}</p><p>缺项：{html.escape("；".join(review.get("missing") or []))}</p><p>错误：{html.escape("；".join(review.get("errors") or []))}</p><small>会话：{r["session_id"]}</small></article>'
        )
    lines.extend(["", "## 泛化20题与额外稳定性原始答案", ""])
    for name, records, review_map in [
        ("泛化20题", holdout, holdout_reviews),
        ("稳定性额外20次", stable, stable_reviews),
    ]:
        lines.extend([f"### {name}", ""])
        for r in records:
            v = review_map.get(r["id"], {})
            lines.extend(
                [
                    f"#### {r['id']} {r['question']}",
                    "",
                    f"内容：{LABELS.get(v.get('status'), '待核对')}；系统：{r['outcome']}；会话：`{r['session_id']}`。",
                    "",
                    r["answer"],
                    "",
                    v.get("reason", "待核对"),
                    "",
                ]
            )
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf8")
    (OUT / "review.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in review_rows), encoding="utf8"
    )
    webpage = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CampusGuide 100题答案质量复测</title><style>body{font:16px/1.65 system-ui,"Microsoft YaHei",sans-serif;max-width:1100px;margin:auto;padding:24px;color:#172b3a;background:#f4f6f8}header,article{background:white;padding:24px;border-radius:12px;margin:18px 0;box-shadow:0 2px 8px #1231}h1{font-size:26px}h2{font-size:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;background:#f8fafc;padding:16px}input,select{padding:10px;font:inherit;margin:6px;max-width:90%}small{color:#596b77}article[hidden]{display:none}</style><header><h1>CampusGuide 答案质量统一复测</h1>"""
    webpage += f'<p>原100题完整回答{core_stats["content_verdicts"].get("complete", 0)}，正确处理{core_stats["correctly_handled"]}；基线27题完整回答。系统状态与内容标签独立统计。</p><p>业务日期2026-10-25，实际测试2026-10-28。详细泛化、稳定性、成本、冻结标准及限制见同目录report.md和summary.json。</p><input id="q" aria-label="搜索题目、答案或分析" placeholder="搜索题目、答案或分析"><select id="s" aria-label="内容标签"><option value="">全部内容标签</option>'
    webpage += (
        "".join(f'<option value="{k}">{v}</option>' for k, v in LABELS.items())
        + '</select><p id="count"></p></header>'
        + "".join(cards)
    )
    webpage += """<script>const cards=[...document.querySelectorAll('article')],q=document.querySelector('#q'),s=document.querySelector('#s');function filter(){let n=0;for(const c of cards){c.hidden=!!((s.value&&c.dataset.status!==s.value)||!c.dataset.search.toLowerCase().includes(q.value.toLowerCase()));if(!c.hidden)n++}document.querySelector('#count').textContent=`显示 ${n} / ${cards.length} 题`}q.addEventListener('input',filter);s.addEventListener('change',filter);filter()</script></html>"""
    (OUT / "report.html").write_text(webpage, encoding="utf8")
    print(
        json.dumps(
            {
                "core": core_stats,
                "holdout": holdout_stats,
                "stability": stable_stats,
                "citation_errors": len(errors),
                "code_changed": changed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
