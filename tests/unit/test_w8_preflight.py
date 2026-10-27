"""W8 demo preflight：缺文件、points/hash 不一致必须失败。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.demo_preflight import run_preflight


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(root: Path, *, points: int = 3, vector_hash: str = "abc") -> None:
    _write(
        root / ".env",
        "\n".join(
            [
                "LLM_BASE_URL=https://example.invalid/v1",
                "LLM_API_KEY=test-key",
                "PLANNER_MODEL=test-model",
                "ADVISOR_MODEL=test-model",
                "VERIFIER_MODEL=test-model",
                "QDRANT_URL=http://localhost:6333",
            ]
        )
        + "\n",
    )
    _write(root / "data/processed/articles.jsonl", '{"article_id":"a1"}\n')
    _write(root / "data/processed/documents.jsonl", '{"doc_id":"d1"}\n')
    _write(root / "data/sparse_vocab.json", "{}\n")
    _write(root / "data/manifest.json", json.dumps({"content_hash": "abc"}))
    _write(
        root / "data/vector_manifest.json",
        json.dumps(
            {
                "points": points,
                "source_content_hash": vector_hash,
                "index_version": "idx-test",
            }
        ),
    )


def test_preflight_ok(tmp_path: Path) -> None:
    _fixture(tmp_path)
    result = run_preflight(tmp_path, expected_points=3)
    assert result.ok, result.issues
    assert any("idx-test" in note for note in result.notes)


def test_preflight_reports_missing_env_and_files(tmp_path: Path) -> None:
    result = run_preflight(tmp_path, expected_points=3)
    assert not result.ok
    assert any(".env" in issue for issue in result.issues)
    assert any("articles.jsonl" in issue for issue in result.issues)


def test_preflight_rejects_points_and_hash_mismatch(tmp_path: Path) -> None:
    _fixture(tmp_path, points=2, vector_hash="other")
    result = run_preflight(tmp_path, expected_points=3)
    assert not result.ok
    assert any("points=2" in issue for issue in result.issues)
    assert any("content_hash" in issue for issue in result.issues)
