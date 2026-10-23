"""W3 评测指标纯函数测试：支持率、混淆矩阵、错误归因与门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from campusguide.eval.w3 import build_report, label_index, load_support_labels, render_markdown


def _claim(text: str, *, rendered: bool, label: str = "unlabeled") -> dict:
    return {"claim_id": "", "text": text, "rendered": rendered, "label": label}


def _row(
    row_id: str,
    *,
    expected: str,
    outcome: str,
    claims: list[dict] | None = None,
    issues: list[dict] | None = None,
    recall: float | None = 1.0,
    citations_total: int = 1,
    valid: int = 1,
    fabricated: int = 0,
    quotes_located: int = 1,
    duration_ms: int = 100,
    probe_ms: int = 0,
) -> dict:
    return {
        "id": row_id,
        "category": "article",
        "question": row_id,
        "expected_outcome": expected,
        "outcome": outcome,
        "partial": outcome == "PARTIAL_ANSWER",
        "recall_at_8": recall,
        "claims": claims or [],
        "citations": [],
        "issues": issues or [],
        "citations_total": citations_total,
        "valid_citations": valid,
        "fabricated_id_count": fabricated,
        "quotes_total": citations_total,
        "quotes_located": quotes_located,
        "duration_ms": duration_ms,
        "probe_ms": probe_ms,
    }


def test_label_index_distinguishes_duplicate_text_by_occurrence() -> None:
    labels = [
        {"question_id": "d001", "claim_text": "同一句", "label": "supported"},
        {"question_id": "d001", "claim_text": "同一句", "label": "unsupported"},
    ]
    index = label_index(labels)
    assert index[("d001", "同一句", 0)] == "supported"
    assert index[("d001", "同一句", 1)] == "unsupported"


def test_label_index_normalizes_text() -> None:
    # 标注文件字段为 claim_text_normalized；兼容旧字段 claim_text
    labels = [
        {"question_id": "d001", "claim_text_normalized": "成绩 优异", "label": "supported"},
        {"question_id": "d001", "claim_text": "品行 端正", "label": "partial"},
    ]
    index = label_index(labels)
    assert index[("d001", "成绩优异", 0)] == "supported"
    assert index[("d001", "品行端正", 0)] == "partial"


def test_label_index_uses_explicit_occurrence_field() -> None:
    # 文件顺序与 occurrence 不一致时，以显式字段为准
    labels = [
        {
            "question_id": "d001",
            "claim_text_normalized": "同一句",
            "occurrence": 1,
            "label": "unsupported",
        },
        {
            "question_id": "d001",
            "claim_text_normalized": "同一句",
            "occurrence": 0,
            "label": "supported",
        },
    ]
    index = label_index(labels)
    assert index[("d001", "同一句", 0)] == "supported"
    assert index[("d001", "同一句", 1)] == "unsupported"


def test_build_report_metrics_and_gates() -> None:
    rows = [
        _row(
            "d001",
            expected="ANSWERED",
            outcome="ANSWERED",
            claims=[
                _claim("成绩优异", rendered=True, label="supported"),
                _claim("奖励8000元", rendered=False, label="unsupported"),
            ],
        ),
        _row("d002", expected="ANSWERED", outcome="PARTIAL_ANSWER"),
        _row("d003", expected="NO_EVIDENCE", outcome="NO_EVIDENCE"),
        _row(
            "d004",
            expected="ANSWERED",
            outcome="UNVERIFIABLE",
            recall=1.0,
            issues=[{"code": "NUMBER_NOT_SUPPORTED"}],
        ),
        _row("d005", expected="NO_EVIDENCE", outcome="ANSWERED"),
    ]
    report = build_report(rows, labels=[], metadata={"git_commit": "test"})
    summary = report["summary"]

    assert summary["questions"] == 5
    assert summary["abstention_recall"] == pytest.approx(1 / 2)
    assert summary["over_refusal_rate"] == pytest.approx(1 / 3)
    assert summary["partial_rate"] == pytest.approx(1 / 3)
    assert summary["reason_code_correct_rate"] == pytest.approx(1 / 2)
    assert summary["citation_id_valid_rate"] == 1.0
    assert summary["fabricated_id_count"] == 0
    assert summary["quote_locatable_rate"] == 1.0
    assert summary["issue_distribution"] == {"NUMBER_NOT_SUPPORTED": 1}
    assert report["struct_gate_ok"] is True

    attribution = report["error_attribution"]
    assert attribution["NUMBER_NOT_SUPPORTED"] == 1  # d004
    assert attribution["LABEL_ERROR"] == 1  # d005
    assert attribution.get("OVER_REFUSAL", 0) == 0

    confusion = {
        (cell["expected"], cell["outcome"]): cell["count"] for cell in report["confusion_matrix"]
    }
    assert confusion[("NO_EVIDENCE", "ANSWERED")] == 1
    assert confusion[("ANSWERED", "PARTIAL_ANSWER")] == 1

    # 无标注：支持率为 None，门禁不通过
    assert summary["rendered_support"]["support_rate"] is None
    assert summary["rendered_support_gate_ok"] is False
    assert report["w3_gate_ok"] is False


def test_build_report_support_gate_uses_rendered_only() -> None:
    rows = [
        _row(
            "d001",
            expected="ANSWERED",
            outcome="ANSWERED",
            claims=[
                _claim("渲染句A", rendered=True, label="supported"),
                _claim("渲染句B", rendered=True, label="supported"),
                _claim("被丢弃句", rendered=False, label="unsupported"),
            ],
        )
    ]
    labels = [
        {"question_id": "d001", "claim_text": "渲染句A", "label": "supported"},
        {"question_id": "d001", "claim_text": "渲染句B", "label": "supported"},
        {"question_id": "d001", "claim_text": "被丢弃句", "label": "unsupported"},
    ]
    report = build_report(rows, labels=labels, metadata={})
    summary = report["summary"]
    assert summary["rendered_support"]["support_rate"] == 1.0
    assert summary["draft_support"]["support_rate"] == pytest.approx(2 / 3)
    assert summary["rendered_support_gate_ok"] is True


def test_build_report_unlabeled_not_counted_as_supported() -> None:
    rows = [
        _row(
            "d001",
            expected="ANSWERED",
            outcome="ANSWERED",
            claims=[
                _claim("已标注句", rendered=True, label="supported"),
                _claim("未标注句", rendered=True),
            ],
        )
    ]
    labels = [{"question_id": "d001", "claim_text": "已标注句", "label": "supported"}]
    report = build_report(rows, labels=labels, metadata={})
    rendered = report["summary"]["rendered_support"]
    assert rendered["unlabeled"] == 1
    assert rendered["support_rate"] == 1.0  # 只按已标注的分母计算


def test_latency_excludes_probe_time() -> None:
    rows = [
        _row("d001", expected="NO_EVIDENCE", outcome="NO_EVIDENCE", duration_ms=1000, probe_ms=900),
        _row("d002", expected="ANSWERED", outcome="ANSWERED", duration_ms=300),
        _row("d003", expected="ANSWERED", outcome="ANSWERED", duration_ms=500),
    ]
    report = build_report(rows, labels=[], metadata={})
    # 探针题的有效延迟 = 1000-900 = 100ms；排序后 [100, 300, 500]，p50 = 300
    assert report["summary"]["p50_latency_ms"] == 300
    assert report["summary"]["probe"] == {"questions": 1, "total_ms": 900, "max_ms": 900}


def test_unknown_label_value_counts_as_unlabeled() -> None:
    rows = [
        _row(
            "d001",
            expected="ANSWERED",
            outcome="ANSWERED",
            claims=[
                _claim("已标注句", rendered=True, label="supported"),
                _claim("非法标注", rendered=True, label="maybe"),
            ],
        )
    ]
    labels = [
        {"question_id": "d001", "claim_text": "已标注句", "label": "supported"},
        {"question_id": "d001", "claim_text": "非法标注", "label": "maybe"},
    ]
    report = build_report(rows, labels=labels, metadata={})
    rendered = report["summary"]["rendered_support"]
    assert rendered["unlabeled"] == 1
    assert rendered["support_rate"] == 1.0


def test_struct_gate_fails_on_fabricated_id() -> None:
    rows = [_row("d001", expected="ANSWERED", outcome="ANSWERED", fabricated=1, valid=0)]
    report = build_report(rows, labels=[], metadata={})
    assert report["struct_gate_ok"] is False


def test_load_support_labels_missing_file(tmp_path: Path) -> None:
    assert load_support_labels(tmp_path / "missing.jsonl") == []


def test_load_support_labels_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text(
        json.dumps(
            {"question_id": "d001", "claim_text": "成绩优异", "label": "supported"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    labels = load_support_labels(path)
    assert labels[0]["label"] == "supported"


def test_render_markdown_contains_key_sections() -> None:
    rows = [_row("d001", expected="ANSWERED", outcome="ANSWERED")]
    report = build_report(rows, labels=[], metadata={"git_commit": "abc1234"})
    markdown = render_markdown(report)
    assert "## 拒答混淆矩阵" in markdown
    assert "## 错误归因" in markdown
    assert "rendered 支持率" in markdown
    assert "abc1234" in markdown
