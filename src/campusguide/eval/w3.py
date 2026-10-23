"""W3 开发集指标：引用支持、拒答混淆矩阵与错误归因（设计第 10 节）。

纯函数，供 scripts/eval_w3.py 与单元测试共用；20 题样本只作为
"开发集方向性证据"，不声称统计显著。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campusguide.domain.text import normalize_text

ANSWER_OUTCOMES = {"ANSWERED", "PARTIAL_ANSWER"}

# 错误归因优先级：检索缺失优先于校验失败，避免把检索漏归因成校验过严
_ATTRIBUTION_ISSUE_PRIORITY = [
    "NUMBER_NOT_SUPPORTED",
    "QUOTE_MISMATCH",
    "AUDIENCE_MISMATCH",
    "VERSION_CONFLICT",
    "VERSION_MISMATCH",
    "ONLY_REPEALED",
]


def load_support_labels(path: Path) -> list[dict[str, Any]]:
    """读取 w3_support_labels.jsonl；文件不存在返回空列表。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.open("r", encoding="utf-8"), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
    return rows


def label_index(labels: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], str]:
    """(question_id, claim_text_normalized, 序号) -> label。

    同一 claim 文本重复时按出现顺序用序号区分（设计 10.2）。
    """
    index: dict[tuple[str, str, int], str] = {}
    seen: Counter[tuple[str, str]] = Counter()
    for row in labels:
        raw_text = row.get("claim_text_normalized") or row.get("claim_text") or ""
        key = (str(row.get("question_id", "")), normalize_text(str(raw_text)))
        explicit = row.get("occurrence")
        if explicit is None:
            occurrence = seen[key]
            seen[key] += 1
        else:
            occurrence = int(explicit)
        index[(key[0], key[1], occurrence)] = str(row.get("label", "unlabeled"))
    return index


def match_labels(
    index: dict[tuple[str, str, int], str],
    rows: list[dict[str, Any]],
) -> None:
    """就地给每行 claims 写入 label 字段（supported/partial/unsupported/unlabeled）。"""
    counters: Counter[tuple[str, str]] = Counter()
    for row in rows:
        for claim in row.get("claims", []):
            key = (row["id"], normalize_text(claim["text"]))
            occurrence = counters[key]
            counters[key] += 1
            claim["label"] = index.get((key[0], key[1], occurrence), "unlabeled")


_VALID_LABELS = {"supported", "partial", "unsupported", "unlabeled"}


