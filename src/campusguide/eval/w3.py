"""W3 v2 评测：run 快照、support 标注、拒答混淆矩阵与报告门禁。

本模块只做确定性的数据处理，不调用 LLM；live 运行由 scripts/eval_w3.py
负责。support 标注必须由人工填写 ``annotator``；AI 预填不计入门禁。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campusguide.domain.enums import VerificationDecision
from campusguide.domain.text import normalize_text
from campusguide.qa.service import QAResult
from campusguide.verification.quotes import locate_quote

SCHEMA_VERSION = "w3-run-v1"
SUPPORT_LABELS = {"supported", "partial", "unsupported", "unlabeled"}
LABELED = {"supported", "partial", "unsupported"}
RENDERED_OUTCOMES = {"ANSWERED", "PARTIAL_ANSWER"}


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_run_hash(run: dict[str, Any]) -> str:
    payload = {key: value for key, value in run.items() if key != "run_sha256"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def finalize_run(run: dict[str, Any]) -> dict[str, Any]:
    payload = dict(run)
    payload.pop("run_sha256", None)
    payload["run_sha256"] = compute_run_hash(payload)
    return payload


def verify_run_hash(run: dict[str, Any]) -> None:
    expected = run.get("run_sha256")
    actual = compute_run_hash(run)
    if expected != actual:
        raise ValueError("run hash 不一致：run_sha256 与 run 内容不符")


def percentile(values: list[int], ratio: float) -> float:
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


def summarize_structure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    valid = 0
    fabricated = 0
    wrong_part = 0
    quotes_total = 0
    quotes_located = 0
    pass_with_blocking = 0
    for row in rows:
        structure = row.get("structure") or {}
        total += int(structure.get("citations_total", 0) or 0)
        valid += int(structure.get("valid_citations", 0) or 0)
        fabricated += int(structure.get("fabricated_id_count", 0) or 0)
        wrong_part += int(structure.get("wrong_part_count", 0) or 0)
        quotes_total += int(structure.get("quotes_total", 0) or 0)
        quotes_located += int(structure.get("quotes_located", 0) or 0)
        if structure.get("pass_with_blocking"):
            pass_with_blocking += 1

    citation_rate = (valid / total) if total else 1.0
    quote_rate = (quotes_located / quotes_total) if quotes_total else 1.0
    ok = (
        fabricated == 0
        and wrong_part == 0
        and pass_with_blocking == 0
        and (total == 0 or valid == total)
        and (quotes_total == 0 or quotes_located == quotes_total)
    )
    return {
        "citations_total": total,
        "valid_citations": valid,
        "citation_id_valid_rate": citation_rate,
        "fabricated_id_count": fabricated,
        "wrong_part_count": wrong_part,
        "quotes_total": quotes_total,
        "quotes_located": quotes_located,
        "quote_locatable_rate": quote_rate,
        "pass_with_blocking_count": pass_with_blocking,
        "ok": ok,
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(row.get("outcome", "UNKNOWN") for row in rows)
    decisions = Counter(row.get("decision") or "none" for row in rows)
    qa_latencies = [int(row.get("qa_ms", row.get("duration_ms", 0)) or 0) for row in rows]
    probe_latencies = [
        int(row.get("probe_ms", 0) or 0) for row in rows if int(row.get("probe_ms", 0) or 0) > 0
    ]
    structure = summarize_structure(rows)
    answerable = sum(1 for row in rows if row.get("expected_outcome") == "ANSWERED")
    return {
        "questions": len(rows),
        "outcomes": dict(outcomes),
        "decisions": dict(decisions),
        "answerable": answerable,
        "non_answerable": len(rows) - answerable,
        "partial_count": outcomes.get("PARTIAL_ANSWER", 0),
        "structure": structure,
        "structure_gate_ok": structure["ok"],
        "p50_latency_ms": percentile(qa_latencies, 0.50),
        "p95_latency_ms": percentile(qa_latencies, 0.95),
        "probe_count": len(probe_latencies),
        "probe_p50_latency_ms": (percentile(probe_latencies, 0.50) if probe_latencies else None),
        "probe_p95_latency_ms": (percentile(probe_latencies, 0.95) if probe_latencies else None),
        "llm_models": sorted({row.get("llm_model") for row in rows if row.get("llm_model")}),
    }


def build_run_snapshot(
    *,
    dataset: str,
    dataset_path: str | Path,
    rows: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    run = {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "dataset_sha256": sha256_file(dataset_path),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "frozen": False,
        "metadata": metadata,
        "summary": summarize_rows(rows),
        "rows": rows,
    }
    return finalize_run(run)


def load_run(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "rows" not in payload:
        raise ValueError(f"run 文件结构不合法：{path}")
    return payload


def save_run(run: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _draft_label_record(
    row: dict[str, Any],
    draft: dict[str, Any],
    run_sha256: str,
) -> dict[str, Any]:
    draft_citations = draft.get("draft_citations") or []
    located = draft.get("citations") or []
    fact_coverage = draft.get("fact_coverage") or {}
    return {
        "run_sha256": run_sha256,
        "question_id": row["id"],
        "claim_id": draft["claim_id"],
        "text": draft.get("text", ""),
        "quotes": [item.get("quote", "") for item in draft_citations],
        "located_quotes": [item.get("quote", "") for item in located],
        "kept": bool(draft.get("kept")),
        "issues": [issue.get("code", "") for issue in (draft.get("issues") or [])],
        "fact_uncovered": list(fact_coverage.get("uncovered") or []),
        "label": "unlabeled",
        "annotator": "",
        "labeled_at": None,
        "note": "",
    }


def _label_match_key(record: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    text = normalize_text(str(record.get("text") or ""))
    quotes = tuple(
        sorted(
            normalize_text(str(quote))
            for quote in (record.get("located_quotes") or record.get("quotes") or [])
        )
    )
    return str(record.get("question_id") or ""), text, quotes


def build_label_template(
    run: dict[str, Any],
    *,
    baseline_labels: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    run_sha256 = run.get("run_sha256") or compute_run_hash(run)
    baseline_by_key: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for item in baseline_labels or []:
        if effective_label(item) == "unlabeled":
            continue
        baseline_by_key[_label_match_key(item)] = item

    records: list[dict[str, Any]] = []
    for row in run.get("rows", []):
        for draft in row.get("drafts", []):
            record = _draft_label_record(row, draft, run_sha256)
            match = baseline_by_key.get(_label_match_key(record))
            if match is not None:
                record["prefill_label"] = effective_label(match)
                record["prefill_note"] = match.get("note", "")
                record["prefill_source"] = "baseline"
                record["prefill_from"] = f"{match.get('question_id')}/{match.get('claim_id')}"
            records.append(record)
    return records


def write_labels(labels: list[dict[str, Any]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False) for item in labels]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_labels(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{line_no} 必须是对象")
            for field in ("run_sha256", "question_id", "claim_id", "label"):
                if field not in item:
                    raise ValueError(f"{path}:{line_no} 缺少字段 {field}")
            if item["label"] not in SUPPORT_LABELS:
                raise ValueError(f"{path}:{line_no} label 不合法：{item['label']}")
            item.setdefault("annotator", "")
            item.setdefault("labeled_at", None)
            item.setdefault("note", "")
            records.append(item)
    return records


def merge_labels(
    run: dict[str, Any],
    labels: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    run_sha256 = run.get("run_sha256") or compute_run_hash(run)
    known = {
        (row["id"], draft["claim_id"])
        for row in run.get("rows", [])
        for draft in row.get("drafts", [])
    }
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in labels:
        if item.get("run_sha256") != run_sha256:
            raise ValueError("support labels 的 run_sha256 与 run 不一致")
        key = (item["question_id"], item["claim_id"])
        if key in merged:
            raise ValueError(f"support labels 存在重复 claim：{key}")
        if key not in known:
            raise ValueError(f"support labels 引用了不存在的 claim：{key}")
        merged[key] = item
    return merged


def _effective_label(record: dict[str, Any] | None) -> str:
    if record is None:
        return "unlabeled"
    label = str(record.get("label") or "unlabeled").strip().lower()
    if label not in LABELED:
        return "unlabeled"
    annotator = str(record.get("annotator") or "").strip()
    if not annotator or annotator.lower().startswith("ai"):
        return "unlabeled"
    return label


def effective_label(record: dict[str, Any] | None) -> str:
    """公开的标签有效性判断：空/ai annotator 视为 unlabeled。"""
    return _effective_label(record)


def apply_support_label(
    record: dict[str, Any],
    label: str,
    *,
    annotator: str,
    note: str | None = None,
    labeled_at: str | None = None,
) -> dict[str, Any]:
    """写入一条人工 support 标签，返回新记录（不修改入参）。"""
    if label not in LABELED:
        raise ValueError("label 必须是 supported / partial / unsupported")
    annotator = annotator.strip()
    if not annotator or annotator.lower().startswith("ai"):
        raise ValueError("annotator 必须是人工标注者，不能为空或以 ai 开头")
    updated = dict(record)
    updated["label"] = label
    updated["annotator"] = annotator
    updated["labeled_at"] = labeled_at or datetime.now(UTC).isoformat(timespec="seconds")
    if note is not None:
        updated["note"] = note.strip()
    return updated


def label_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_effective_label(record) for record in records)
    return {key: counts.get(key, 0) for key in ("supported", "partial", "unsupported", "unlabeled")}


def next_unlabeled_index(
    records: list[dict[str, Any]],
    *,
    include_labeled: bool = False,
) -> int | None:
    for index, record in enumerate(records):
        if include_labeled or _effective_label(record) == "unlabeled":
            return index
    return None


def render_label_sheet(
    records: list[dict[str, Any]],
    questions: dict[str, str],
    *,
    title: str = "W3 support 标注核对表",
) -> str:
    """生成人工核对用的 Markdown 表；不写入、不替人判定。"""
    counts = label_counts(records)
    run_sha256 = records[0].get("run_sha256", "") if records else ""
    lines = [
        f"# {title}",
        "",
        f"- run_sha256：`{run_sha256}`",
        f"- 总数：{len(records)}；已标注："
        f"{counts['supported'] + counts['partial'] + counts['unsupported']}；"
        f"unlabeled：{counts['unlabeled']}",
        "",
        "## 判定口径（严格引文级）",
        "",
        "- **supported**：quote 本身直接支持 claim 的主要事实，数字/日期/条件/范围一致。",
        "- **partial**：quote 支持主要事实，但 claim 多了关键条件/范围/第二个断言，"
        "或措辞强弱不一致。",
        "- **unsupported**：quote 与 claim 的主要事实不符、矛盾，或只是主题相关。",
        "- 不看条款上下文，不看问题，不补外部知识；只判断 quote 文本。",
        "",
        "## 待核对条目",
        "",
    ]
    for index, record in enumerate(records, start=1):
        label = _effective_label(record)
        annotator = str(record.get("annotator") or "").strip()
        current = label if label == "unlabeled" else f"{label}（{annotator}）"
        kept = "kept" if record.get("kept") else "dropped"
        if record.get("dropped_reason"):
            kept += f"（{record['dropped_reason']}）"
        located = record.get("located_quotes") or record.get("quotes") or []
        lines.extend(
            [
                f"## {index:02d}. {record['question_id']} / {record['claim_id']}",
                "",
                f"- 状态：{kept}",
                f"- 问题：{questions.get(record['question_id'], '')}",
                f"- claim：{record.get('text', '')}",
                "- quote（located）：",
                "",
            ]
        )
        if located:
            lines.extend(f"  > {quote}" for quote in located)
        else:
            lines.append("  > （无可用 quote）")
        issues = record.get("issues") or []
        facts = record.get("fact_uncovered") or []
        lines.extend(
            [
                "",
                f"- issues：{', '.join(issues) if issues else '—'}",
                f"- fact_uncovered：{', '.join(facts) if facts else '—'}",
                f"- 当前标签：{current}",
            ]
        )
        if record.get("prefill_label"):
            lines.append(
                f"- prefill 建议：{record['prefill_label']}"
                f"（来自 {record.get('prefill_source', 'baseline')}，待确认）"
            )
            if record.get("prefill_note"):
                lines.append(f"- prefill note：{record['prefill_note']}")
        lines.extend(
            [
                "- 判定：[ ] supported  [ ] partial  [ ] unsupported",
                "- note：",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def support_metrics(
    run: dict[str, Any],
    labels: list[dict[str, Any]] | None,
    *,
    support_gate: float = 0.85,
) -> dict[str, Any]:
    labels_by_key = merge_labels(run, labels) if labels is not None else {}

    drafts_total = 0
    drafts_labeled = 0
    drafts_unlabeled = 0
    drafts_supported = 0
    drafts_partial = 0
    drafts_unsupported = 0
    rendered_total = 0
    rendered_labeled = 0
    rendered_unlabeled = 0
    rendered_supported = 0
    rendered_partial = 0
    rendered_unsupported = 0
    unlabeled_ids: list[str] = []

    for row in run.get("rows", []):
        rendered_ids = set(row.get("rendered_claim_ids") or [])
        for draft in row.get("drafts", []):
            drafts_total += 1
            key = (row["id"], draft["claim_id"])
            label = _effective_label(labels_by_key.get(key))
            if label == "unlabeled":
                drafts_unlabeled += 1
                unlabeled_ids.append(f"{row['id']}/{draft['claim_id']}")
            else:
                drafts_labeled += 1
                if label == "supported":
                    drafts_supported += 1
                elif label == "partial":
                    drafts_partial += 1
                else:
                    drafts_unsupported += 1

            rendered = bool(draft.get("kept")) and draft["claim_id"] in rendered_ids
            if not rendered:
                continue
            rendered_total += 1
            if label == "unlabeled":
                rendered_unlabeled += 1
            else:
                rendered_labeled += 1
                if label == "supported":
                    rendered_supported += 1
                elif label == "partial":
                    rendered_partial += 1
                else:
                    rendered_unsupported += 1

    draft_support_rate = drafts_supported / drafts_labeled if drafts_labeled > 0 else None
    rendered_support_rate = rendered_supported / rendered_labeled if rendered_labeled > 0 else None
    labels_complete = drafts_unlabeled == 0
    rendered_labels_complete = rendered_unlabeled == 0
    gate_ok = (
        labels_complete
        and rendered_labels_complete
        and rendered_total > 0
        and rendered_support_rate is not None
        and rendered_support_rate >= support_gate
    )
    if not labels_complete:
        gate_reason = "存在未标注的 draft claim（含非人工标注）"
    elif not rendered_labels_complete:
        gate_reason = "存在未标注的 rendered claim"
    elif rendered_total == 0:
        gate_reason = "没有可计算支持率的 rendered claim"
    elif rendered_support_rate is None or rendered_support_rate < support_gate:
        gate_reason = (
            f"rendered 支持率 {0.0 if rendered_support_rate is None else rendered_support_rate:.1%} "
            f"低于 {support_gate:.0%}"
        )
    else:
        gate_reason = "通过"

    return {
        "support_gate": support_gate,
        "drafts_total": drafts_total,
        "drafts_labeled": drafts_labeled,
        "drafts_unlabeled": drafts_unlabeled,
        "drafts_supported": drafts_supported,
        "drafts_partial": drafts_partial,
        "drafts_unsupported": drafts_unsupported,
        "draft_support_rate": draft_support_rate,
        "rendered_total": rendered_total,
        "rendered_labeled": rendered_labeled,
        "rendered_unlabeled": rendered_unlabeled,
        "rendered_supported": rendered_supported,
        "rendered_partial": rendered_partial,
        "rendered_unsupported": rendered_unsupported,
        "rendered_support_rate": rendered_support_rate,
        "labels_complete": labels_complete,
        "rendered_labels_complete": rendered_labels_complete,
        "unlabeled_claim_ids": unlabeled_ids,
        "gate_ok": gate_ok,
        "gate_reason": gate_reason,
    }


def abstention_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = 0
    non_answerable = 0
    true_positive = 0
    false_negative = 0
    false_positive = 0
    true_negative = 0
    partial = 0
    reason_correct = 0

    for row in rows:
        expected = row.get("expected_outcome")
        outcome = row.get("outcome")
        rendered = outcome in RENDERED_OUTCOMES
        if expected == "ANSWERED":
            answerable += 1
            if rendered:
                true_positive += 1
            else:
                false_negative += 1
            if outcome == "PARTIAL_ANSWER":
                partial += 1
        else:
            non_answerable += 1
            if rendered:
                false_positive += 1
            else:
                true_negative += 1
            if outcome == expected:
                reason_correct += 1

    return {
        "answerable": answerable,
        "non_answerable": non_answerable,
        "true_positive": true_positive,
        "false_negative": false_negative,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "refusal_recall": (true_negative / non_answerable if non_answerable else None),
        "false_refusal_rate": false_negative / answerable if answerable else None,
        "partial_rate": partial / answerable if answerable else None,
        "reason_code_correct": reason_correct,
        "reason_code_accuracy": (reason_correct / non_answerable if non_answerable else None),
    }


def issue_distribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        for issue in row.get("issues", []):
            counts[
                (
                    issue.get("code", "UNKNOWN"),
                    issue.get("scope", "unknown"),
                    issue.get("severity", "unknown"),
                )
            ] += 1
    items = [
        {"code": code, "scope": scope, "severity": severity, "count": count}
        for (code, scope, severity), count in counts.items()
    ]
    items.sort(key=lambda item: (-item["count"], item["code"], item["scope"]))
    return items


_MISSING_RETRIEVAL = {"NO_EVIDENCE": "RETRIEVAL_MISS"}
_VERSION_OUTCOMES = {"VERSION_MISMATCH", "VERSION_CONFLICT"}
_QUOTE_CODES = {
    "QUOTE_MISMATCH",
    "QUOTE_TOO_SHORT",
    "QUOTE_ELLIPSIS",
    "NO_VALID_CITATION",
    "CITATION_NOT_IN_EVIDENCE",
}


def attribute_row(
    row: dict[str, Any],
    labels_by_key: dict[tuple[str, str], dict[str, Any]],
) -> str | None:
    expected = row.get("expected_outcome")
    outcome = row.get("outcome")
    rendered = outcome in RENDERED_OUTCOMES
    issue_codes = {issue.get("code") for issue in row.get("issues", [])}
    rendered_ids = list(row.get("rendered_claim_ids") or [])
    drafts_by_id = {draft["claim_id"]: draft for draft in row.get("drafts", [])}

    if expected == "ANSWERED":
        if outcome == "PARTIAL_ANSWER":
            return "PARTIAL_INCOMPLETE"
        if not rendered:
            if outcome == "NO_EVIDENCE":
                return "RETRIEVAL_MISS"
            if outcome == "ONLY_REPEALED":
                return "ONLY_REPEALED"
            if outcome in _VERSION_OUTCOMES:
                return "VERSION_MISMATCH"
            if outcome == "GENERATION_FAILED":
                return "GENERATION_FAILED"
            if outcome == "INDEX_UNAVAILABLE":
                return "INDEX_UNAVAILABLE"
            if "NUMBER_NOT_SUPPORTED" in issue_codes:
                return "NUMBER_NOT_SUPPORTED"
            if "PRIVACY_VIOLATION" in issue_codes:
                return "PRIVACY_VIOLATION"
            if "AUDIENCE_MISMATCH" in issue_codes:
                return "AUDIENCE_MISMATCH"
            if issue_codes & _QUOTE_CODES:
                return "QUOTE_MISMATCH"
            return "OVER_REFUSAL"

        unsupported = []
        partial = []
        for claim_id in rendered_ids:
            label = _effective_label(labels_by_key.get((row["id"], claim_id)))
            if label == "unsupported":
                unsupported.append(claim_id)
            elif label == "partial":
                partial.append(claim_id)
        if unsupported:
            for claim_id in unsupported:
                draft = drafts_by_id.get(claim_id) or {}
                coverage = draft.get("fact_coverage") or {}
                if coverage.get("uncovered"):
                    return "NUMBER_NOT_SUPPORTED"
            return "QUOTE_MISMATCH"
        if partial:
            return "PARTIAL_INCOMPLETE"
        return None

    if rendered:
        return "OVER_ANSWER"
    if outcome != expected:
        return "REASON_CODE_MISMATCH"
    return None


def build_report(
    run: dict[str, Any],
    labels: list[dict[str, Any]] | None = None,
    *,
    support_gate: float = 0.85,
    refusal_recall_gate: float = 0.80,
    false_refusal_gate: float = 0.20,
) -> dict[str, Any]:
    rows = [dict(row) for row in run.get("rows", [])]
    labels_by_key = merge_labels(run, labels) if labels is not None else {}
    for row in rows:
        row["error_attribution"] = attribute_row(row, labels_by_key)

    support = support_metrics(run, labels, support_gate=support_gate)
    abstention = abstention_metrics(rows)
    structure = summarize_structure(rows)
    distribution = issue_distribution(rows)
    attribution_counts = Counter(
        row["error_attribution"] for row in rows if row.get("error_attribution")
    )

    support_ok = support["gate_ok"]
    recall_ok = (
        abstention["refusal_recall"] is not None
        and abstention["refusal_recall"] >= refusal_recall_gate
    )
    false_refusal_ok = (
        abstention["false_refusal_rate"] is not None
        and abstention["false_refusal_rate"] <= false_refusal_gate
    )
    overall = structure["ok"] and support_ok and recall_ok and false_refusal_ok

    blocking_reasons: list[str] = []
    if not structure["ok"]:
        blocking_reasons.append("结构门禁未通过")
    if labels is None or not support["labels_complete"]:
        blocking_reasons.append("未完成人工 support 标注")
    if not support_ok:
        blocking_reasons.append(f"support 门禁未通过：{support['gate_reason']}")
    if not recall_ok:
        blocking_reasons.append(f"拒答召回未达 {refusal_recall_gate:.0%}")
    if not false_refusal_ok:
        blocking_reasons.append(f"误拒率超过 {false_refusal_gate:.0%}")

    gates = {
        "structure": {"ok": structure["ok"]},
        "support": {
            "ok": support_ok,
            "value": support["rendered_support_rate"],
            "gate": support_gate,
            "labels_complete": support["labels_complete"],
            "reason": support["gate_reason"],
        },
        "refusal_recall": {
            "ok": recall_ok,
            "value": abstention["refusal_recall"],
            "gate": refusal_recall_gate,
        },
        "false_refusal": {
            "ok": false_refusal_ok,
            "value": abstention["false_refusal_rate"],
            "gate": false_refusal_gate,
        },
        "w3_overall": overall,
        "blocking_reasons": blocking_reasons,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": run.get("dataset", ""),
        "dataset_sha256": run.get("dataset_sha256", ""),
        "frozen": False,
        "run_sha256": run.get("run_sha256", ""),
        "metadata": run.get("metadata", {}),
        "summary": run.get("summary", {}),
        "structure": structure,
        "support": support,
        "abstention": abstention,
        "issue_distribution": distribution,
        "error_attribution": {
            "counts": dict(attribution_counts),
            "rows": [
                {"id": row["id"], "code": row["error_attribution"]}
                for row in rows
                if row.get("error_attribution")
            ],
        },
        "gates": gates,
        "labels_provided": labels is not None and len(labels) > 0,
        "rows": rows,
        "note": "W3 开发集 v0，非冻结测试集；support 只统计人工标注。",
    }


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def render_markdown(report: dict[str, Any]) -> str:
    metadata = report.get("metadata", {})
    summary = report.get("summary", {})
    support = report.get("support", {})
    abstention = report.get("abstention", {})
    structure = report.get("structure", {})
    gates = report.get("gates", {})
    latency_scope = metadata.get("latency_scope", "warm")
    vector_manifest = metadata.get("vector_manifest") or {}
    prompt_version = str(metadata.get("advisor_prompt_version") or "")
    retrieval_mode = str(metadata.get("retrieval_mode") or "")
    title = (
        "CampusGuide W6-A 开发集报告"
        if prompt_version.startswith("w6")
        else "CampusGuide W3 开发集报告"
    )
    gate_label = "W6-A 总门禁" if prompt_version.startswith("w6") else "W3 总门禁"
    support_gate_ok = gates.get("support", {}).get("ok")
    support_gate_text = "通过" if support_gate_ok else f"未通过（{support.get('gate_reason', '')}）"
    lines = [
        f"# {title}",
        "",
        f"- 数据集：`{report.get('dataset', '')}`（开发集 v0，**非冻结测试集**）",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- run_sha256：`{report.get('run_sha256', '')}`",
        f"- git commit：`{metadata.get('git_commit', 'n/a')}`"
        f"（dirty={metadata.get('git_dirty', 'n/a')}）",
        f"- 模型：{', '.join(summary.get('llm_models') or []) or 'n/a'}",
        f"- prompt：{metadata.get('advisor_prompt_version', 'n/a')}"
        f"（max_tokens={metadata.get('advisor_max_tokens', 'n/a')}）",
        f"- 向量 manifest：`{vector_manifest.get('sha256', 'n/a')}`",
        f"- 检索：{retrieval_mode or 'dense（旧口径）'}",
        f"- config hash：`{metadata.get('config_hash', 'n/a')}`",
        f"- 延迟口径：{metadata.get('latency_scope', 'n/a')}"
        f"（warmup {metadata.get('warmup_ms', 0)} ms；探针耗时单列）",
        "",
        "## 门禁",
        "",
        "| 门禁 | 结果 | 数值 |",
        "|---|---|---:|",
        f"| 结构门禁 | {'通过' if gates.get('structure', {}).get('ok') else '未通过'} | "
        f"引用 ID {_fmt_rate(structure.get('citation_id_valid_rate'))} / "
        f"quote {_fmt_rate(structure.get('quote_locatable_rate'))} |",
        f"| support 门禁 | {'通过' if gates.get('support', {}).get('ok') else '未通过'} | "
        f"rendered {_fmt_rate(support.get('rendered_support_rate'))} |",
        f"| 拒答召回 | {'通过' if gates.get('refusal_recall', {}).get('ok') else '未通过'} | "
        f"{_fmt_rate(abstention.get('refusal_recall'))} |",
        f"| 误拒率 | {'通过' if gates.get('false_refusal', {}).get('ok') else '未通过'} | "
        f"{_fmt_rate(abstention.get('false_refusal_rate'))} |",
        f"| {gate_label} | {'通过' if gates.get('w3_overall') else '未通过'} | — |",
        "",
    ]
    reasons = gates.get("blocking_reasons") or []
    if reasons:
        lines.extend(["**未通过原因：**", ""])
        lines.extend(f"- {reason}" for reason in reasons)
        lines.append("")
    if report.get("labels_provided") is False:
        lines.extend(
            [
                "> 未完成人工 support 标注；本报告不声称支持率门禁通过。",
                "",
            ]
        )

    lines.extend(
        [
            "## 汇总",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 题数 | {summary.get('questions', 0)} |",
            f"| 可答题数 | {summary.get('answerable', 0)} |",
            f"| 非可答题数 | {summary.get('non_answerable', 0)} |",
            f"| outcomes | {summary.get('outcomes', {})} |",
            f"| 引用 ID 有效率 | {_fmt_rate(structure.get('citation_id_valid_rate'))} |",
            f"| 编造 ID 数 | {structure.get('fabricated_id_count', 0)} |",
            f"| part_no 错误数 | {structure.get('wrong_part_count', 0)} |",
            f"| quote 可定位率 | {_fmt_rate(structure.get('quote_locatable_rate'))} |",
            f"| p50 延迟（{latency_scope}，排除探针） | {_fmt_ms(summary.get('p50_latency_ms'))} |",
            f"| p95 延迟（{latency_scope}，排除探针） | {_fmt_ms(summary.get('p95_latency_ms'))} |",
            f"| 探针次数 | {summary.get('probe_count', 0)} |",
            f"| 探针 p50 / p95 | {_fmt_ms(summary.get('probe_p50_latency_ms'))} / "
            f"{_fmt_ms(summary.get('probe_p95_latency_ms'))} |",
            "",
            "## support 标注",
            "",
            "严格按引文级口径：只判断 quote 本身是否支撑 claim，不拿条款上下文补足。",
            "unlabeled 不计入分母，但未完成人工标注时门禁不得通过。",
            "",
            "| 指标 | draft | rendered |",
            "|---|---:|---:|",
            f"| 总数 | {support.get('drafts_total', 0)} | {support.get('rendered_total', 0)} |",
            f"| 已标注 | {support.get('drafts_labeled', 0)} | "
            f"{support.get('rendered_labeled', 0)} |",
            f"| unlabeled | {support.get('drafts_unlabeled', 0)} | "
            f"{support.get('rendered_unlabeled', 0)} |",
            f"| supported | {support.get('drafts_supported', 0)} | "
            f"{support.get('rendered_supported', 0)} |",
            f"| partial | {support.get('drafts_partial', 0)} | "
            f"{support.get('rendered_partial', 0)} |",
            f"| unsupported | {support.get('drafts_unsupported', 0)} | "
            f"{support.get('rendered_unsupported', 0)} |",
            f"| 支持率 | {_fmt_rate(support.get('draft_support_rate'))} | "
            f"{_fmt_rate(support.get('rendered_support_rate'))} |",
            "",
            f"- draft 支持率：{_fmt_rate(support.get('draft_support_rate'))}",
            f"- rendered 支持率：{_fmt_rate(support.get('rendered_support_rate'))}",
            f"- 人工标注完整：{'是' if support.get('labels_complete') else '否'}",
            f"- support 门禁：{support_gate_text}",
            "",
            "## 拒答混淆矩阵",
            "",
            "| 预期 \\ 是否渲染 | 渲染答案 | 未渲染答案 |",
            "|---|---:|---:|",
            f"| 可答 | TP={abstention.get('true_positive', 0)} | "
            f"FN={abstention.get('false_negative', 0)} |",
            f"| 非可答 | FP={abstention.get('false_positive', 0)} | "
            f"TN={abstention.get('true_negative', 0)} |",
            "",
            f"- 拒答召回：{_fmt_rate(abstention.get('refusal_recall'))}",
            f"- 误拒率：{_fmt_rate(abstention.get('false_refusal_rate'))}",
            f"- partial 率：{_fmt_rate(abstention.get('partial_rate'))}",
            f"- 原因码正确率：{_fmt_rate(abstention.get('reason_code_accuracy'))}",
            "",
            "## issue 分布",
            "",
            "| code | scope | severity | count |",
            "|---|---|---|---:|",
        ]
    )
    for item in report.get("issue_distribution", []):
        lines.append(f"| {item['code']} | {item['scope']} | {item['severity']} | {item['count']} |")
    lines.extend(
        [
            "",
            "## 错误归因",
            "",
            "| 主因 | 题数 |",
            "|---|---:|",
        ]
    )
    counts = report.get("error_attribution", {}).get("counts", {})
    if counts:
        for code, count in sorted(counts.items()):
            lines.append(f"| {code} | {count} |")
    else:
        lines.append("| — | 0 |")
    lines.extend(
        [
            "",
            "## 逐题结果",
            "",
            "| ID | 类别 | 期望 | outcome | decision | partial | 渲染 claim | 丢弃 claim | 主因 | 延迟(ms) |",
            "|---|---|---|---|---|---:|---:|---|---|---:|",
        ]
    )
    for row in report.get("rows", []):
        lines.append(
            f"| {row['id']} | {row.get('category', '')} | "
            f"{row.get('expected_outcome', '')} | {row.get('outcome', '')} | "
            f"{row.get('decision', '')} | {'是' if row.get('partial') else '否'} | "
            f"{len(row.get('rendered_claim_ids') or [])} | "
            f"{', '.join(row.get('dropped_claim_ids') or []) or '—'} | "
            f"{row.get('error_attribution') or '—'} | {row.get('qa_ms', 0)} |"
        )
    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- 开发集 v0 只用于调参与回归，不是冻结测试集。",
            "- support 只统计人工确认的标注；AI 预填不算人工验收。",
            "- 探针耗时单列，不混入问答 p50/p95。",
            "- W3 只做确定性核对；语义蕴含与重写循环在 W6。",
        ]
    )
    return "\n".join(lines) + "\n"


def _citation_payload(citation: Any, evidence_by_key: dict, evidence_ids: set[str]) -> dict:
    key = (citation.article_id, citation.part_no)
    evidence = evidence_by_key.get(key)
    valid = evidence is not None
    fabricated = citation.article_id not in evidence_ids
    wrong_part = citation.article_id in evidence_ids and not valid
    span_ok = False
    if evidence is not None:
        span = locate_quote(evidence.article.text, citation.quote)
        span_ok = (
            span is not None
            and span.start == citation.quote_start
            and span.end == citation.quote_end
        )
    return {
        "article_id": citation.article_id,
        "part_no": citation.part_no,
        "block_type": citation.block_type.value,
        "doc_title": citation.doc_title,
        "article_no": citation.article_no,
        "quote": citation.quote,
        "quote_start": citation.quote_start,
        "quote_end": citation.quote_end,
        "source_url": str(citation.source_url),
        "date_confidence": citation.date_confidence,
        "valid_id": valid,
        "fabricated": fabricated,
        "wrong_part": wrong_part,
        "span_ok": span_ok,
    }


def row_from_result(item: dict[str, Any], result: QAResult) -> dict[str, Any]:
    evidence_by_key = {
        (evidence.article.article_id, evidence.article.part_no): evidence
        for evidence in result.evidence
    }
    evidence_ids = {evidence.article.article_id for evidence in result.evidence}
    citations = [
        _citation_payload(citation, evidence_by_key, evidence_ids) for citation in result.citations
    ]
    valid_citations = sum(1 for citation in citations if citation["valid_id"])
    fabricated = sum(1 for citation in citations if citation["fabricated"])
    wrong_part = sum(1 for citation in citations if citation["wrong_part"])
    quotes_located = sum(1 for citation in citations if citation["span_ok"])
    pass_with_blocking = result.decision is VerificationDecision.PASS and any(
        issue.severity.value == "blocking" for issue in result.issues
    )
    expected_ids = set(item.get("expected_article_ids") or [])
    top8 = [evidence.article.article_id for evidence in result.evidence[:8]]
    recall = len(expected_ids & set(top8)) / len(expected_ids) if expected_ids else None
    structure = {
        "citations_total": len(citations),
        "valid_citations": valid_citations,
        "fabricated_id_count": fabricated,
        "wrong_part_count": wrong_part,
        "quotes_total": len(citations),
        "quotes_located": quotes_located,
        "pass_with_blocking": pass_with_blocking,
    }
    return {
        "id": item["id"],
        "category": item.get("category", ""),
        "question": item.get("question", ""),
        "expected_outcome": item.get("expected_outcome", ""),
        "expected_article_ids": list(item.get("expected_article_ids") or []),
        "block_types": list(item.get("block_types") or ["article"]),
        "outcome": result.outcome.value,
        "decision": result.decision.value if result.decision else None,
        "partial": result.partial,
        "reason_code": result.outcome.value if not result.answered else None,
        "answer_markdown": result.answer_markdown,
        "message": result.message,
        "claims": [claim.model_dump(mode="json") for claim in result.claims],
        "citations": citations,
        "drafts": result.drafts,
        "rendered_claim_ids": list(result.rendered_claim_ids),
        "dropped_claim_ids": list(result.dropped_claim_ids),
        "issues": [issue.model_dump(mode="json", exclude_none=True) for issue in result.issues],
        "scope_issues": [
            issue.model_dump(mode="json", exclude_none=True) for issue in result.scope_issues
        ],
        "evidence": [
            {
                "article_id": evidence.article.article_id,
                "part_no": evidence.article.part_no,
                "score": round(evidence.score, 4),
                "dense_score": evidence.dense_score,
                "sparse_score": evidence.sparse_score,
                "rrf_score": evidence.rrf_score,
                "rerank_score": evidence.rerank_score,
                "rank": evidence.rank,
                "block_type": evidence.article.block_type.value,
            }
            for evidence in result.evidence
        ],
        "retrieval": result.retrieval_scores,
        "queries": result.queries,
        "index_version": result.index_version,
        "retrieval_timing": result.retrieval_timing,
        "retrieval_warnings": result.retrieval_warnings,
        "mcp": result.mcp,
        "probe": result.probe,
        "probe_ms": result.probe_ms,
        "qa_ms": result.qa_ms,
        "duration_ms": result.duration_ms,
        "llm_model": result.llm_model,
        "retrieval_recall_at_8": recall,
        "structure": structure,
        "outcome_correct": result.outcome.value == item.get("expected_outcome"),
    }


def git_info(root: str | Path) -> dict[str, Any]:
    root = Path(root)

    def _run(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError:
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    commit = _run("rev-parse", "HEAD")
    status = _run("status", "--porcelain")
    return {"git_commit": commit or "unknown", "git_dirty": bool(status)}


def vector_manifest_info(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    info: dict[str, Any] = {"path": str(path), "sha256": "", "content_hash": ""}
    if not path.exists():
        return info
    info["sha256"] = sha256_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return info
    if isinstance(payload, dict):
        for key in (
            "content_hash",
            "source_content_hash",
            "points",
            "device",
            "model",
            "embedding_model",
        ):
            if key in payload:
                info[key] = payload[key]
    return info


def hash_configs(paths: list[str | Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        item = Path(path)
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        if item.exists():
            digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
