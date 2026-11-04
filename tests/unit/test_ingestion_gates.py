"""构建门禁：本次解析报告必须逐源通过，旧 parsed 文件不能混入。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from campusguide.ingest.build_index import (
    BuildError,
    _vision_proof_problems,
    build_index,
)
from tests.unit.test_build_index import write_parsed, write_sources


def _write_report(
    path: Path,
    *,
    status: str = "parsed",
    source_sha256: str = "a" * 64,
    ingestion_mode: str = "native_only",
    include: bool = True,
) -> Path:
    results = (
        [
            {
                "doc_id": "status-rules",
                "version_id": "v2021",
                "status": status,
                "error": "vision_first 未通过" if status == "error" else None,
                "source_sha256": source_sha256,
                "ingestion_mode": ingestion_mode,
                "warnings": [],
            }
        ]
        if include
        else []
    )
    path.write_text(
        json.dumps({"schema_version": "1.0", "results": results}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_failed_parse_report_blocks_build_even_when_parsed_file_exists(
    tmp_path: Path,
) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")
    report = _write_report(tmp_path / "parse_report.json", status="error")

    with pytest.raises(BuildError, match="不能进入构建"):
        build_index(
            sources_path=sources,
            parsed_dir=tmp_path / "parsed",
            output_dir=tmp_path / "out",
            parse_report_path=report,
            strict=True,
        )


def test_hash_mismatch_report_blocks_stale_parsed(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")
    report = _write_report(tmp_path / "parse_report.json", source_sha256="c" * 64)

    with pytest.raises(BuildError, match="旧文件混入"):
        build_index(
            sources_path=sources,
            parsed_dir=tmp_path / "parsed",
            output_dir=tmp_path / "out",
            parse_report_path=report,
            strict=True,
        )


def test_report_missing_source_blocks_stale_parsed(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")
    report = _write_report(tmp_path / "parse_report.json", include=False)

    with pytest.raises(BuildError, match="缺少 status-rules@v2021"):
        build_index(
            sources_path=sources,
            parsed_dir=tmp_path / "parsed",
            output_dir=tmp_path / "out",
            parse_report_path=report,
            strict=True,
        )


def test_require_vision_requires_parse_report(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")

    with pytest.raises(BuildError, match="必须提供本次 parse_report"):
        build_index(
            sources_path=sources,
            parsed_dir=tmp_path / "parsed",
            output_dir=tmp_path / "out",
            require_vision=True,
        )


def _vision_summary(**overrides):
    summary = {
        "publishable": True,
        "response_validated": True,
        "ok": True,
        "status": "success",
        "pages_expected": 2,
        "pages_processed": 2,
        "images_sent": 2,
        "cache_hits": 0,
        "page_evidence": {
            "1": {"image_sha256": "a" * 64},
            "2": {"image_sha256": "b" * 64},
        },
        "page_status": {
            "1": {"image_sha256": "a" * 64},
            "2": {"image_sha256": "b" * 64},
        },
    }
    summary.update(overrides)
    return summary


def test_vision_proof_rejects_zero_page_marker() -> None:
    summary = _vision_summary(
        pages_expected=0,
        pages_processed=0,
        images_sent=0,
        cache_hits=0,
        page_evidence={},
        page_status={},
    )

    problems = _vision_proof_problems(summary)

    assert any("pages_expected 必须大于 0" in item for item in problems)
    assert any("缺少逐页视觉证据" in item for item in problems)


def test_vision_proof_rejects_page_status_without_evidence() -> None:
    summary = _vision_summary(page_evidence={})

    problems = _vision_proof_problems(summary)

    assert any("缺少逐页视觉证据" in item for item in problems)


def test_vision_proof_accepts_complete_page_evidence() -> None:
    assert _vision_proof_problems(_vision_summary()) == []


def test_vision_proof_accepts_cache_only_full_coverage() -> None:
    summary = _vision_summary(
        images_sent=0,
        cache_hits=2,
        page_evidence={},
        page_status={},
        page_proofs=[
            {
                "page": 1,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": True,
            },
            {
                "page": 2,
                "image_sha256": "b" * 64,
                "validated": True,
                "cache_hit": True,
            },
        ],
    )

    assert _vision_proof_problems(summary) == []


def test_vision_proof_rejects_duplicate_and_missing_pages() -> None:
    summary = _vision_summary(
        page_evidence={},
        page_status={},
        page_proofs=[
            {
                "page": 1,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": False,
            },
            {
                "page": 1,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": False,
            },
        ],
    )

    problems = _vision_proof_problems(summary)

    assert any("page_proofs 页号重复：1" in item for item in problems)
    assert any("page_proofs 缺少页号：[2]" in item for item in problems)


def test_vision_proof_rejects_page_count_mismatch() -> None:
    problems = _vision_proof_problems(_vision_summary(pages_processed=1))

    assert any("页覆盖不足或页数不一致：1/2" in item for item in problems)


def test_vision_proof_rejects_unresolved_conflicts() -> None:
    problems = _vision_proof_problems(
        _vision_summary(unresolved_conflicts=["第 2 页表格列数冲突"])
    )

    assert any("未解决视觉冲突" in item for item in problems)


def test_vision_proof_rejects_no_request_and_no_cache() -> None:
    summary = _vision_summary(
        images_sent=0,
        cache_hits=0,
        page_proofs=[
            {
                "page": 1,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": False,
            },
            {
                "page": 2,
                "image_sha256": "b" * 64,
                "validated": True,
                "cache_hit": False,
            },
        ],
    )

    problems = _vision_proof_problems(summary)

    assert any("images_sent=0" in item and "cache_hits=0" in item for item in problems)


def test_vision_proof_rejects_insufficient_requests_and_cache() -> None:
    summary = _vision_summary(
        images_sent=1,
        cache_hits=0,
        page_evidence={},
        page_status={},
        page_proofs=[
            {
                "page": 1,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": False,
            },
            {
                "page": 2,
                "image_sha256": "b" * 64,
                "validated": True,
                "cache_hit": False,
            },
        ],
    )

    problems = _vision_proof_problems(summary)

    assert any("合计未覆盖 2 页" in item for item in problems)
    assert any("声明非缓存" in item for item in problems)


def test_vision_proof_rejects_non_hex_image_sha() -> None:
    summary = _vision_summary(
        page_evidence={},
        page_status={},
        page_proofs=[
            {
                "page": 1,
                "image_sha256": "Z" * 64,
                "validated": True,
                "cache_hit": False,
            },
            {
                "page": 2,
                "image_sha256": "b" * 64,
                "validated": True,
                "cache_hit": False,
            },
        ],
    )

    problems = _vision_proof_problems(summary)

    assert any("64 位十六进制" in item for item in problems)


def test_vision_proof_rejects_bool_and_float_counts_and_pages() -> None:
    summary = _vision_summary(
        pages_expected=2.0,
        pages_processed=True,
        images_sent=2.0,
        cache_hits=False,
    )
    problems = _vision_proof_problems(summary)

    assert any("pages_expected 必须大于 0" in item for item in problems)
    assert any("images_sent 缺失或非法" in item for item in problems)
    assert any("cache_hits 缺失或非法" in item for item in problems)

    proofs = _vision_summary(
        page_evidence={},
        page_status={},
        page_proofs=[
            {
                "page": 1.5,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": False,
            },
            {
                "page": True,
                "image_sha256": "b" * 64,
                "validated": True,
                "cache_hit": False,
            },
        ],
    )
    assert any(
        "缺少合法页号" in item for item in _vision_proof_problems(proofs)
    )


def test_vision_proof_cross_checks_cache_flags_with_counts() -> None:
    summary = _vision_summary(
        images_sent=1,
        cache_hits=1,
        page_evidence={},
        page_status={},
        page_proofs=[
            {
                "page": 1,
                "image_sha256": "a" * 64,
                "validated": True,
                "cache_hit": True,
            },
            {
                "page": 2,
                "image_sha256": "b" * 64,
                "validated": True,
                "cache_hit": True,
            },
        ],
    )

    problems = _vision_proof_problems(summary)

    assert any("声明 cache_hit" in item and "cache_hits=1" in item for item in problems)


def test_vision_proof_cross_checks_declared_source_sha() -> None:
    summary = _vision_summary(source_sha256="a" * 64)

    mismatched = _vision_proof_problems(summary, parsed_source_sha256="b" * 64)
    assert any("source_sha256 与本次解析源不一致" in item for item in mismatched)
    assert _vision_proof_problems(summary, parsed_source_sha256="A" * 64) == []
