"""W7 冻结测试集：校验、冻结 hash、Wilson 区间。

本模块只做确定性数据处理；不调用模型、不运行问答。冻结后的测试集只允许
修正标注错误并记录 changelog，不允许根据模型结果改题。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campusguide.domain.enums import (
    BlockType,
    OutcomeCode,
)
from campusguide.domain.profiles import UserProfile
from campusguide.domain.text import normalize_text

SCHEMA_VERSION = "w7-testset-v1"
TOTAL_QUESTIONS = 50
CATEGORY_COUNTS = {
    "single_article": 25,
    "multi_article": 10,
    "section": 5,
    "out_of_scope": 4,
    "repealed": 3,
    "privacy": 3,
}
ANSWERABLE_CATEGORIES = {"single_article", "multi_article", "section"}
ABSTAIN_CATEGORIES = {"out_of_scope", "repealed", "privacy"}
EXPECTED_OUTCOMES = {item.value for item in OutcomeCode}
VERSION_SENSITIVE_COUNT = 5


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_no} 必须是 JSON 对象")
            rows.append(payload)
    return rows


def canonical_jsonl(rows: list[dict[str, Any]]) -> str:
    ordered = sorted(rows, key=lambda row: str(row.get("qid", "")))
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in ordered
    )


def dataset_hash(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_jsonl(rows).encode("utf-8")).hexdigest()


def _question_key(question: str) -> str:
    return normalize_text(question)


def _parse_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _validate_gold_spans(row: dict[str, Any], articles: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    spans = row.get("gold_quote_spans") or []
    if spans and not row.get("gold_article_ids"):
        errors.append(f"{row['qid']}: gold_quote_spans 需要 gold_article_ids")
        return errors
    if not spans:
        return errors
    article_id = row["gold_article_ids"][0]
    article = articles.get(article_id)
    if article is None:
        return errors
    text = str(article.get("text") or "")
    for span in spans:
        if (
            not isinstance(span, list)
            or len(span) != 2
            or not all(isinstance(value, int) for value in span)
            or span[0] < 0
            or span[1] <= span[0]
            or span[1] > len(text)
        ):
            errors.append(f"{row['qid']}: gold_quote_spans 非法 {span!r}")
    return errors


def validate_testset(
    rows: list[dict[str, Any]],
    *,
    articles: dict[str, dict[str, Any]],
    dev_questions: list[str] | None = None,
    population_questions: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if len(rows) != TOTAL_QUESTIONS:
        errors.append(f"题目数必须是 {TOTAL_QUESTIONS}，实际 {len(rows)}")

    qids = [str(row.get("qid", "")) for row in rows]
    if len(qids) != len(set(qids)):
        errors.append("qid 不能重复")
    questions = [_question_key(str(row.get("question", ""))) for row in rows]
    if len(questions) != len(set(questions)):
        errors.append("题面不能重复")
    known_questions = {_question_key(item) for item in (dev_questions or [])}
    known_questions.update(_question_key(item) for item in (population_questions or []))
    for row in rows:
        if _question_key(str(row.get("question", ""))) in known_questions:
            errors.append(f"{row.get('qid')}: 题面与 dev/population 重复")

    counts = Counter(str(row.get("category", "")) for row in rows)
    for category, expected in CATEGORY_COUNTS.items():
        if counts.get(category, 0) != expected:
            errors.append(f"{category} 数量应为 {expected}，实际 {counts.get(category, 0)}")
    unknown_categories = set(counts) - set(CATEGORY_COUNTS)
    if unknown_categories:
        errors.append(f"未知 category：{sorted(unknown_categories)}")

    version_sensitive = 0
    for row in rows:
        qid = str(row.get("qid", "") or "<missing>")
        question = str(row.get("question", "") or "").strip()
        category = str(row.get("category", "") or "")
        if not question or len(question) > 300:
            errors.append(f"{qid}: question 为空或超过 300 字")
        annotator = row.get("annotator")
        if not isinstance(annotator, str) or not annotator.strip():
            errors.append(f"{qid}: annotator 不能为空")
        elif annotator.strip().lower().startswith("ai"):
            errors.append(f"{qid}: annotator 必须是人工确认，不能以 ai 开头")
        if not _parse_date(row.get("reviewed_at")):
            errors.append(f"{qid}: reviewed_at 必须是 YYYY-MM-DD")
        if not _parse_date(row.get("as_of")):
            errors.append(f"{qid}: as_of 必须是 YYYY-MM-DD")

        block_types = row.get("block_types")
        if not isinstance(block_types, list) or not block_types:
            errors.append(f"{qid}: block_types 不能为空")
            block_types = []
        for block_type in block_types:
            try:
                BlockType(block_type)
            except ValueError:
                errors.append(f"{qid}: 未知 block_type {block_type!r}")

        profile_data = row.get("user_profile") or {}
        profile: UserProfile | None = None
        try:
            profile = UserProfile.model_validate(profile_data)
        except Exception as exc:
            errors.append(f"{qid}: user_profile 不合法：{exc}")

        should_abstain = row.get("should_abstain")
        if not isinstance(should_abstain, bool):
            errors.append(f"{qid}: should_abstain 必须是布尔值")
            should_abstain = category in ABSTAIN_CATEGORIES
        expected_outcome = str(row.get("expected_outcome", "") or "")
        if expected_outcome not in EXPECTED_OUTCOMES:
            errors.append(f"{qid}: 未知 expected_outcome {expected_outcome!r}")

        gold_ids = row.get("gold_article_ids") or []
        if not isinstance(gold_ids, list):
            errors.append(f"{qid}: gold_article_ids 必须是数组")
            gold_ids = []
        if category in ANSWERABLE_CATEGORIES:
            if should_abstain:
                errors.append(f"{qid}: 可答题不应 should_abstain")
            if not gold_ids:
                errors.append(f"{qid}: 可答题缺少 gold_article_ids")
            if category == "single_article" and len(gold_ids) != 1:
                errors.append(f"{qid}: single_article 必须恰好 1 个 gold")
            if category == "multi_article" and len(gold_ids) < 2:
                errors.append(f"{qid}: multi_article 至少 2 个 gold")
            if category == "section" and len(gold_ids) != 1:
                errors.append(f"{qid}: section 必须恰好 1 个 gold")
            if not str(row.get("gold_answer", "") or "").strip():
                errors.append(f"{qid}: 可答题缺少 gold_answer")
        else:
            if not should_abstain:
                errors.append(f"{qid}: 不可答题必须 should_abstain=true")
            if gold_ids:
                errors.append(f"{qid}: 不可答题不应有 gold_article_ids")

        for article_id in gold_ids:
            article = articles.get(str(article_id))
            if article is None:
                errors.append(f"{qid}: gold article 不存在 {article_id}")
                continue
            article_block = str(article.get("block_type", ""))
            if category == "section" and article_block != BlockType.SECTION.value:
                errors.append(f"{qid}: section 题 gold 不是 section")
            if (
                category in {"single_article", "multi_article"}
                and article_block != BlockType.ARTICLE.value
            ):
                errors.append(f"{qid}: article 题 gold 不是 article")
        errors.extend(_validate_gold_spans(row, articles))

        if row.get("version_sensitive"):
            version_sensitive += 1
            if profile is None or profile.intake_year is None:
                errors.append(f"{qid}: version_sensitive 题必须有 intake_year")

    if version_sensitive != VERSION_SENSITIVE_COUNT:
        errors.append(
            f"version_sensitive 题应为 {VERSION_SENSITIVE_COUNT}，实际 {version_sensitive}"
        )
    return errors


def build_manifest(
    rows: list[dict[str, Any]],
    *,
    source: str,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    counts = Counter(str(row.get("category", "")) for row in rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "questions": len(rows),
        "composition": dict(sorted(counts.items())),
        "version_sensitive": sum(1 for row in rows if row.get("version_sensitive")),
        "sha256": dataset_hash(rows),
        "frozen_at": frozen_at or datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_frozen_testset(
    rows: list[dict[str, Any]],
    *,
    output: str | Path,
    manifest_path: str | Path,
    source: str,
) -> dict[str, Any]:
    output = Path(output)
    manifest_path = Path(manifest_path)
    manifest = build_manifest(rows, source=source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    temporary.write_text(canonical_jsonl(rows), encoding="utf-8")
    temporary.replace(output)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_frozen(
    *,
    testset_path: str | Path,
    manifest_path: str | Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    testset_path = Path(testset_path)
    manifest_path = Path(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"缺少冻结 manifest：{manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"冻结 manifest 不是合法 JSON：{manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"冻结 manifest 顶层必须是对象：{manifest_path}")
    rows = load_jsonl(testset_path)
    actual = dataset_hash(rows)
    if actual != manifest.get("sha256"):
        raise ValueError(
            f"测试集已被修改：manifest={manifest.get('sha256')}, actual={actual}；拒绝运行"
        )
    if manifest.get("questions") != len(rows):
        raise ValueError("manifest 题数与实际不一致")
    return rows, manifest


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def _rate(successes: int, total: int) -> dict[str, Any]:
    low, high = wilson_interval(successes, total)
    return {
        "successes": successes,
        "total": total,
        "rate": round(successes / total, 4) if total else None,
        "wilson_low": round(low, 4),
        "wilson_high": round(high, 4),
    }


def retrieval_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from campusguide.eval.w4 import recall_at_k, reciprocal_rank

    groups = {"article": [], "section": []}
    for row in rows:
        if row.get("should_abstain"):
            continue
        expected = list(row.get("expected_article_ids") or [])
        if not expected:
            continue
        retrieved = [item.get("article_id") for item in row.get("evidence") or []]
        recall = recall_at_k(retrieved, expected)
        rr = reciprocal_rank(retrieved, expected)
        if recall is None or rr is None:
            continue
        group = "section" if row.get("category") == "section" else "article"
        groups[group].append((recall, rr))

    result: dict[str, Any] = {}
    all_recalls: list[float] = []
    all_rrs: list[float] = []
    for name, values in groups.items():
        recalls = [item[0] for item in values]
        rrs = [item[1] for item in values]
        all_recalls.extend(recalls)
        all_rrs.extend(rrs)
        result[name] = {
            "questions": len(values),
            "recall_at_8": round(sum(recalls) / len(recalls), 4) if recalls else None,
            "mrr": round(sum(rrs) / len(rrs), 4) if rrs else None,
        }
    result["overall"] = {
        "questions": len(all_recalls),
        "recall_at_8": round(sum(all_recalls) / len(all_recalls), 4) if all_recalls else None,
        "mrr": round(sum(all_rrs) / len(all_rrs), 4) if all_rrs else None,
    }
    return result


def refusal_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if not row.get("should_abstain")]
    unanswerable = [row for row in rows if row.get("should_abstain")]
    rendered = {"ANSWERED", "PARTIAL_ANSWER"}

    false_refusals = sum(1 for row in answerable if row.get("outcome") not in rendered)
    correct_refusals = sum(1 for row in unanswerable if row.get("outcome") not in rendered)
    reason_correct = sum(
        1 for row in unanswerable if row.get("outcome") == row.get("expected_outcome")
    )
    return {
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "false_refusal": _rate(false_refusals, len(answerable)),
        "refusal_recall": _rate(correct_refusals, len(unanswerable)),
        "reason_code_accuracy": _rate(reason_correct, len(unanswerable)),
    }


def support_summary(labels: list[dict[str, Any]]) -> dict[str, Any]:
    labeled = {"supported", "partial", "unsupported"}
    excluded = [item for item in labels if item.get("excluded_from_support")]
    active = [item for item in labels if not item.get("excluded_from_support")]
    drafts = [item for item in active if item.get("label") in labeled]
    rendered = [item for item in drafts if item.get("kept")]
    draft_supported = sum(1 for item in drafts if item.get("label") == "supported")
    rendered_supported = sum(1 for item in rendered if item.get("label") == "supported")
    return {
        "drafts_total": len(labels),
        "drafts_excluded": len(excluded),
        "drafts_labeled": len(drafts),
        "drafts_unlabeled": len(active) - len(drafts),
        "drafts_supported": draft_supported,
        "rendered_total": sum(1 for item in active if item.get("kept")),
        "rendered_labeled": len(rendered),
        "rendered_supported": rendered_supported,
        "draft_support_rate": _rate(draft_supported, len(drafts)),
        "rendered_support_rate": _rate(rendered_supported, len(rendered)),
    }


def answer_summary(labels: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [item for item in labels if not item.get("should_abstain")]
    unanswerable = [item for item in labels if item.get("should_abstain")]
    counts = Counter(str(item.get("label", "unlabeled")) for item in answerable)
    labeled = sum(counts[key] for key in ("correct", "partial", "incorrect"))
    correct = counts.get("correct", 0)
    unanswerable_correct = sum(
        1 for item in unanswerable if item.get("label") == "unanswerable_correct"
    )
    return {
        "total": len(labels),
        "answerable_total": len(answerable),
        "answerable_labeled": labeled,
        "answerable_unlabeled": len(answerable) - labeled,
        "unanswerable_total": len(unanswerable),
        "unanswerable_correct": unanswerable_correct,
        "unlabeled": (len(answerable) - labeled)
        + sum(1 for item in unanswerable if item.get("label") in (None, "", "unlabeled")),
        "counts": dict(sorted(counts.items())),
        "correct_rate": _rate(correct, labeled),
    }


# ---------------------------------------------------------------------------
# 第二轮 remediation：只重标 changed claims / changed answers。
# ---------------------------------------------------------------------------

ANSWER_LABELS = ("correct", "partial", "incorrect", "unanswerable_correct")
SUPPORT_REVIEW_HEADER_RE = re.compile(r"^##\s+\d+\.\s+(\S+)\s*/\s*(\S+)\s*$")
ANSWER_REVIEW_HEADER_RE = re.compile(r"^##\s+(\S+)\s*·")


def _canonical_claims(claims: Any) -> str:
    return json.dumps(claims or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def carry_support_labels(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ``build_label_template(baseline_labels=...)`` 的 prefill 变成 carried 标签。

    匹配键与 W3 相同（问题 + 归一化 claim text + 归一化 quote 集合）。未匹配的
    记录保持 unlabeled，等待人工重标；claim 内容变了就不再复用旧标签。
    """
    carried: list[dict[str, Any]] = []
    for record in records:
        record = dict(record)
        prefilled = False
        if record.get("prefill_label"):
            record["label"] = record["prefill_label"]
            record["annotator"] = str(record.get("prefill_annotator") or "")
            record["labeled_at"] = record.get("prefill_labeled_at")
            record["note"] = str(record.get("prefill_note") or "")
            prefilled = True
        if record.get("prefill_excluded"):
            record["excluded_from_support"] = True
            prefilled = True
        if prefilled:
            record["carried_over"] = True
            record["carried_from"] = record.get("prefill_from")
        carried.append(record)
    return carried


