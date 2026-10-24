"""W7 第二轮：changed labels 沿用与核对表导入。"""

from __future__ import annotations

import pytest

from campusguide.eval.w7 import (
    apply_answer_review,
    apply_support_review,
    carry_answer_labels,
    carry_support_labels,
    changed_answer_records,
    changed_support_records,
    parse_answer_review,
    parse_support_review,
    render_answer_review,
)


def _support_record(claim_id: str, text: str, *, question_id: str = "test-001") -> dict:
    return {
        "run_sha256": "b" * 64,
        "question_id": question_id,
        "claim_id": claim_id,
        "text": text,
        "quotes": [text],
        "located_quotes": [text],
        "kept": True,
        "issues": [],
        "fact_uncovered": [],
        "label": "unlabeled",
        "annotator": "",
        "labeled_at": None,
        "note": "",
    }


def test_carry_support_labels_keeps_unchanged_and_drops_changed() -> None:
    carried = _support_record("c1", "奖学金条件：成绩优异。")
    carried.update(
        {
            "prefill_label": "supported",
            "prefill_note": "ok",
            "prefill_source": "baseline",
            "prefill_from": "test-001/c1",
            "prefill_annotator": "yaukin",
            "prefill_labeled_at": "2026-10-24T10:00:00+00:00",
        }
    )
    changed = _support_record("c2", "新的 claim 文本。")
    labels = carry_support_labels([carried, changed])
    assert labels[0]["label"] == "supported"
    assert labels[0]["carried_over"] is True
    assert labels[0]["annotator"] == "yaukin"
    assert labels[1]["label"] == "unlabeled"
    assert not labels[1].get("carried_over")
    assert [item["claim_id"] for item in changed_support_records(labels)] == ["c2"]


def test_carry_support_labels_keeps_excluded_marker() -> None:
    record = _support_record("c6", "dropped claim。")
    record.update(
        {
            "kept": False,
            "prefill_label": "unlabeled",
            "prefill_excluded": True,
            "prefill_from": "test-032/c6",
        }
    )
    labels = carry_support_labels([record])
    assert labels[0]["excluded_from_support"] is True
    assert labels[0]["carried_over"] is True
    assert changed_support_records(labels) == []


def _answer_row(qid: str, answer: str, claims: list | None = None) -> dict:
    return {
        "qid": qid,
        "question": "问题",
        "category": "single_article",
        "expected_outcome": "ANSWERED",
        "should_abstain": False,
        "gold_answer": "gold",
        "required_facts": [],
        "answer_markdown": answer,
        "claims": claims or [{"claim_id": "c1", "text": answer}],
    }


def _answer_label(qid: str, answer: str, label: str) -> dict:
    return {
        "qid": qid,
        "answer_markdown": answer,
        "claims": [{"claim_id": "c1", "text": answer}],
        "expected_outcome": "ANSWERED",
        "label": label,
        "annotator": "yaukin",
        "labeled_at": "2026-10-24T10:00:00+00:00",
        "note": "",
    }


def test_carry_answer_labels_only_reuses_unchanged_answers() -> None:
    rows = [_answer_row("test-001", "答案 A"), _answer_row("test-002", "答案 B（新）")]
    baseline = [
        _answer_label("test-001", "答案 A", "correct"),
        _answer_label("test-002", "答案 B", "correct"),
    ]
    labels = carry_answer_labels(rows, baseline)
    assert labels[0]["label"] == "correct"
    assert labels[0]["carried_over"] is True
    assert labels[1]["label"] == "unlabeled"
    assert labels[1]["changed"] is True
    assert "changed since previous run" in labels[1]["note"]
    assert [item["qid"] for item in changed_answer_records(labels)] == ["test-002"]


_SUPPORT_REVIEW = """
## 01. test-001 / c1

- 判定：[x] supported  [ ] partial  [ ] unsupported  [ ] excluded（不参与统计）
- note：直接支持

## 02. test-002 / c2

- 判定：[ ] supported  [x] partial  [ ] unsupported  [ ] excluded（不参与统计）
- note：多了限定
"""

_ANSWER_REVIEW = """
## test-001 · single_article

- 判定：[x] correct  [ ] partial  [ ] incorrect  [ ] unanswerable_correct
- note：完整

## test-002 · multi_article

- 判定：[ ] correct  [ ] partial  [x] incorrect  [ ] unanswerable_correct
- note：漏第二问
"""


def test_parse_and_apply_support_review() -> None:
    review = parse_support_review(_SUPPORT_REVIEW)
    assert review[("test-001", "c1")]["label"] == "supported"
    assert review[("test-002", "c2")]["label"] == "partial"
    assert review[("test-002", "c2")]["note"] == "多了限定"

    labels = [
        _support_record("c1", "a", question_id="test-001"),
        _support_record("c2", "b", question_id="test-002"),
    ]
    updated = apply_support_review(labels, review, annotator="yaukin")
    assert updated == 2
    assert labels[0]["label"] == "supported"
    assert labels[1]["label"] == "partial"
    assert labels[0]["annotator"] == "yaukin"


def test_parse_support_review_excluded_option() -> None:
    review = parse_support_review(
        "## 01. test-032 / c6\n\n- 判定：[ ] supported  [ ] partial  "
        "[ ] unsupported  [x] excluded（不参与统计）\n- note：\n"
    )
    labels = [_support_record("c6", "dropped", question_id="test-032")]
    apply_support_review(labels, review, annotator="yaukin")
    assert labels[0]["excluded_from_support"] is True


def test_parse_answer_review_and_render() -> None:
    review = parse_answer_review(_ANSWER_REVIEW)
    assert review["test-001"]["label"] == "correct"
    assert review["test-002"]["label"] == "incorrect"

    labels = [_answer_row("test-002", "答案 B（新）")]
    labels[0].update({"label": "unlabeled", "annotator": "", "note": "changed"})
    markdown = render_answer_review(labels)
    assert "test-002" in markdown
    assert "[ ] correct  [ ] partial  [ ] incorrect  [ ] unanswerable_correct" in markdown

    updated = apply_answer_review(labels, review, annotator="yaukin")
    assert updated == 1
    assert labels[0]["label"] == "incorrect"
    assert labels[0]["annotator"] == "yaukin"


def test_parse_support_review_rejects_multiple_checks() -> None:
    with pytest.raises(ValueError):
        parse_support_review(
            "## 01. test-001 / c1\n\n- 判定：[x] supported  [x] partial  "
            "[ ] unsupported  [ ] excluded（不参与统计）\n"
        )
