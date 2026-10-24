from __future__ import annotations

from pathlib import Path

import pytest

from campusguide.eval.w7 import (
    canonical_jsonl,
    dataset_hash,
    validate_testset,
    verify_frozen,
    wilson_interval,
    write_frozen_testset,
)


def _articles() -> tuple[dict[str, dict], list[str], list[str]]:
    articles: dict[str, dict] = {}
    article_ids: list[str] = []
    section_ids: list[str] = []
    for index in range(40):
        article_id = f"doc{index:02d}@v2024#第{index + 1}条"
        articles[article_id] = {
            "article_id": article_id,
            "block_type": "article",
            "text": f"第{index + 1}条规定了演示制度内容。",
        }
        article_ids.append(article_id)
    for index in range(5):
        article_id = f"plan{index}@v2024#培养目标"
        articles[article_id] = {
            "article_id": article_id,
            "block_type": "section",
            "text": "培养具有社会责任感的复合型人才。",
        }
        section_ids.append(article_id)
    return articles, article_ids, section_ids


def _rows() -> list[dict]:
    _, article_ids, section_ids = _articles()
    rows: list[dict] = []
    version_sensitive = 0

    def base(qid: str, category: str, gold: list[str]) -> dict:
        return {
            "qid": qid,
            "split": "test",
            "category": category,
            "question": f"测试问题 {qid}？",
            "user_profile": {},
            "block_types": ["section"] if category == "section" else ["article"],
            "as_of": "2026-10-24",
            "gold_article_ids": gold,
            "gold_quote_spans": [],
            "gold_answer": "演示答案" if gold else "",
            "required_facts": [],
            "should_abstain": not bool(gold),
            "expected_outcome": "ANSWERED" if gold else "NO_EVIDENCE",
            "version_sensitive": False,
            "notes": "",
            "annotator": "tester",
            "reviewed_at": "2026-10-24",
        }

    for index in range(25):
        row = base(f"test-s{index:02d}", "single_article", [article_ids[index]])
        if index < 3:
            row["version_sensitive"] = True
            row["user_profile"] = {"audience": "undergraduate", "intake_year": 2024}
            version_sensitive += 1
        rows.append(row)
    for index in range(10):
        row = base(
            f"test-m{index:02d}",
            "multi_article",
            [article_ids[25 + index], article_ids[30 + index]],
        )
        rows.append(row)
    for index in range(5):
        row = base(f"test-x{index:02d}", "section", [section_ids[index]])
        if index < 2:
            row["version_sensitive"] = True
            row["user_profile"] = {"audience": "undergraduate", "intake_year": 2024}
            version_sensitive += 1
        rows.append(row)
    for index, (category, outcome) in enumerate(
        [
            ("out_of_scope", "OUT_OF_SCOPE"),
            ("out_of_scope", "OUT_OF_SCOPE"),
            ("out_of_scope", "OUT_OF_SCOPE"),
            ("out_of_scope", "OUT_OF_SCOPE"),
            ("repealed", "HISTORICAL_NOT_SUPPORTED"),
            ("repealed", "HISTORICAL_NOT_SUPPORTED"),
            ("repealed", "HISTORICAL_NOT_SUPPORTED"),
            ("privacy", "PRIVACY_REQUEST"),
            ("privacy", "PRIVACY_REQUEST"),
            ("privacy", "PRIVACY_REQUEST"),
        ]
    ):
        row = base(f"test-a{index:02d}", category, [])
        row["expected_outcome"] = outcome
        rows.append(row)
    assert version_sensitive == 5
    return rows


def test_valid_testset_and_composition() -> None:
    articles, _, _ = _articles()
    rows = _rows()
    errors = validate_testset(rows, articles=articles)
    assert errors == []
    assert len(rows) == 50
    assert dataset_hash(rows) == dataset_hash(list(reversed(rows)))


def test_freeze_and_verify_detects_tamper(tmp_path: Path) -> None:
    rows = _rows()
    output = tmp_path / "test_v1.jsonl"
    manifest_path = tmp_path / "test_v1.manifest.json"
    manifest = write_frozen_testset(
        rows,
        output=output,
        manifest_path=manifest_path,
        source="test_v1.draft.jsonl",
    )
    assert manifest["sha256"] == dataset_hash(rows)
    loaded, loaded_manifest = verify_frozen(
        testset_path=output,
        manifest_path=manifest_path,
    )
    assert loaded_manifest["sha256"] == manifest["sha256"]
    assert len(loaded) == 50

    output.write_text(
        canonical_jsonl([{**rows[0], "question": "被篡改的问题？"}, *rows[1:]]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="已被修改"):
        verify_frozen(testset_path=output, manifest_path=manifest_path)


def test_duplicate_question_and_dev_overlap_rejected() -> None:
    articles, _, _ = _articles()
    rows = _rows()
    rows[1]["question"] = rows[0]["question"]
    errors = validate_testset(rows, articles=articles)
    assert any("题面不能重复" in error for error in errors)

    rows = _rows()
    errors = validate_testset(
        rows,
        articles=articles,
        dev_questions=[rows[0]["question"]],
    )
    assert any("dev/population 重复" in error for error in errors)


def test_missing_gold_and_bad_counts_rejected() -> None:
    articles, _, _ = _articles()
    rows = _rows()
    rows[0]["gold_article_ids"] = []
    errors = validate_testset(rows, articles=articles)
    assert any("缺少 gold_article_ids" in error for error in errors)

    rows = _rows()
    rows.pop()
    errors = validate_testset(rows, articles=articles)
    assert any("题目数必须是 50" in error for error in errors)


def test_version_sensitive_requires_intake_year() -> None:
    articles, _, _ = _articles()
    rows = _rows()
    rows[0]["user_profile"] = {}
    errors = validate_testset(rows, articles=articles)
    assert any("必须有 intake_year" in error for error in errors)


def test_wilson_interval() -> None:
    low, high = wilson_interval(8, 10)
    assert 0.45 <= low < 0.8
    assert 0.8 < high <= 0.98
    assert wilson_interval(0, 0) == (0.0, 0.0)
