"""W7 报告：门禁、Wilson 区间与 Markdown 渲染。"""

from __future__ import annotations

from typing import Any

from campusguide.eval.w3 import percentile
from campusguide.eval.w7 import (
    _rate,
    answer_summary,
    refusal_summary,
    retrieval_summary,
    support_summary,
)


def build_w7_report(
    run: dict[str, Any],
    *,
    support_labels: list[dict[str, Any]] | None,
    answer_labels: list[dict[str, Any]] | None,
    gates_config: dict[str, float] | None = None,
) -> dict[str, Any]:
    gates_config = gates_config or {
        "support": 0.95,
        "answer_correct": 0.85,
        "refusal_recall": 0.90,
        "false_refusal": 0.10,
        "version_sensitive": 0.90,
        "article_recall": 0.90,
        "p95_ms": 10_000,
    }
    rows = run.get("rows") or []
    retrieval = retrieval_summary(rows)
    refusal = refusal_summary(rows)
    support = support_summary(support_labels or [])
    answer = answer_summary(answer_labels or [])
    structure = (run.get("summary") or {}).get("structure") or {}

    answer_by_qid = {str(item.get("qid")): item.get("label") for item in (answer_labels or [])}
    version_rows = [row for row in rows if row.get("version_sensitive")]
    version_labeled = sum(
        1
        for row in version_rows
        if answer_by_qid.get(str(row.get("qid")))
        in {"correct", "partial", "incorrect", "unanswerable_correct"}
    )
    version_correct = sum(
        1
        for row in version_rows
        if answer_by_qid.get(str(row.get("qid"))) in {"correct", "unanswerable_correct"}
    )
    version = _rate(version_correct, version_labeled)
    version["unlabeled"] = len(version_rows) - version_labeled
    latency = [int(row.get("qa_ms", row.get("duration_ms", 0)) or 0) for row in rows]
    p50 = percentile(latency, 0.50) if latency else 0.0
    p95 = percentile(latency, 0.95) if latency else 0.0

    gates = {
        "structure": {"ok": bool(structure.get("ok"))},
        "support": {
            "ok": bool(
                support["drafts_unlabeled"] == 0
                and support["rendered_support_rate"]["rate"] is not None
                and support["rendered_support_rate"]["rate"] >= gates_config["support"]
            ),
            "value": support["rendered_support_rate"]["rate"],
            "gate": gates_config["support"],
            "labeled": support["drafts_labeled"],
            "unlabeled": support["drafts_unlabeled"],
        },
        "answer_correct": {
            "ok": bool(
                answer["unlabeled"] == 0
                and answer["correct_rate"]["rate"] is not None
                and answer["correct_rate"]["rate"] >= gates_config["answer_correct"]
            ),
            "value": answer["correct_rate"]["rate"],
            "gate": gates_config["answer_correct"],
            "labeled": answer["answerable_labeled"],
            "unlabeled": answer["answerable_unlabeled"],
        },
        "refusal_recall": {
            "ok": bool(
                refusal["refusal_recall"]["rate"] is not None
                and refusal["refusal_recall"]["rate"] >= gates_config["refusal_recall"]
            ),
            "value": refusal["refusal_recall"]["rate"],
            "gate": gates_config["refusal_recall"],
        },
        "false_refusal": {
            "ok": bool(
                refusal["false_refusal"]["rate"] is not None
                and refusal["false_refusal"]["rate"] <= gates_config["false_refusal"]
            ),
            "value": refusal["false_refusal"]["rate"],
            "gate": gates_config["false_refusal"],
        },
        "version_sensitive": {
            "ok": bool(
                version["rate"] is not None and version["rate"] >= gates_config["version_sensitive"]
            ),
            "value": version["rate"],
            "gate": gates_config["version_sensitive"],
        },
        "article_recall": {
            "ok": bool(
                retrieval["article"]["recall_at_8"] is not None
                and retrieval["article"]["recall_at_8"] >= gates_config["article_recall"]
            ),
            "value": retrieval["article"]["recall_at_8"],
            "gate": gates_config["article_recall"],
        },
        "p95": {
            "ok": p95 <= gates_config["p95_ms"],
            "value": round(p95, 1),
            "gate": gates_config["p95_ms"],
        },
    }
    overall = all(item["ok"] for item in gates.values())
    annotators = sorted(
        {
            str(item.get("annotator"))
            for item in list(support_labels or []) + list(answer_labels or [])
            if item.get("annotator")
        }
    )
    return {
        "schema_version": "w7-report-v1",
        "dataset": run.get("dataset"),
        "dataset_sha256": run.get("dataset_sha256"),
        "testset_sha256": run.get("testset_sha256"),
        "run_sha256": run.get("run_sha256"),
        "annotators": annotators,
        "metadata": run.get("metadata") or {},
        "retrieval": retrieval,
        "structure": structure,
        "support": support,
        "answer_correctness": answer,
        "refusal": refusal,
        "version_sensitive": version,
        "latency_ms": {"p50": round(p50, 1), "p95": round(p95, 1)},
        "gates": gates,
        "overall": overall,
    }


