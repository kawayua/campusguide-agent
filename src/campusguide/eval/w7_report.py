"""W7 报告：门禁、Wilson 区间与 Markdown 渲染。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from campusguide.eval.w3 import percentile
from campusguide.eval.w7 import (
    _rate,
    answer_summary,
    refusal_summary,
    retrieval_summary,
    support_summary,
)

ANSWER_CATEGORIES = ("single_article", "multi_article", "section")
ERROR_CATEGORIES = (
    "RETRIEVAL_MISS",
    "WRONG_VERSION",
    "WRONG_AUDIENCE",
    "UNSUPPORTED_CLAIM",
    "CITATION_MISMATCH",
    "INCOMPLETE_FLOW",
    "OVER_REFUSAL",
    "UNDER_REFUSAL",
    "TOOL_FAILURE",
    "LATENCY",
)


def _category_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(item.get("label") or "unlabeled") for item in items)
    labeled = sum(counts[key] for key in ("correct", "partial", "incorrect"))
    return {
        "answerable": len(items),
        "labeled": labeled,
        "correct": counts.get("correct", 0),
        "partial": counts.get("partial", 0),
        "incorrect": counts.get("incorrect", 0),
        "correct_rate": _rate(counts.get("correct", 0), labeled),
    }


def answer_by_category(
    rows: list[dict[str, Any]],
    answer_labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """按 single/multi/section 和 version-sensitive 拆分 answer correctness。"""
    labels = [item for item in answer_labels if not item.get("should_abstain")]
    result: dict[str, Any] = {"overall": _category_stats(labels)}
    for category in ANSWER_CATEGORIES:
        result[category] = _category_stats(
            [item for item in labels if item.get("category") == category]
        )
    version_qids = {str(row.get("qid")) for row in rows if row.get("version_sensitive")}
    result["version_sensitive"] = _category_stats(
        [item for item in labels if str(item.get("qid")) in version_qids]
    )
    return result


def confusion_matrix(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    rendered = {"ANSWERED", "PARTIAL_ANSWER"}
    answerable = [row for row in rows if not row.get("should_abstain")]
    unanswerable = [row for row in rows if row.get("should_abstain")]
    return {
        "answerable": {
            "rendered": sum(1 for row in answerable if row.get("outcome") in rendered),
            "abstained": sum(1 for row in answerable if row.get("outcome") not in rendered),
        },
        "unanswerable": {
            "rendered": sum(1 for row in unanswerable if row.get("outcome") in rendered),
            "abstained": sum(1 for row in unanswerable if row.get("outcome") not in rendered),
        },
    }


def _version_of(article_id: Any) -> str:
    text = str(article_id or "")
    if "@v" not in text:
        return ""
    return text.rsplit("@v", 1)[1].split("#", 1)[0]


def error_attribution(
    rows: list[dict[str, Any]],
    answer_labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """按设计 12.7 给每个失败题唯一主因；LATENCY 作为系统级限制单列。"""
    by_qid = {str(item.get("qid")): item for item in answer_labels}
    categories: dict[str, dict[str, Any]] = {
        name: {"count": 0, "qids": [], "details": []} for name in ERROR_CATEGORIES
    }
    for row in rows:
        qid = str(row.get("qid"))
        label = by_qid.get(qid) or {}
        label_value = str(label.get("label") or "unlabeled")
        if label_value in {"correct", "unanswerable_correct", "unlabeled"}:
            continue
        outcome = str(row.get("outcome") or "")
        expected = set(row.get("expected_article_ids") or [])
        evidence = {item.get("article_id") for item in row.get("evidence") or []}
        structure = row.get("structure") or {}
        if outcome == "GENERATION_FAILED":
            cause = "TOOL_FAILURE"
        elif expected and not (expected & evidence):
            cause = "RETRIEVAL_MISS"
        elif structure and not structure.get("ok", True):
            cause = "CITATION_MISMATCH"
        elif row.get("should_abstain"):
            cause = "UNDER_REFUSAL"
        elif outcome not in {"ANSWERED", "PARTIAL_ANSWER"}:
            cause = "OVER_REFUSAL"
        elif row.get("version_sensitive"):
            gold_versions = {_version_of(item) for item in expected if _version_of(item)}
            cited_versions = {
                _version_of(item.get("article_id")) for item in row.get("citations") or []
            }
            cited_versions.discard("")
            if gold_versions and cited_versions and not (gold_versions & cited_versions):
                cause = "WRONG_VERSION"
            else:
                cause = "UNSUPPORTED_CLAIM"
        else:
            cause = "UNSUPPORTED_CLAIM"
        categories[cause]["count"] += 1
        categories[cause]["qids"].append(qid)
        note = str(label.get("note") or "").strip()
        if note:
            categories[cause]["details"].append(f"{qid}: {note}")
    return {
        "categories": categories,
        "latency": {
            "note": "p50/p95 未达门禁属于系统级延迟限制，不计入单题错误主因。",
        },
    }


def build_w7_report(
    run: dict[str, Any],
    *,
    support_labels: list[dict[str, Any]] | None,
    answer_labels: list[dict[str, Any]] | None,
    gates_config: dict[str, float] | None = None,
    ablations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gates_config = gates_config or {
        "support": 0.95,
        "answer_correct": 0.85,
        "refusal_recall": 0.90,
        "false_refusal": 0.10,
        "version_sensitive": 0.90,
        "article_recall": 0.90,
        "p50_ms": 6_000,
        "p95_ms": 10_000,
    }
    rows = run.get("rows") or []
    retrieval = retrieval_summary(rows)
    refusal = refusal_summary(rows)
    support = support_summary(support_labels or [])
    answer = answer_summary(answer_labels or [])
    structure = (run.get("summary") or {}).get("structure") or {}
    category_breakdown = answer_by_category(rows, answer_labels or [])
    confusion = confusion_matrix(rows)
    errors = error_attribution(rows, answer_labels or [])

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
        "p50": {
            "ok": p50 <= gates_config["p50_ms"],
            "value": round(p50, 1),
            "gate": gates_config["p50_ms"],
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
        "answer_by_category": category_breakdown,
        "refusal": {**refusal, "confusion": confusion},
        "error_attribution": errors,
        "version_sensitive": version,
        "ablations": ablations or {},
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
        "p50": "p50 延迟",
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
        elif key in {"p50", "p95"}:
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
            "## 分类正确率",
            "",
            "| 类别 | correct/可答 | partial | incorrect | 正确率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    category_labels = {
        "overall": "Overall",
        "single_article": "single_article",
        "multi_article": "multi_article",
        "section": "section",
        "version_sensitive": "version_sensitive",
    }
    for key, label in category_labels.items():
        item = report["answer_by_category"][key]
        lines.append(
            f"| {label} | {item['correct']}/{item['answerable']} | {item['partial']} | "
            f"{item['incorrect']} | {fmt(item['correct_rate']['rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 拒答混淆矩阵",
            "",
            "| 实际 \\ 系统 | 渲染答案 | 明确拒答 |",
            "|---|---:|---:|",
            f"| 可回答（{report['refusal']['answerable']}） | "
            f"{report['refusal']['confusion']['answerable']['rendered']} | "
            f"{report['refusal']['confusion']['answerable']['abstained']} |",
            f"| 不可回答（{report['refusal']['unanswerable']}） | "
            f"{report['refusal']['confusion']['unanswerable']['rendered']} | "
            f"{report['refusal']['confusion']['unanswerable']['abstained']} |",
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
            "## 错误归因（设计 12.7）",
            "",
            "| 主因 | 数量 | 题号 |",
            "|---|---:|---|",
        ]
    )
    error_categories = report["error_attribution"]["categories"]
    for name, item in error_categories.items():
        if item["count"]:
            lines.append(f"| {name} | {item['count']} | {', '.join(item['qids'])} |")
    zero_categories = [name for name, item in error_categories.items() if not item["count"]]
    lines.append("")
    lines.append(
        f"- 其余类别为 0：{', '.join(zero_categories)}；INCOMPLETE_FLOW 为 N/A（W5 未实现）。"
    )
    for name, item in error_categories.items():
        if not item["count"]:
            continue
        lines.append("")
        lines.append(f"### {name}")
        for detail in item["details"]:
            lines.append(f"- {detail}")
    lines.extend(
        [
            "",
            f"- 延迟：{report['error_attribution']['latency']['note']}",
            "",
            "## 消融",
            "",
        ]
    )
    ablations = report.get("ablations") or {}
    a5 = ablations.get("a5") or {}
    if a5:
        lines.extend(
            [
                "### A5 检索",
                "",
                "| 模式 | overall Recall@8 | overall MRR | article MRR | section MRR | p95 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for mode, payload in (a5.get("modes") or {}).items():
            summary = payload.get("summary") or {}
            overall = summary.get("overall") or {}
            article = summary.get("article") or {}
            section = summary.get("section") or {}
            latency = summary.get("latency_ms") or {}
            lines.append(
                f"| `{mode}` | {fmt(overall.get('recall_at_8'))} | "
                f"{fmt(overall.get('mrr'))} | {fmt(article.get('mrr'))} | "
                f"{fmt(section.get('mrr'))} | {latency.get('p95', 0):.1f} ms |"
            )
    else:
        lines.append("- A5 检索消融未纳入。")
    a4 = ablations.get("a4") or {}
    if a4:
        baseline = a4.get("baseline") or {}
        no_profile = a4.get("a4") or {}
        lines.extend(
            [
                "",
                "### A4 profile 过滤",
                "",
                "| 变体 | 有 retrieved 错配的题 | retrieved 错配/总数 | 误拒率 |",
                "|---|---:|---:|---:|",
            ]
        )
        for name, payload in (
            ("baseline（有 profile）", baseline),
            ("A4（无 profile 元数据）", no_profile),
        ):
            mismatch = payload.get("mismatch") or {}
            refusal = payload.get("refusal") or {}
            false_refusal = (refusal.get("false_refusal") or {}).get("rate")
            lines.append(
                f"| {name} | {mismatch.get('questions_with_retrieved_mismatch', 0)}/"
                f"{mismatch.get('profile_questions', 0)} | "
                f"{mismatch.get('retrieved_mismatched', 0)}/"
                f"{mismatch.get('retrieved_evidence', 0)} | "
                f"{fmt(false_refusal)} |"
            )
    else:
        lines.extend(["", "- A4 profile 过滤消融未纳入。"])
    a2 = ablations.get("a2") or {}
    if a2:
        baseline = a2.get("baseline") or {}
        no_verifier = a2.get("a2") or {}
        lines.extend(
            [
                "",
                "### A2 Verifier",
                "",
                "| 变体 | supported/已标 | support rate | unsupported rate | 误拒率 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for name, payload in (
            ("baseline（有 Verifier）", baseline),
            ("A2（无 Verifier）", no_verifier),
        ):
            support = payload.get("support") or {}
            refusal = payload.get("refusal") or {}
            support_rate = (support.get("claim_support_rate") or {}).get("rate")
            unsupported_rate = (support.get("claim_unsupported_rate") or {}).get("rate")
            false_refusal = (refusal.get("false_refusal") or {}).get("rate")
            lines.append(
                f"| {name} | {support.get('supported_claims', 0)}/"
                f"{support.get('drafts_labeled', 0)}（共 {support.get('drafts_total', 0)}） | "
                f"{fmt(support_rate)} | "
                f"{fmt(unsupported_rate)} | {fmt(false_refusal)} |"
            )
        unlabeled = (no_verifier.get("support") or {}).get("drafts_unlabeled", 0)
        if unlabeled:
            lines.append("")
            lines.append(f"- A2 support 仍有 {unlabeled} 条未标注；该表仅基于已标注子集。")
    else:
        lines.extend(["", "- A2 Verifier 消融未纳入。"])
    lines.extend(
        [
            "",
            "- A1 固定分块、A3 流程 DAG 为 N/A。",
            "",
            "## 限制",
            "",
            "- 50 题冻结集只作方向性证据；比例指标给 Wilson 95% 区间。",
            "- A1 固定分块、A3 流程 DAG 未执行（缺少相应实现），报告标 N/A。",
            "- support 与 answer correctness 必须是人工标注；AI 预标注不计入。",
            f"- 单标注人：{', '.join(report.get('annotators') or []) or '未标注'}；"
            "未执行 20% 第二人复核/隔天盲审，按设计披露。",
            "- 人工修改处数（改事实/改条件/删无依据句/补引用）未分项记录，标 N/A。",
            "- token/成本：当前 run row 未落盘 token，且无统一单价配置，标 N/A。",
            "- remediation2 对 32 条 support、11 题 answer 完全一致项沿用第一轮标签；"
            "设计原要求旧 run 标签不复用，此为记录在案的偏差。",
            f"- p50/p95 实际为 {report['latency_ms']['p50']:.0f}/"
            f"{report['latency_ms']['p95']:.0f} ms，未达 6s/10s；"
            "按设计记录实际值，不删题、不改门禁。",
            "",
        ]
    )
    return "\n".join(lines)
