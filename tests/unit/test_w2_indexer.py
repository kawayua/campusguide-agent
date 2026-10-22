from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.indexer import (
    article_payload,
    build_vector_index,
    embedding_text,
    point_id,
    read_vector_manifest,
    verify_vector_manifest,
    write_vector_manifest,
)
from tests.w2_helpers import make_article


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        collection="w2_test",
        alias="w2_test_current",
        embedding_model="fake",
        dense_vector_size=64,
    )


def test_point_id_is_stable_and_part_specific() -> None:
    first = point_id("demo@v1#第一条", 1)
    second = point_id("demo@v1#第一条", 1)
    other_part = point_id("demo@v1#第一条", 2)

    assert first == second
    assert first != other_part


def test_embedding_text_and_payload_contract() -> None:
    article = make_article(date_confidence="low")
    text = embedding_text(article)
    assert "标题：演示办法" in text
    assert "章节：第一章 总则" in text
    assert "位置：第一条" in text

    payload = article_payload(article)
    assert payload["article_id"] == article.article_id
    assert payload["part_no"] == 1
    assert payload["block_type"] == "article"
    assert payload["date_confidence"] == "low"
    assert payload["effective_from"].endswith("Z")
    assert payload["effective_to"] is None
    assert payload["audience"] == ["undergraduate"]


def test_build_vector_index_and_manifest_roundtrip(tmp_path: Path) -> None:
    client = QdrantClient(location=":memory:")
    articles = [
        make_article(),
        make_article(
            article_id="demo-policy@v2021#第二条",
            article_no="第二条",
            article_index=2,
            text="奖学金评审应当公开公平公正。",
        ),
    ]
    config = _config()
    manifest = build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=config,
        recreate=True,
    )

    assert manifest["points"] == 2
    assert manifest["dimension"] == 64
    assert manifest["block_types"] == {"article": 2}
    assert client.count(config.collection).count == 2
    manifest["source_content_hash"] = "text-hash"

    path = write_vector_manifest(tmp_path / "vector_manifest.json", manifest)
    loaded = read_vector_manifest(path)
    assert verify_vector_manifest(client=client, manifest=loaded) == []
    assert (
        verify_vector_manifest(
            client=client,
            manifest=loaded,
            source_content_hash=loaded["source_content_hash"],
        )
        == []
    )

    source_problems = verify_vector_manifest(
        client=client,
        manifest=loaded,
        source_content_hash="different-text-manifest-hash",
    )
    assert any("不同源" in problem for problem in source_problems)

    loaded["points"] = 3
    problems = verify_vector_manifest(client=client, manifest=loaded)
    assert problems and "点数不一致" in problems[0]
