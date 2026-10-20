from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.ingest.build_index import (
    BuildError,
    build_index,
    init_qdrant_collection,
)
from campusguide.ingest.parse import ParsedDocument

ARTICLE_TEXT = """华南农业大学本科学生学籍管理实施细则
（华南农办〔2021〕68 号）

第一章 总则
第一条 为规范本科学生学籍管理，制定本细则。
第二条 本细则适用于本校全日制本科学生。

第二章 学籍异动
第三条 学生申请转专业，应当提交申请材料，咨询电话 13800138000。
"""


def write_sources(
    path: Path,
    *,
    published_at: str | None = "2021-08-30",
    with_document: bool = True,
) -> Path:
    version: dict[str, object] = {
        "version_id": "v2021",
        "url": "https://jwc.scau.edu.cn/example.htm",
        "effective_from": "2021-09-01",
        "status": "active",
        "format": "html",
    }
    if published_at is not None:
        version["published_at"] = published_at
    documents = (
        [
            {
                "doc_id": "status-rules",
                "title": "华南农业大学本科学生学籍管理实施细则",
                "doc_type": "regulation",
                "issuing_department": "教务处",
                "audience": ["undergraduate"],
                "topic_tags": ["学籍"],
                "versions": [version],
            }
        ]
        if with_document
        else []
    )
    payload = {
        "schema_version": "1.0",
        "corpus_id": "scau",
        "school": "华南农业大学",
        "documents": documents,
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def write_parsed(
    parsed_dir: Path,
    *,
    text: str = ARTICLE_TEXT,
    fetched_at: str | None = "2026-10-19T08:00:00+00:00",
) -> Path:
    parsed = ParsedDocument(
        doc_id="status-rules",
        version_id="v2021",
        title="华南农业大学本科学生学籍管理实施细则",
        text=text,
        format="html",
        source_sha256="a" * 64,
        fetched_at=fetched_at,
        parser_version="test-parser",
        cjk_count=len(text),
        char_count=len(text),
    )
    path = parsed_dir / "status-rules" / "v2021.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_empty_corpus_writes_empty_index_and_manifest(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml", with_document=False)
    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
        generated_at=datetime(2026, 10, 19, tzinfo=UTC),
    )

    assert result.manifest["counts"]["documents"] == 0
    assert result.manifest["counts"]["articles"] == 0
    assert result.articles_path is not None and result.articles_path.read_text("utf-8") == ""
    assert result.documents_path is not None and result.documents_path.read_text("utf-8") == ""
    manifest = json.loads(result.manifest_path.read_text("utf-8"))  # type: ignore[union-attr]
    assert manifest["index_version"] == "w1-articles-v2"
    assert manifest["generated_at"] == "2026-10-19T00:00:00+00:00"


def test_builds_articles_documents_and_redacts_contacts(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")
    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )

    assert result.articles_written == 3
    assert result.versions_written == 1
    assert result.documents_written == 1
    assert result.blocks_skipped == 1  # 前言

    articles = [
        json.loads(line)
        for line in result.articles_path.read_text("utf-8").splitlines()  # type: ignore[union-attr]
    ]
    assert [article["article_no"] for article in articles] == ["第一条", "第二条", "第三条"]
    assert articles[0]["chapter_path"] == ["第一章 总则"]
    assert "13800138000" not in articles[2]["text"]
    assert "[手机号已脱敏]" in articles[2]["text"]
    assert articles[2]["source_sha256"] == "a" * 64

    documents = [
        json.loads(line)
        for line in result.documents_path.read_text("utf-8").splitlines()  # type: ignore[union-attr]
    ]
    assert documents[0]["effective_from"] == "2021-09-01"
    assert documents[0]["published_at"] == "2021-08-30"
    assert documents[0]["doc_type"] == "regulation"


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")

    first = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
        generated_at=datetime(2026, 10, 19, 1, tzinfo=UTC),
    )
    articles_first = first.articles_path.read_bytes()  # type: ignore[union-attr]
    second = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
        generated_at=datetime(2026, 10, 20, 1, tzinfo=UTC),
    )

    assert first.manifest["content_hash"] == second.manifest["content_hash"]
    assert articles_first == second.articles_path.read_bytes()  # type: ignore[union-attr]
    assert first.manifest["generated_at"] != second.manifest["generated_at"]


def test_manifest_path_can_be_overridden(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml", with_document=False)
    manifest_path = tmp_path / "data" / "manifest.json"

    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
        manifest_path=manifest_path,
    )

    assert result.manifest_path == manifest_path
    assert manifest_path.exists()


def test_missing_parsed_result_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )

    assert result.articles_written == 0
    assert result.missing_versions == ["status-rules@v2021"]
    assert any("缺少解析结果" in warning for warning in result.warnings)
    assert result.manifest["counts"]["missing_versions"] == 1


def test_missing_published_at_fails_loudly(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml", published_at=None)
    write_parsed(tmp_path / "parsed")

    with pytest.raises(BuildError, match="published_at"):
        build_index(
            sources_path=sources,
            parsed_dir=tmp_path / "parsed",
            output_dir=tmp_path / "out",
        )


def test_audience_and_jurisdiction_fields_flow_into_index(tmp_path: Path) -> None:
    sources = write_sources(tmp_path / "sources.yaml")
    payload = yaml.safe_load(sources.read_text("utf-8"))
    payload["documents"][0].update(
        {
            "authority_level": "binding",
            "issuer_level": "college",
            "issuer_college_id": "engineering",
            "audience_detail": {
                "colleges": ["engineering"],
                "stages": ["year1"],
            },
        }
    )
    payload["documents"][0]["versions"][0]["audience_detail"] = {
        "colleges": ["engineering"],
        "stages": ["year2"],
    }
    sources.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    write_parsed(tmp_path / "parsed")

    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )

    documents = [
        json.loads(line)
        for line in result.documents_path.read_text("utf-8").splitlines()  # type: ignore[union-attr]
    ]
    assert documents[0]["authority_level"] == "binding"
    assert documents[0]["issuer_level"] == "college"
    assert documents[0]["issuer_college_id"] == "engineering"
    assert documents[0]["audience_detail"]["stages"] == ["year2"]

    articles = [
        json.loads(line)
        for line in result.articles_path.read_text("utf-8").splitlines()  # type: ignore[union-attr]
    ]
    assert articles[0]["doc_type"] == "regulation"
    assert articles[0]["authority_level"] == "binding"
    assert articles[0]["audience_detail"]["colleges"] == ["engineering"]
    assert articles[0]["audience_detail"]["stages"] == ["year2"]


def test_qdrant_collection_skeleton_is_idempotent() -> None:
    client = QdrantClient(":memory:")
    retrieval = RetrievalConfig(
        collection="articles_test",
        alias="articles_test_current",
        embedding_model="fake-embedding",
        dense_vector_size=8,
    )

    first, warnings = init_qdrant_collection(client, retrieval)
    assert first["created"] is True
    assert first["dense_vector_size"] == 8
    assert set(warnings) == set(first["warnings"])

    info = client.get_collection("articles_test")
    vectors = info.config.params.vectors
    assert isinstance(vectors, dict)
    assert vectors["dense"].size == 8
    sparse = info.config.params.sparse_vectors
    assert sparse is not None and "sparse" in sparse

    second, _ = init_qdrant_collection(client, retrieval)
    assert second["created"] is False

    recreated, _ = init_qdrant_collection(client, retrieval, recreate=True)
    assert recreated["created"] is True
    assert recreated["recreated"] is True