def _support_stats(claims: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(
        claim.get("label") if claim.get("label") in _VALID_LABELS else "unlabeled"
        for claim in claims
    )
    labeled = counts["supported"] + counts["partial"] + counts["unsupported"]
    return {
        "total": len(claims),
        "supported": counts["supported"],
        "partial": counts["partial"],
        "unsupported": counts["unsupported"],
        "unlabeled": counts["unlabeled"],
        "support_rate": (counts["supported"] / labeled) if labeled else None,
    }


def _attribute(row: dict[str, Any]) -> str | None:
    """失败题的唯一主因（设计 10.4）。"""
    expected = row["expected_outcome"]
    outcome = row["outcome"]
    if expected == "ANSWERED":
        if outcome in ANSWER_OUTCOMES:
            # ANSWERED 正常；PARTIAL_ANSWER 本身不算失败，partial 率单独报告
            return None
        if outcome == "GENERATION_FAILED":
            return "GENERATION_FAILED"
        if row.get("recall_at_8") == 0:
            return "RETRIEVAL_MISS"
        issue_codes = {issue.get("code") for issue in row.get("issues", [])}
        if outcome in _ATTRIBUTION_ISSUE_PRIORITY:
            return outcome
        for code in _ATTRIBUTION_ISSUE_PRIORITY:
            if code in issue_codes:
                return code
        return "OVER_REFUSAL"
    # 期望拒答但给出了答案，或原因码不符：标注为需人工复核
    if outcome in ANSWER_OUTCOMES or outcome != expected:
        return "LABEL_ERROR"
    return None


def build_report(
    rows: list[dict[str, Any]],
    *,
    labels: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    match_labels(label_index(labels), rows)

    answerable = [row for row in rows if row["expected_outcome"] == "ANSWERED"]
    non_answerable = [row for row in rows if row["expected_outcome"] != "ANSWERED"]

    citations_total = sum(row.get("citations_total", 0) for row in rows)
    valid_citations = sum(row.get("valid_citations", 0) for row in rows)
    fabricated = sum(row.get("fabricated_id_count", 0) for row in rows)
    quotes_total = sum(row.get("quotes_total", 0) for row in rows)
    quotes_located = sum(row.get("quotes_located", 0) for row in rows)

    draft_stats = _support_stats([claim for row in rows for claim in row.get("claims", [])])
    rendered_stats = _support_stats(
        [claim for row in rows for claim in row.get("claims", []) if claim.get("rendered")]
    )

    def abstained(row: dict[str, Any]) -> bool:
        return row["outcome"] not in ANSWER_OUTCOMES

    issue_distribution = Counter(
        issue.get("code", "UNKNOWN") for row in rows for issue in row.get("issues", [])
    )
    confusion: Counter[tuple[str, str]] = Counter(
        (row["expected_outcome"], row["outcome"]) for row in rows
    )
    for row in rows:
        row["error_attribution"] = _attribute(row)
    attributions = Counter(row["error_attribution"] for row in rows if row["error_attribution"])

    durations = [max(row["duration_ms"] - row.get("probe_ms", 0), 0) for row in rows]
    probe_rows = [row for row in rows if row.get("probe_ms")]
    summary = {
        "questions": len(rows),
        "answerable": len(answerable),
        "non_answerable": len(non_answerable),
        "citation_id_valid_rate": (valid_citations / citations_total) if citations_total else None,
        "fabricated_id_count": fabricated,
        "quote_locatable_rate": (quotes_located / quotes_total) if quotes_total else None,
        "draft_support": draft_stats,
        "rendered_support": rendered_stats,
        "partial_rate": (
            sum(1 for row in answerable if row["outcome"] == "PARTIAL_ANSWER") / len(answerable)
            if answerable
            else None
        ),
        "abstention_recall": (
            sum(1 for row in non_answerable if abstained(row)) / len(non_answerable)
            if non_answerable
            else None
        ),
        "over_refusal_rate": (
            sum(1 for row in answerable if abstained(row)) / len(answerable) if answerable else None
        ),
        "reason_code_correct_rate": (
            sum(1 for row in non_answerable if row["outcome"] == row["expected_outcome"])
            / len(non_answerable)
            if non_answerable
            else None
        ),
        "issue_distribution": dict(sorted(issue_distribution.items())),
        "probe": {
            "questions": len(probe_rows),
            "total_ms": sum(row.get("probe_ms", 0) for row in probe_rows),
            "max_ms": max((row.get("probe_ms", 0) for row in probe_rows), default=0),
        },
        "p50_latency_ms": _percentile(durations, 0.50),
        "p95_latency_ms": _percentile(durations, 0.95),
    }
    summary["rendered_support_gate_ok"] = (
        rendered_stats["support_rate"] is not None and rendered_stats["support_rate"] >= 0.85
    )
    summary["abstention_gate_ok"] = (summary["abstention_recall"] or 0.0) >= 0.80 and (
        summary["over_refusal_rate"] or 0.0
    ) <= 0.20
    struct_ok = (
        fabricated == 0
        and (summary["citation_id_valid_rate"] in (None, 1.0))
        and (summary["quote_locatable_rate"] in (None, 1.0))
    )

    return {
        **metadata,
        "note": (
            "W3 开发集 v0，仅用于方向性证据，不是冻结测试集；"
            "支持率基于人工标注，unlabeled 不计入支持率分母。"
        ),
        "support_labels": {
            "count": len(labels),
            "annotators": sorted(
                {str(label.get("annotator", "")) for label in labels if label.get("annotator")}
            ),
            "provisional": any(
                str(label.get("annotator", "")).startswith("qoder") for label in labels
            ),
        },
        "summary": summary,
        "struct_gate_ok": struct_ok,
        "w3_gate_ok": struct_ok
        and summary["abstention_gate_ok"]
        and summary["rendered_support_gate_ok"],
        "confusion_matrix": [
            {"expected": expected, "outcome": outcome, "count": count}
            for (expected, outcome), count in sorted(confusion.items())
        ],
        "error_attribution": dict(sorted(attributions.items())),
        "rows": rows,
    }


def _percentile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]

    def rate(value: Any) -> str:
        return f"{value:.1%}" if isinstance(value, (int, float)) else "n/a"

    lines = [
        "# CampusGuide W3 开发集报告",
        "",
        f"- 数据集：`{report.get('dataset', 'dev_v0.jsonl')}`（开发集 v0，**非冻结测试集**）",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- commit：`{report.get('git_commit', 'n/a')}`",
        f"- 模型：{report.get('advisor_model', 'n/a')}",
        f"- 运行模式：{'warm' if report.get('warm', True) else 'cold'}",
        (
            f"- 支持标注：{report.get('support_labels', {}).get('count', 0)} 条，"
            f"标注人 {', '.join(report.get('support_labels', {}).get('annotators', []) or ['n/a'])}"
            + (
                "（**AI 预标注，待人工确认**）"
                if report.get("support_labels", {}).get("provisional")
                else ""
            )
        ),
        "- 标注口径：严格引文级——引文自身须支撑 claim 的实质内容；所属条款的引导句/上下文不计入。",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 题数 | {summary['questions']} |",
        f"| 引用 ID 有效率 | {rate(summary['citation_id_valid_rate'])} |",
        f"| 编造 ID 数 | {summary['fabricated_id_count']} |",
        f"| quote 可定位率（原文 span） | {rate(summary['quote_locatable_rate'])} |",
        (
            f"| draft 支持率 | {rate(summary['draft_support']['support_rate'])} "
            f"（unlabeled {summary['draft_support']['unlabeled']}） |"
        ),
        (
            f"| rendered 支持率 | {rate(summary['rendered_support']['support_rate'])} "
            f"（unlabeled {summary['rendered_support']['unlabeled']}） |"
        ),
        (
            f"| partial 率 | {rate(summary['partial_rate'])} |"
            if summary["partial_rate"] is not None
            else "| partial 率 | n/a |"
        ),
        f"| 拒答召回 | {rate(summary['abstention_recall'])} |",
        f"| 误拒率 | {rate(summary['over_refusal_rate'])} |",
        f"| 原因码正确率 | {rate(summary['reason_code_correct_rate'])} |",
        (
            f"| p50 / p95（warm{'，不含探针' if summary['probe']['questions'] else ''}） | "
            f"{summary['p50_latency_ms']:.0f} / {summary['p95_latency_ms']:.0f} ms |"
        ),
        (
            f"| 废止探针 | {summary['probe']['questions']} 题，"
            f"合计 {summary['probe']['total_ms']} ms，最大 {summary['probe']['max_ms']} ms |"
            if summary["probe"]["questions"]
            else "| 废止探针 | 本快照未记录 probe_ms（旧版 runner）；下次运行起单独记录 |"
        ),
        f"| 结构门禁 | {'通过' if report['struct_gate_ok'] else '未通过'} |",
        f"| 拒答门禁 | {'通过' if summary['abstention_gate_ok'] else '未通过'} |",
        f"| rendered 支持率门禁（≥85%） | {'通过' if summary['rendered_support_gate_ok'] else '未通过'} |",
        f"| W3 总门禁 | {'通过' if report['w3_gate_ok'] else '未通过'} |",
        "",
        "## issue 分布",
        "",
        "| code | 数量 |",
        "|---|---:|",
    ]
    for code, count in summary["issue_distribution"].items():
        lines.append(f"| {code} | {count} |")

    lines.extend(["", "## 拒答混淆矩阵", "", "| 期望 | 实际 | 数量 |", "|---|---|---:|"])
    for cell in report["confusion_matrix"]:
        lines.append(f"| {cell['expected']} | {cell['outcome']} | {cell['count']} |")

    lines.extend(["", "## 错误归因", "", "| 主因 | 数量 |", "|---|---:|"])
    for cause, count in report["error_attribution"].items():
        lines.append(f"| {cause} | {count} |")

    lines.extend(
        [
            "",
            "## 逐题结果",
            "",
            "| ID | 类别 | outcome | 期望 | 主因 | partial | 引用 | 延迟(ms) |",
            "|---|---|---|---|---|---|---:|---:|",
        ]
    )
    for row in report["rows"]:
        lines.append(
            f"| {row['id']} | {row['category']} | {row['outcome']} | {row['expected_outcome']} | "
            f"{row.get('error_attribution') or '—'} | {'是' if row.get('partial') else '否'} | "
            f"{row.get('citations_total', 0)} | {row['duration_ms']} |"
        )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- 20 题样本仅提供方向性证据；支持率依赖单标注人，W7 冻结测试集再报告区间。",
            "- 严格引文级口径下，rendered 支持率未达 85% 的主因是**引文过窄**："
            "d005 的引文只截取第八条情形列举项（处分后果在条款引导句），"
            "d008 的引文只截取条件项（“申请励志奖学金应同时符合…”的匹配句未入选）；"
            "若按条款上下文口径，两条可判 supported。W6 用重写/提示词改进引文跨度。",
            "- p95 沿用 W2 已知限制（MiMo 长尾），W3 未新增 LLM 调用，不新增延迟达标声明。",
            "- 版本冲突只在 Evidence 内检测；不同生效日的届别版本按 warning 保留，"
            "完整版本解析在 W4 接入。",
        ]
    )
    return "\n".join(lines) + "\n"


def default_metadata(
    *,
    dataset: str,
    git_commit: str,
    advisor_model: str,
    vector_manifest_sha256: str,
    config_hash: str,
) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": git_commit,
        "advisor_model": advisor_model,
        "vector_manifest_sha256": vector_manifest_sha256,
        "config_hash": config_hash,
        "warm": True,  # runner 在评测前调用 service.warmup()
    }