def render_w7_markdown(report: dict[str, Any]) -> str:
    metadata = report.get("metadata") or {}

    def fmt(value: Any) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    lines = [
        "# CampusGuide W7 冻结测试集报告",
        "",
        f"- 数据集：`{report.get('dataset')}`（文件 `{str(report.get('dataset_sha256'))[:12]}…`）",
        f"- 冻结测试集 hash：`{str(report.get('testset_sha256'))[:12]}…`",
        f"- run_sha256：`{report.get('run_sha256')}`",
        f"- git：`{metadata.get('git_commit', 'n/a')}`（dirty={metadata.get('git_dirty')}）",
        f"- 模型：{metadata.get('model', 'n/a')}",
        f"- prompt：{metadata.get('advisor_prompt_version', 'n/a')}",
        f"- 索引：{metadata.get('vector_manifest', {}).get('sha256', 'n/a')}",
        f"- 标注：{', '.join(report.get('annotators') or []) or '未标注'}",
        "",
        "## 门禁",
        "",
        "| 门禁 | 结果 | 数值 | 目标 |",
        "|---|---|---:|---:|",
    ]
    labels = {
        "structure": "结构门禁",
        "support": "引用支持率",
        "answer_correct": "可回答题正确率",
        "refusal_recall": "拒答召回",
        "false_refusal": "误拒率",
        "version_sensitive": "版本敏感题正确率",
        "article_recall": "article Recall@8",
        "p95": "p95 延迟",
    }
    for key, item in report["gates"].items():
        value = item.get("value")
        if key == "structure":
            value_text = "100%" if item["ok"] else "未通过"
        elif key in {"support", "answer_correct"} and item.get("unlabeled"):
            labeled = item.get("labeled", 0)
            total = labeled + item.get("unlabeled", 0)
            value_text = f"待标注（已标 {labeled}/{total}）"
        elif key == "p95":
            value_text = f"{value:.0f} ms" if value is not None else "n/a"
        else:
            value_text = fmt(value)
        target = item.get("gate")
        target_text = f"{target:.2f}" if isinstance(target, (int, float)) else "—"
        lines.append(
            f"| {labels.get(key, key)} | {'通过' if item['ok'] else '未通过'} | "
            f"{value_text} | {target_text} |"
        )
    lines.extend(
        [
            "",
            f"**W7 总门禁：{'通过' if report['overall'] else '未通过'}**",
            "",
            "## 检索与拒答",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| article Recall@8 | {fmt(report['retrieval']['article']['recall_at_8'])} |",
            f"| article MRR | {fmt(report['retrieval']['article']['mrr'])} |",
            f"| section Recall@8 | {fmt(report['retrieval']['section']['recall_at_8'])} |",
            f"| section MRR | {fmt(report['retrieval']['section']['mrr'])} |",
            f"| 拒答召回 | {fmt(report['refusal']['refusal_recall']['rate'])} |",
            f"| 误拒率 | {fmt(report['refusal']['false_refusal']['rate'])} |",
            f"| 原因码正确率 | {fmt(report['refusal']['reason_code_accuracy']['rate'])} |",
            "",
            "## 支持与正确性",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| draft support | {fmt(report['support']['draft_support_rate']['rate'])} |",
            f"| rendered support | {fmt(report['support']['rendered_support_rate']['rate'])} |",
            f"| answer correctness | {fmt(report['answer_correctness']['correct_rate']['rate'])} |",
            f"| version-sensitive | {fmt(report['version_sensitive']['rate'])} |",
            f"| p50 / p95 | {report['latency_ms']['p50']:.1f} / {report['latency_ms']['p95']:.1f} ms |",
            "",
        ]
    )
    support_pending = report["support"]["drafts_unlabeled"]
    answer_pending = report["answer_correctness"]["answerable_unlabeled"]
    if support_pending or answer_pending:
        lines.append(
            f"- 注意：仍有 {support_pending} 条 support、{answer_pending} 道可答题待重标；"
            "上表比率仅基于已标注子集，不能作为最终结果。"
        )
        lines.append("")
    lines.extend(
        [
            "## 标注样本与 Wilson 95% 区间",
            "",
            "| 指标 | 成功/样本 | 比率 | 95% CI |",
            "|---|---:|---:|---:|",
        ]
    )
    interval_rows = [
        ("rendered support", report["support"]["rendered_support_rate"]),
        ("可回答题正确", report["answer_correctness"]["correct_rate"]),
        ("版本敏感", report["version_sensitive"]),
        ("拒答召回", report["refusal"]["refusal_recall"]),
        ("误拒", report["refusal"]["false_refusal"]),
    ]
    for name, item in interval_rows:
        rate = item.get("rate")
        low = item.get("wilson_low")
        high = item.get("wilson_high")
        lines.append(
            f"| {name} | {item.get('successes')}/{item.get('total')} | {fmt(rate)} | "
            f"{fmt(low)}–{fmt(high)} |"
        )
    answer = report["answer_correctness"]
    counts = answer.get("counts") or {}
    lines.extend(
        [
            "",
            f"- support 排除 {report['support']['drafts_excluded']} 条未进入统计的 draft"
            f"（共 {report['support']['drafts_total']} 条）。",
            f"- answer 明细：correct {counts.get('correct', 0)} / partial "
            f"{counts.get('partial', 0)} / incorrect {counts.get('incorrect', 0)}；"
            f"不可答题 {answer['unanswerable_correct']}/{answer['unanswerable_total']} 判断正确。",
            "",
            "## 限制",
            "",
            "- 50 题冻结集只作方向性证据；比例指标给 Wilson 95% 区间。",
            "- A1 固定分块、A3 流程 DAG 未执行（缺少相应实现），报告标 N/A。",
            "- support 与 answer correctness 必须是人工标注；AI 预标注不计入。",
            "",
        ]
    )
    return "\n".join(lines)
