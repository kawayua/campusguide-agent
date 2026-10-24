from __future__ import annotations

from campusguide.eval.w7_report import build_w7_report, render_w7_markdown


def _run() -> dict:
    rows = [
        {
            "qid": "t1",
            "category": "single_article",
            "should_abstain": False,
            "expected_outcome": "ANSWERED",
            "outcome": "ANSWERED",
            "expected_article_ids": ["a@v1#第一条"],
            "evidence": [{"article_id": "a@v1#第一条"}],
            "qa_ms": 1000,
            "version_sensitive": True,
        },
        {
            "qid": "t2",
            "category": "section",
            "should_abstain": False,
            "expected_outcome": "ANSWERED",
            "outcome": "NO_EVIDENCE",
            "expected_article_ids": ["plan@v1#培养目标"],
            "evidence": [],
            "qa_ms": 3000,
            "version_sensitive": False,
        },
        {
            "qid": "t3",
            "category": "out_of_scope",
            "should_abstain": True,
            "expected_outcome": "OUT_OF_SCOPE",
            "outcome": "OUT_OF_SCOPE",
            "expected_article_ids": [],
            "evidence": [],
            "qa_ms": 100,
            "version_sensitive": False,
        },
        {
            "qid": "t4",
            "category": "privacy",
            "should_abstain": True,
            "expected_outcome": "PRIVACY_REQUEST",
            "outcome": "ANSWERED",
            "expected_article_ids": [],
            "evidence": [],
            "qa_ms": 100,
            "version_sensitive": False,
        },
    ]
    return {
        "dataset": "test_v1",
        "dataset_sha256": "a" * 64,
        "run_sha256": "b" * 64,
        "metadata": {},
        "summary": {"structure": {"ok": True}},
        "rows": rows,
    }


def test_w7_report_summarizes_and_renders() -> None:
    support = [
        {
            "question_id": "t1",
            "claim_id": "c1",
            "kept": True,
            "label": "supported",
            "annotator": "tester",
        },
        {"question_id": "t1", "claim_id": "c2", "kept": False, "label": "partial"},
    ]
    answers = [
        {"qid": "t1", "label": "correct"},
        {"qid": "t2", "label": "incorrect"},
        {"qid": "t3", "label": "unanswerable_correct", "should_abstain": True},
        {"qid": "t4", "label": "incorrect", "should_abstain": True},
    ]
    report = build_w7_report(_run(), support_labels=support, answer_labels=answers)
    assert report["retrieval"]["article"]["recall_at_8"] == 1.0
    assert report["retrieval"]["section"]["recall_at_8"] == 0.0
    assert report["refusal"]["refusal_recall"]["rate"] == 0.5
    assert report["refusal"]["false_refusal"]["rate"] == 0.5
    assert report["support"]["rendered_support_rate"]["rate"] == 1.0
    assert report["answer_correctness"]["correct_rate"]["rate"] == 0.5
    assert report["version_sensitive"]["rate"] == 1.0
    assert report["overall"] is False

    markdown = render_w7_markdown(report)
    assert "CampusGuide W7" in markdown
    assert "article Recall@8" in markdown
    assert "标注：tester" in markdown
    assert "Wilson 95%" in markdown
    assert "| rendered support | 1/1 | 1.000 | 0.206–1.000 |" in markdown
    assert "correct 1 / partial 0 / incorrect 1" in markdown


def test_report_without_labels_does_not_pass() -> None:
    report = build_w7_report(_run(), support_labels=[], answer_labels=[])
    assert report["overall"] is False
    assert report["support"]["drafts_unlabeled"] == 0
    assert report["answer_correctness"]["unlabeled"] == 0


def test_report_shows_pending_review_instead_of_rate() -> None:
    support = [{"question_id": "t1", "claim_id": "c1", "kept": True, "label": "unlabeled"}]
    answers = [{"qid": "t1", "label": "unlabeled"}]
    report = build_w7_report(_run(), support_labels=support, answer_labels=answers)
    assert report["gates"]["support"]["unlabeled"] == 1
    assert report["gates"]["answer_correct"]["unlabeled"] == 1
    markdown = render_w7_markdown(report)
    assert "待标注（已标 0/1）" in markdown