def changed_support_records(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """待人工重标：没有有效标签且没有被排除的 claim。"""
    return [
        item
        for item in labels
        if item.get("label") not in {"supported", "partial", "unsupported"}
        and not item.get("excluded_from_support")
    ]


def carry_answer_labels(
    rows: list[dict[str, Any]],
    baseline_labels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """答案与旧 run 完全一致时复用旧标签；否则标记 changed 并要求重标。"""
    by_qid = {str(item.get("qid")): item for item in baseline_labels}
    labels: list[dict[str, Any]] = []
    for row in rows:
        qid = str(row.get("qid"))
        old = by_qid.get(qid) or {}
        unchanged = (
            str(old.get("answer_markdown") or "") == str(row.get("answer_markdown") or "")
            and _canonical_claims(old.get("claims")) == _canonical_claims(row.get("claims"))
            and str(old.get("expected_outcome") or "") == str(row.get("expected_outcome") or "")
        )
        old_label = str(old.get("label") or "unlabeled")
        old_annotator = str(old.get("annotator") or "").strip()
        old_valid = (
            old_label in ANSWER_LABELS
            and old_annotator
            and not old_annotator.lower().startswith("ai")
        )
        record: dict[str, Any] = {
            "qid": row.get("qid"),
            "question": row.get("question"),
            "category": row.get("category"),
            "expected_outcome": row.get("expected_outcome"),
            "should_abstain": bool(row.get("should_abstain")),
            "gold_answer": row.get("gold_answer", ""),
            "required_facts": row.get("required_facts") or [],
            "answer_markdown": row.get("answer_markdown", ""),
            "claims": row.get("claims") or [],
            "label": "unlabeled",
            "annotator": "",
            "labeled_at": None,
            "note": "",
            "changed": not unchanged,
        }
        if unchanged and old_valid:
            record.update(
                {
                    "label": old_label,
                    "annotator": old_annotator,
                    "labeled_at": old.get("labeled_at"),
                    "note": old.get("note", ""),
                    "carried_over": True,
                }
            )
        elif not unchanged and old_valid:
            old_note = str(old.get("note") or "").strip()
            record["note"] = f"changed since previous run：was {old_label}" + (
                f"（{old_note}）" if old_note else ""
            )
        labels.append(record)
    return labels


def changed_answer_records(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in labels if item.get("label") not in ANSWER_LABELS]


def _effective_label(record: dict[str, Any], labeled: set[str]) -> str:
    label = str(record.get("label") or "unlabeled").strip().lower()
    annotator = str(record.get("annotator") or "").strip()
    if label not in labeled or not annotator or annotator.lower().startswith("ai"):
        return "unlabeled"
    return label


def analyze_support_labels(labels: list[dict[str, Any]]) -> dict[str, int]:
    active = [item for item in labels if not item.get("excluded_from_support")]
    counts = Counter(
        _effective_label(item, {"supported", "partial", "unsupported"}) for item in active
    )
    return {
        "supported": counts.get("supported", 0),
        "partial": counts.get("partial", 0),
        "unsupported": counts.get("unsupported", 0),
        "unlabeled": counts.get("unlabeled", 0),
        "excluded": len(labels) - len(active),
    }


def analyze_answer_labels(labels: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_effective_label(item, set(ANSWER_LABELS)) for item in labels)
    return {
        key: counts.get(key, 0)
        for key in ("correct", "partial", "incorrect", "unanswerable_correct", "unlabeled")
    }


def _checked_options(line: str, options: tuple[str, ...]) -> list[str]:
    return [name for name in options if re.search(rf"\[[xX]\]\s*{re.escape(name)}", line)]


def parse_support_review(text: str) -> dict[tuple[str, str], dict[str, str]]:
    """从 support 核对表 Markdown 解析 ``[x]`` 判定与 note。"""
    result: dict[tuple[str, str], dict[str, str]] = {}
    current: tuple[str, str] | None = None
    options = ("supported", "partial", "unsupported", "excluded")
    for line in text.splitlines():
        header = SUPPORT_REVIEW_HEADER_RE.match(line.strip())
        if header:
            current = (header.group(1), header.group(2))
            result[current] = {"label": "", "note": ""}
            continue
        if current is None:
            continue
        if "判定：" in line:
            hits = _checked_options(line, options)
            if len(hits) > 1:
                raise ValueError(f"{current}: 勾选了多个判定：{hits}")
            if hits:
                result[current]["label"] = hits[0]
        elif line.strip().startswith("- note："):
            result[current]["note"] = line.split("：", 1)[1].strip()
    return {key: value for key, value in result.items() if value["label"]}


def parse_answer_review(text: str) -> dict[str, dict[str, str]]:
    """从答案核对表 Markdown 解析 ``[x]`` 判定与 note。"""
    result: dict[str, dict[str, str]] = {}
    current: str | None = None
    options = ("correct", "partial", "incorrect", "unanswerable_correct")
    for line in text.splitlines():
        header = ANSWER_REVIEW_HEADER_RE.match(line.strip())
        if header:
            current = header.group(1)
            result[current] = {"label": "", "note": ""}
            continue
        if current is None:
            continue
        if "判定：" in line:
            hits = _checked_options(line, options)
            if len(hits) > 1:
                raise ValueError(f"{current}: 勾选了多个判定：{hits}")
            if hits:
                result[current]["label"] = hits[0]
        elif line.strip().startswith("- note："):
            result[current]["note"] = line.split("：", 1)[1].strip()
    return {key: value for key, value in result.items() if value["label"]}


def apply_support_review(
    labels: list[dict[str, Any]],
    review: dict[tuple[str, str], dict[str, str]],
    *,
    annotator: str,
    labeled_at: str | None = None,
) -> int:
    """把核对表判定写回 labels（原地更新），返回更新条数。"""
    stamp = labeled_at or datetime.now(UTC).isoformat(timespec="seconds")
    updated = 0
    for item in labels:
        decision = review.get((item.get("question_id"), item.get("claim_id")))
        if decision is None:
            continue
        if decision["label"] == "excluded":
            item["excluded_from_support"] = True
            item["label"] = "unlabeled"
        else:
            item["label"] = decision["label"]
            item.pop("excluded_from_support", None)
        item["annotator"] = annotator
        item["labeled_at"] = stamp
        item["note"] = decision["note"]
        item["carried_over"] = False
        updated += 1
    return updated


def apply_answer_review(
    labels: list[dict[str, Any]],
    review: dict[str, dict[str, str]],
    *,
    annotator: str,
    labeled_at: str | None = None,
) -> int:
    """把答案核对表判定写回 answer labels（原地更新），返回更新条数。"""
    stamp = labeled_at or datetime.now(UTC).isoformat(timespec="seconds")
    updated = 0
    for item in labels:
        decision = review.get(str(item.get("qid")))
        if decision is None:
            continue
        item["label"] = decision["label"]
        item["annotator"] = annotator
        item["labeled_at"] = stamp
        item["note"] = decision["note"]
        item["carried_over"] = False
        updated += 1
    return updated


def render_answer_review(
    labels: list[dict[str, Any]],
    *,
    title: str = "W7 答案正确性核对表",
) -> str:
    """生成只含 changed/待重标题目的 Markdown 核对表。"""
    lines = [
        f"# {title}",
        "",
        "> 逐题判断 correct / partial / incorrect / unanswerable_correct；"
        "只对照 gold answer / required facts 与渲染答案。",
        "",
        f"- 待重标：{len(labels)} 题（其余题目答案与上一轮一致，已沿用旧标签）。",
        "",
    ]
    for item in labels:
        lines.extend(
            [
                f"## {item.get('qid')} · {item.get('category')}",
                "",
                f"- 问题：{item.get('question')}",
                f"- gold answer：{item.get('gold_answer')}",
                f"- required facts：{'、'.join(item.get('required_facts') or [])}",
                f"- should_abstain：{item.get('should_abstain')} / "
                f"expected：{item.get('expected_outcome')}",
                f"- 变化：{item.get('note') or '—'}",
                "",
                "### 模型答案",
                "",
                str(item.get("answer_markdown") or "（空）"),
                "",
                "- 判定：[ ] correct  [ ] partial  [ ] incorrect  [ ] unanswerable_correct",
                "- note：",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
