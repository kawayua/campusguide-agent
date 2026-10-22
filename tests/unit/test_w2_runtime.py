from __future__ import annotations

import json
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.qa.runtime import IndexUnavailableError, verify_or_raise
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.indexer import build_vector_index, write_vector_manifest
from tests.w2_helpers import make_article


def test_startup_requires_text_and_vector_manifests_to_share_source_hash(
    tmp_path: Path,
) -> None:
    client = QdrantClient(location=":memory:")
    config = RetrievalConfig(
        collection="w2_runtime",
        alias="w2_runtime_current",
        embedding_model="fake",
        dense_vector_size=64,
    )
    manifest = build_vector_index(
        client=client,
        articles=[make_article()],
        embedder=FakeEmbedder(dimension=64),
        retrieval=config,
        recreate=True,
    )
    manifest["source_content_hash"] = "text-hash"
    vector_path = write_vector_manifest(tmp_path / "vector_manifest.json", manifest)

    text_manifest = tmp_path / "manifest.json"
    text_manifest.write_text(json.dumps({"content_hash": "text-hash"}), encoding="utf-8")
    assert (
        verify_or_raise(
            client=client,
            manifest_path=vector_path,
            text_manifest_path=text_manifest,
        )["points"]
        == 1
    )

    text_manifest.write_text(json.dumps({"content_hash": "stale"}), encoding="utf-8")
    with pytest.raises(IndexUnavailableError, match="不同源"):
        verify_or_raise(
            client=client,
            manifest_path=vector_path,
            text_manifest_path=text_manifest,
        )
