"""R9 guard cases: frozen benchmark, statistics and non-destructive re-review.

The report requires one selected session per case, citations counted once, the
statistical median, and re-review that never overwrites the original verdict.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AQ = ROOT / "data/eval/answer_quality"
GOLD = AQ / "gold_v1.jsonl"
MERGED = AQ / "merged_100_20261028/merged_cases.jsonl"
STATS = AQ / "merged_100_20261028/selection_stats.json"
MANIFEST = AQ / "frozen_100_20261028/manifest.json"

VERDICTS = {"complete", "partial", "error", "failed", "valid_clarification", "valid_abstention"}


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def gold() -> list[dict]:
    return _rows(GOLD)


def test_100_cases_with_required_and_optional_checks(gold: list[dict]) -> None:
    assert len(gold) == 100
    for row in gold:
        assert row["required_checks"], row["id"]
        optional = set(row.get("optional_checks") or [])
        assert not optional & set(row["required_checks"]), row["id"]


def test_original_verdict_is_preserved_and_rereview_is_separate(gold: list[dict]) -> None:
    # 原判保留在 merged_cases.jsonl 的 review.status；gold 只加复核字段，不改历史。
    merged = {row["case_number"]: row for row in _rows(MERGED)}
    for order, row in enumerate(gold, start=1):
        assert merged[order]["review"]["status"] in VERDICTS, row["id"]
        assert row["baseline_review"], row["id"]
    changed = {row["id"]: row["review_v2"] for row in gold if row.get("review_v2")}
    assert len(changed) == 4
    assert {value["status"] for value in changed.values()} == {"partial", "complete"}
    for value in changed.values():
        assert value["does_not_overwrite"] == "baseline_review"


def test_selection_statistics_use_one_session_per_case() -> None:
    merged = _rows(MERGED)
    assert len({row["case_number"] for row in merged}) == 100
    elapsed = [row["elapsed_s"] for row in merged if row.get("elapsed_s") is not None]
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    assert stats["cases"] == 100
    assert stats["missing_sessions"] == []
    # 312 条结论级引用按选定 session 逐题统计，不再按轮次累加。
    assert stats["citations"] == 312
    assert stats["elapsed_total_s"] == pytest.approx(sum(elapsed), abs=0.01)
    # 统计中位数：偶数样本取中间两值平均，而不是上中位数。
    assert stats["elapsed_median_s"] == pytest.approx(statistics.median(elapsed), abs=0.01)


def test_manifest_freezes_sessions_and_hashes() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["cases"] == 100
    assert len(manifest["sessions"]) == 100
    assert manifest["business_as_of"] == "2026-10-25"
    for name, expected in manifest["hashes"].items():
        path = GOLD if name == "gold_v1.jsonl" else MERGED if name == "merged_cases.jsonl" else ROOT / "data/processed/articles.jsonl"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name


def test_eval_audit_sends_optional_checks_without_penalty() -> None:
    text = (ROOT / "scripts/audit_answer_quality.py").read_text(encoding="utf-8")
    assert '"optional_checks"' in text
    assert "optional_checks 只用于说明" in text