"""W3 评测单元：run hash、support labels、混淆矩阵与报告门禁。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.enums import BlockType, OutcomeCode
from campusguide.domain.models import Citation, Claim
from campusguide.eval.w3 import (
    abstention_metrics,
    apply_support_label,
    build_label_template,
    build_report,
    build_run_snapshot,
    compute_run_hash,
    finalize_run,
    issue_distribution,
    label_counts,
    load_labels,
    merge_labels,
    next_unlabeled_index,
    render_label_sheet,
    render_markdown,
    row_from_result,
    support_metrics,
    verify_run_hash,
    write_labels,
)
from campusguide.qa.service import QAResult
from campusguide.verification.claims import ClaimValidator
from tests.w2_helpers import make_article, make_evidence


def _draft(claim_id: str, *, kept: bool = True) -> dict:
    return {
        "claim_id": claim_id,
        "text": f"结论 {claim_id}。",
        "kind": "other",
        "kept": kept,
        "draft_citations": [{"article_id": "demo", "quote": "学习成绩优异"}],
        "citations": [],
        "issues": [],
        "fact_coverage": {"claim_facts": [], "uncovered": [], "unparsed": [], "quote_unparsed": []},
        "privacy": [],
        "dropped_reason": None if kept else "QUOTE_MISMATCH",
    }


def _row(
    row_id: str = "d001",
    *,
    expected: str = "ANSWERED",
    outcome: str = "ANSWERED",
    drafts: list[dict] | None = None,
    rendered: list[str] | None = None,
    issues: list[dict] | None = None,
    structure: dict | None = None,
    qa_ms: int = 100,
    probe_ms: int = 0,
    probe: dict | None = None,
) -> dict:
    drafts = (
        drafts
        if drafts is not None
        else [_draft("c1", kept=outcome in {"ANSWERED", "PARTIAL_ANSWER"})]
    )
    rendered_ids = (
        rendered
        if rendered is not None
        else (
            [draft["claim_id"] for draft in drafts if draft["kept"]]
            if outcome in {"ANSWERED", "PARTIAL_ANSWER"}
            else []
        )
    )
    structure = structure or {
        "citations_total": 1 if rendered_ids else 0,
        "valid_citations": 1 if rendered_ids else 0,
        "fabricated_id_count": 0,
        "wrong_part_count": 0,
        "quotes_total": 1 if rendered_ids else 0,
        "quotes_located": 1 if rendered_ids else 0,
        "pass_with_blocking": False,
    }
    decision = (
        "pass"
        if outcome == "ANSWERED"
        else ("partial" if outcome == "PARTIAL_ANSWER" else "abstain")
    )
    return {
        "id": row_id,
        "category": "single_article",
        "question": f"问题 {row_id}",
        "expected_outcome": expected,
        "expected_article_ids": [],
        "block_types": ["article"],
        "outcome": outcome,
        "decision": decision,
        "partial": outcome == "PARTIAL_ANSWER",
        "reason_code": None if outcome == "ANSWERED" else outcome,
        "answer_markdown": "",
        "message": "",
        "claims": [],
        "citations": [],
        "drafts": drafts,
        "rendered_claim_ids": rendered_ids,
        "dropped_claim_ids": [draft["claim_id"] for draft in drafts if not draft["kept"]],
        "issues": issues or [],
        "scope_issues": [],
        "evidence": [],
        "retrieval": [],
        "queries": [],
        "probe": probe or {"hit": False, "latency_ms": 0, "article_ids": []},
        "probe_ms": probe_ms,
        "qa_ms": qa_ms,
        "duration_ms": qa_ms + probe_ms,
        "llm_model": "fake",
        "structure": structure,
        "outcome_correct": outcome == expected,
    }


def _label(
    question_id: str,
    claim_id: str,
    label: str,
    *,
    annotator: str = "yaukin",
    run_sha256: str = "run-hash",
) -> dict:
    return {
        "run_sha256": run_sha256,
        "question_id": question_id,
        "claim_id": claim_id,
        "text": f"结论 {claim_id}。",
        "quotes": ["学习成绩优异"],
        "located_quotes": ["学习成绩优异"],
        "kept": True,
        "label": label,
        "annotator": annotator,
        "labeled_at": "2026-10-23T12:00:00Z",
        "note": "",
    }


def _run(rows: list[dict], *, run_sha256: str = "run-hash") -> dict:
    run = {
        "schema_version": "w3-run-v1",
        "dataset": "dev_v0.jsonl",
        "generated_at": "2026-10-23T12:00:00+00:00",
        "metadata": {
            "git_commit": "abc123",
            "git_dirty": False,
            "model": "fake",
            "vector_manifest": {"sha256": "v", "content_hash": "c"},
            "config_hash": "cfg",
            "warmup_ms": 100,
            "latency_scope": "warm",
        },
        "rows": rows,
        "run_sha256": run_sha256,
    }
    return run


def test_run_hash_is_stable_and_detects_tamper() -> None:
    run = {"rows": [{"id": "d001", "outcome": "ANSWERED"}]}
    first = compute_run_hash(run)
    assert compute_run_hash(run) == first

    run["rows"][0]["outcome"] = "UNVERIFIABLE"
    assert compute_run_hash(run) != first


def test_finalize_and_verify_run_hash() -> None:
    run = finalize_run({"rows": [{"id": "d001"}]})
    assert run["run_sha256"]
    verify_run_hash(run)

    run["rows"][0]["id"] = "d002"
    with pytest.raises(ValueError, match="hash"):
        verify_run_hash(run)


def test_build_run_snapshot_sets_summary_and_hash(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text('{"id": "d001"}\n', encoding="utf-8")
    rows = [_row("d001")]
    run = build_run_snapshot(
        dataset="dev.jsonl",
        dataset_path=dataset,
        rows=rows,
        metadata={"model": "fake", "warmup_ms": 100, "latency_scope": "warm"},
    )

    assert run["run_sha256"]
    assert run["summary"]["questions"] == 1
    assert run["summary"]["outcomes"] == {"ANSWERED": 1}
    assert run["dataset_sha256"]
    verify_run_hash(run)


def test_label_template_covers_every_draft_and_roundtrips(tmp_path: Path) -> None:
    run = _run(
        [
            _row("d001", drafts=[_draft("c1"), _draft("c2", kept=False)]),
            _row("d002", expected="NO_EVIDENCE", outcome="NO_EVIDENCE", drafts=[]),
        ]
    )
    template = build_label_template(run)

    assert [(item["question_id"], item["claim_id"]) for item in template] == [
        ("d001", "c1"),
        ("d001", "c2"),
    ]
    assert all(item["run_sha256"] == "run-hash" for item in template)
    assert all(item["label"] == "unlabeled" for item in template)

    path = tmp_path / "labels.jsonl"
    write_labels(template, path)
    assert load_labels(path) == template


def test_merge_labels_rejects_run_mismatch() -> None:
    run = _run([_row("d001")], run_sha256="run-a")
    labels = [_label("d001", "c1", "supported", run_sha256="run-b")]

    with pytest.raises(ValueError, match="run_sha256"):
        merge_labels(run, labels)


def test_support_metrics_excludes_unlabeled_and_requires_human_annotator() -> None:
    run = _run(
        [
            _row(
                "d001",
                drafts=[_draft("c1", kept=True), _draft("c2", kept=True)],
                rendered=["c1", "c2"],
            )
        ]
    )
    labels = [
        _label("d001", "c1", "supported"),
        _label("d001", "c2", "supported", annotator="ai-prefill"),
    ]
    metrics = support_metrics(run, labels, support_gate=0.85)

    assert metrics["drafts_total"] == 2
    assert metrics["drafts_labeled"] == 1
    assert metrics["drafts_unlabeled"] == 1
    assert metrics["draft_support_rate"] == 1.0
    assert metrics["rendered_total"] == 2
    assert metrics["rendered_labeled"] == 1
    assert metrics["rendered_support_rate"] == 1.0
    assert metrics["labels_complete"] is False
    assert metrics["gate_ok"] is False
    assert metrics["unlabeled_claim_ids"] == ["d001/c2"]


def test_support_metrics_counts_partial_separately() -> None:
    run = _run([_row("d001", drafts=[_draft("c1"), _draft("c2")], rendered=["c1", "c2"])])
    labels = [
        _label("d001", "c1", "supported"),
        _label("d001", "c2", "partial"),
    ]
    metrics = support_metrics(run, labels, support_gate=0.85)

    assert metrics["rendered_supported"] == 1
    assert metrics["rendered_partial"] == 1
    assert metrics["rendered_support_rate"] == 0.5
    assert metrics["gate_ok"] is False


def test_support_metrics_passes_with_complete_human_labels() -> None:
    run = _run([_row("d001", drafts=[_draft("c1")], rendered=["c1"])])
    labels = [_label("d001", "c1", "supported")]
    metrics = support_metrics(run, labels, support_gate=0.85)

    assert metrics["labels_complete"] is True
    assert metrics["rendered_support_rate"] == 1.0
    assert metrics["gate_ok"] is True


def test_abstention_metrics_confusion_matrix_and_rates() -> None:
    rows = [
        _row("d001", expected="ANSWERED", outcome="ANSWERED"),
        _row("d002", expected="ANSWERED", outcome="UNVERIFIABLE"),
        _row("d003", expected="NO_EVIDENCE", outcome="NO_EVIDENCE"),
        _row("d004", expected="NO_EVIDENCE", outcome="ANSWERED"),
        _row("d005", expected="ANSWERED", outcome="PARTIAL_ANSWER"),
    ]
    metrics = abstention_metrics(rows)

    assert metrics["answerable"] == 3
    assert metrics["non_answerable"] == 2
    assert metrics["true_positive"] == 2
    assert metrics["false_negative"] == 1
    assert metrics["false_positive"] == 1
    assert metrics["true_negative"] == 1
    assert metrics["refusal_recall"] == 0.5
    assert metrics["false_refusal_rate"] == pytest.approx(1 / 3)
    assert metrics["partial_rate"] == pytest.approx(1 / 3)
    assert metrics["reason_code_accuracy"] == 0.5


def test_issue_distribution_counts_code_scope_severity() -> None:
    issues = [
        {
            "code": "NUMBER_NOT_SUPPORTED",
            "scope": "claim",
            "severity": "blocking",
            "claim_id": "c1",
        },
        {
            "code": "QUOTE_MISMATCH",
            "scope": "citation",
            "severity": "warning",
            "claim_id": "c1",
        },
        {
            "code": "NUMBER_NOT_SUPPORTED",
            "scope": "claim",
            "severity": "blocking",
            "claim_id": "c2",
        },
    ]
    distribution = issue_distribution([_row(issues=issues)])

    assert distribution[0] == {
        "code": "NUMBER_NOT_SUPPORTED",
        "scope": "claim",
        "severity": "blocking",
        "count": 2,
    }
    assert distribution[1]["count"] == 1


def test_error_attribution_primary_cause() -> None:
    labels = []
    report = build_report(
        _run(
            [
                _row("d001", expected="ANSWERED", outcome="NO_EVIDENCE"),
                _row("d002", expected="ANSWERED", outcome="PARTIAL_ANSWER"),
                _row("d003", expected="NO_EVIDENCE", outcome="ANSWERED"),
                _row("d004", expected="NO_EVIDENCE", outcome="HISTORICAL_NOT_SUPPORTED"),
                _row(
                    "d005",
                    expected="ANSWERED",
                    outcome="UNVERIFIABLE",
                    issues=[
                        {
                            "code": "NUMBER_NOT_SUPPORTED",
                            "scope": "claim",
                            "severity": "blocking",
                            "claim_id": "c1",
                        }
                    ],
                ),
                _row(
                    "d006",
                    expected="ANSWERED",
                    outcome="UNVERIFIABLE",
                    issues=[
                        {
                            "code": "QUOTE_MISMATCH",
                            "scope": "citation",
                            "severity": "warning",
                            "claim_id": "c1",
                        }
                    ],
                ),
            ]
        ),
        labels,
    )

    by_id = {row["id"]: row["error_attribution"] for row in report["rows"]}
    assert by_id["d001"] == "RETRIEVAL_MISS"
    assert by_id["d002"] == "PARTIAL_INCOMPLETE"
    assert by_id["d003"] == "OVER_ANSWER"
    assert by_id["d004"] == "REASON_CODE_MISMATCH"
    assert by_id["d005"] == "NUMBER_NOT_SUPPORTED"
    assert by_id["d006"] == "QUOTE_MISMATCH"


def test_build_report_does_not_claim_support_gate_without_labels() -> None:
    report = build_report(_run([_row("d001")]), labels=None)

    assert report["support"]["labels_complete"] is False
    assert report["support"]["rendered_support_rate"] is None
    assert report["gates"]["support"]["ok"] is False
    assert report["gates"]["w3_overall"] is False
    assert "未完成人工 support 标注" in " ".join(report["gates"]["blocking_reasons"])


def test_build_report_passes_all_gates_with_complete_labels() -> None:
    run = _run(
        [
            _row("d001", expected="ANSWERED", outcome="ANSWERED"),
            _row("d002", expected="NO_EVIDENCE", outcome="NO_EVIDENCE", drafts=[]),
        ]
    )
    report = build_report(
        run,
        [
            _label("d001", "c1", "supported"),
        ],
    )

    assert report["support"]["labels_complete"] is True
    assert report["support"]["rendered_support_rate"] == 1.0
    assert report["structure"]["ok"] is True
    assert report["gates"]["refusal_recall"]["ok"] is True
    assert report["gates"]["false_refusal"]["ok"] is True
    assert report["gates"]["w3_overall"] is True


def test_render_markdown_contains_required_sections() -> None:
    report = build_report(_run([_row("d001")]), None)
    markdown = render_markdown(report)

    for text in (
        "W3 开发集报告",
        "run_sha256",
        "引用 ID 有效率",
        "rendered 支持率",
        "拒答召回",
        "误拒率",
        "issue 分布",
        "错误归因",
        "未完成人工 support 标注",
    ):
        assert text in markdown


def test_row_from_result_marks_structure_and_original_spans() -> None:
    article = make_article(article_id="demo@v1#第一条", text="第一条 学习成绩优异。")
    evidence = make_evidence(article)
    draft = ClaimDraft(
        claim_id="c1",
        text="申请奖学金需要学习成绩优异。",
        kind="eligibility",
        citations=[CitationDraft(article_id=article.article_id, quote="学习成绩优异")],
    )
    claim = ClaimValidator().validate([draft], [evidence]).claims[0]
    result = QAResult(
        trace_id="t1",
        outcome=OutcomeCode.ANSWERED,
        answer_markdown="答案",
        claims=[claim],
        citations=claim.citations,
        evidence=[evidence],
        rendered_claim_ids=["c1"],
        decision=None,
        drafts=[],
        llm_model="fake",
        duration_ms=50,
    )
    # decision 仅用于 trace；这里补一个 PASS 以便结构检查。
    from campusguide.domain.enums import VerificationDecision

    result.decision = VerificationDecision.PASS
    item = {
        "id": "d001",
        "category": "single_article",
        "question": "问题",
        "expected_outcome": "ANSWERED",
        "expected_article_ids": [article.article_id],
        "block_types": ["article"],
    }
    row = row_from_result(item, result)

    assert row["structure"]["citations_total"] == 1
    assert row["structure"]["valid_citations"] == 1
    assert row["structure"]["quotes_located"] == 1
    assert row["citations"][0]["valid_id"] is True
    assert row["citations"][0]["span_ok"] is True
    assert row["citations"][0]["quote_start"] == article.text.index("学习成绩优异")
    assert row["drafts"] == []


def test_row_from_result_marks_fabricated_citation() -> None:
    article = make_article(article_id="demo@v1#第一条", text="第一条 学习成绩优异。")
    evidence = make_evidence(article)
    ghost = Citation(
        article_id="ghost@v1#第一条",
        quote="不存在的引文",
        quote_start=0,
        quote_end=6,
        doc_title="幽灵",
        article_no="第一条",
        block_type=BlockType.ARTICLE,
        effective_from=date(2021, 9, 1),
        source_url="https://example.edu.cn/ghost",
    )
    claim = Claim(claim_id="c1", text="结论。", citations=[ghost])
    result = QAResult(
        trace_id="t1",
        outcome=OutcomeCode.ANSWERED,
        claims=[claim],
        citations=[ghost],
        evidence=[evidence],
        rendered_claim_ids=["c1"],
        decision=None,
        duration_ms=1,
    )
    from campusguide.domain.enums import VerificationDecision

    result.decision = VerificationDecision.PASS
    row = row_from_result(
        {
            "id": "d001",
            "category": "single_article",
            "question": "问题",
            "expected_outcome": "ANSWERED",
            "expected_article_ids": [],
            "block_types": ["article"],
        },
        result,
    )

    assert row["structure"]["fabricated_id_count"] == 1
    assert row["structure"]["valid_citations"] == 0
    assert row["citations"][0]["fabricated"] is True


def test_apply_support_label_preserves_fields_and_sets_human_metadata() -> None:
    record = _label("d001", "c1", "unlabeled", annotator="")
    updated = apply_support_label(
        record,
        "supported",
        annotator="yaukin",
        note="quote 直接覆盖",
        labeled_at="2026-10-23T13:00:00+00:00",
    )

    assert updated["label"] == "supported"
    assert updated["annotator"] == "yaukin"
    assert updated["labeled_at"] == "2026-10-23T13:00:00+00:00"
    assert updated["note"] == "quote 直接覆盖"
    assert updated["run_sha256"] == record["run_sha256"]
    assert updated["text"] == record["text"]
    assert record["label"] == "unlabeled"
    assert record["annotator"] == ""


def test_apply_support_label_rejects_invalid_label_or_non_human_annotator() -> None:
    record = _label("d001", "c1", "unlabeled", annotator="")

    with pytest.raises(ValueError, match="label"):
        apply_support_label(record, "unknown", annotator="yaukin")
    with pytest.raises(ValueError, match="annotator"):
        apply_support_label(record, "supported", annotator="")
    with pytest.raises(ValueError, match="annotator"):
        apply_support_label(record, "supported", annotator="ai-prefill")


def test_label_counts_and_next_unlabeled_index() -> None:
    records = [
        _label("d001", "c1", "supported", annotator="yaukin"),
        _label("d001", "c2", "supported", annotator="ai-prefill"),
        _label("d002", "c1", "unsupported", annotator="yaukin"),
        _label("d002", "c2", "unlabeled", annotator=""),
    ]

    assert label_counts(records) == {
        "supported": 1,
        "partial": 0,
        "unsupported": 1,
        "unlabeled": 2,
    }
    assert next_unlabeled_index(records) == 1
    assert next_unlabeled_index(records, include_labeled=True) == 0

    records[1] = apply_support_label(
        records[1],
        "partial",
        annotator="yaukin",
        labeled_at="2026-10-23T13:00:00+00:00",
    )
    assert next_unlabeled_index(records) == 3


def test_render_label_sheet_contains_rubric_and_unlabeled_items() -> None:
    records = [
        _label("d001", "c1", "unlabeled", annotator=""),
        _label("d002", "c1", "supported", annotator="yaukin"),
    ]
    questions = {"d001": "问题一", "d002": "问题二"}
    sheet = render_label_sheet(records, questions)

    assert "W3 support 标注核对表" in sheet
    assert "d001 / c1" in sheet
    assert "问题一" in sheet
    assert "结论 c1。" in sheet
    assert "学习成绩优异" in sheet
    assert "[ ] supported" in sheet
    assert "[ ] partial" in sheet
    assert "[ ] unsupported" in sheet
    assert "当前标签：unlabeled" in sheet
    assert "当前标签：supported（yaukin）" in sheet

    w6_sheet = render_label_sheet(
        records,
        questions,
        title="W6-A support 标注核对表",
    )
    assert "W6-A support 标注核对表" in w6_sheet


def _draft_custom(claim_id: str, text: str, quote: str) -> dict:
    record = _draft(claim_id)
    record["text"] = text
    record["draft_citations"] = [{"article_id": "demo", "quote": quote}]
    record["citations"] = [
        {
            "article_id": "demo",
            "part_no": 1,
            "quote": quote,
            "quote_start": 0,
            "quote_end": len(quote),
        }
    ]
    return record


def test_build_label_template_prefills_exact_human_matches() -> None:
    run = _run(
        [
            _row(
                "d001",
                drafts=[
                    _draft_custom("c1", "结论 c1。", "学习成绩优异"),
                    _draft_custom("c2", "结论 c2 改了。", "学习成绩优异"),
                ],
                rendered=["c1", "c2"],
            )
        ]
    )
    baseline = [
        _label("d001", "c1", "supported", annotator="yaukin"),
        _label("d001", "c2", "partial", annotator="yaukin"),
    ]
    template = build_label_template(run, baseline_labels=baseline)
    by_id = {item["claim_id"]: item for item in template}

    assert by_id["c1"]["prefill_label"] == "supported"
    assert by_id["c1"]["prefill_source"] == "baseline"
    assert by_id["c1"]["label"] == "unlabeled"
    assert by_id["c1"]["annotator"] == ""
    assert "prefill_label" not in by_id["c2"]


def test_build_label_template_does_not_prefill_ai_or_unlabeled() -> None:
    run = _run(
        [
            _row(
                "d001",
                drafts=[_draft_custom("c1", "结论 c1。", "学习成绩优异")],
                rendered=["c1"],
            )
        ]
    )
    ai_baseline = [_label("d001", "c1", "supported", annotator="ai-prefill")]
    assert "prefill_label" not in build_label_template(run, baseline_labels=ai_baseline)[0]

    human_baseline = [_label("d001", "c1", "partial", annotator="yaukin")]
    assert (
        build_label_template(run, baseline_labels=human_baseline)[0]["prefill_label"] == "partial"
    )


def test_render_label_sheet_shows_prefill_suggestion() -> None:
    record = _label("d001", "c1", "unlabeled", annotator="")
    record["prefill_label"] = "supported"
    record["prefill_note"] = "quote 直接覆盖"
    record["prefill_source"] = "baseline"
    sheet = render_label_sheet([record], {"d001": "问题一"})

    assert "prefill 建议：supported" in sheet
    assert "quote 直接覆盖" in sheet
