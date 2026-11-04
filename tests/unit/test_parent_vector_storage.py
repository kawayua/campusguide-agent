from __future__ import annotations

import json

import pytest
from qdrant_client import QdrantClient

from campusguide.config import RetrievalConfig
from campusguide.domain.enums import BlockType
from campusguide.domain.models import Article, CellProvenance
from campusguide.retrieval.embedder import FakeEmbedder
from campusguide.retrieval.hybrid import HybridRetriever
from campusguide.retrieval.indexer import (
    VectorIndexError,
    article_payload,
    build_vector_index,
    embedding_text,
    point_id,
    verify_vector_manifest,
)
from campusguide.retrieval.sparse import BM25SparseEncoder
from tests.w2_helpers import make_article

PARENT_ID = "demo-policy@v2021#table1"
ROW_IDS = (f"{PARENT_ID}.row0", f"{PARENT_ID}.row1")


def _config() -> RetrievalConfig:
    return RetrievalConfig(
        collection="parent_storage_test",
        alias="parent_storage_test_current",
        embedding_model="fake",
        dense_vector_size=64,
    )


def _parent() -> Article:
    article = make_article(
        article_id=PARENT_ID,
        article_no="表格1（完整表）",
        article_index=9001,
        block_type=BlockType.TABLE,
        text="【表格】\n学院 | 名额\n计算机学院 | 10\n软件学院 | 20",
    )
    return article.model_copy(
        update={
            "block_kind": "table_parent",
            "table_id": "tbl-1",
            "parent_table_id": "tbl-1",
            "table_headers": ["学院", "名额"],
            "header_paths": [["学院"], ["名额"]],
            "page": 3,
            "bbox": [10.0, 20.0, 30.0, 40.0],
            "dom_path": "#table-1",
            "table_continuation_of": "tbl-0",
            "table_header_source_id": "tbl-0",
            "table_header_source_page": 2,
        }
    )


def _row(index: int, college: str, quota: str) -> Article:
    article = make_article(
        article_id=f"{PARENT_ID}.row{index}",
        article_no=f"表格1第{index + 1}行",
        article_index=10001 + index,
        block_type=BlockType.SECTION,
        text=f"表头：学院 | 名额\n行：{college} | {quota}",
    )
    cells = [
        CellProvenance(
            cell_id=f"tbl-1-r{index}-c0",
            row=index,
            col=0,
            text=college,
            origin_row=index,
            origin_col=0,
        ),
        CellProvenance(
            cell_id=f"tbl-1-r{index}-c1",
            row=index,
            col=1,
            text=quota,
            origin_row=index,
            origin_col=1,
        ),
    ]
    return article.model_copy(
        update={
            "block_kind": "table_row",
            "parent_article_id": PARENT_ID,
            "parent_table_id": "tbl-1",
            "table_id": "tbl-1",
            "row_id": f"tbl-1-r{index}",
            "row_index": index,
            "table_headers": ["学院", "名额"],
            "header_paths": [["学院"], ["名额"]],
            "cell_provenance": cells,
            "source_cell_ids": [cell.cell_id for cell in cells],
            "page": 3,
            "bbox": [10.0, 50.0, 30.0, 60.0],
            "dom_path": f"#table-1 .row-{index}",
            "table_continuation_of": "tbl-0",
            "table_header_source_id": "tbl-0",
            "table_header_source_page": 2,
        }
    )


def _articles() -> list[Article]:
    return [_parent(), _row(0, "计算机学院", "10"), _row(1, "软件学院", "20")]


def _copy(article: Article, **updates: object) -> Article:
    return Article.model_validate({**article.model_dump(), **updates})


def _build(
    client: QdrantClient,
    articles: list[Article],
) -> tuple[dict, BM25SparseEncoder]:
    config = _config()
    encoder = BM25SparseEncoder.fit(
        [embedding_text(article) for article in articles if article.is_indexable]
    )
    manifest = build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=config,
        sparse_encoder=encoder,
        recreate=True,
    )
    return manifest, encoder


def _assert_rejected_before_upsert(
    client: QdrantClient,
    articles: list[Article],
    *,
    match: str | None = None,
) -> None:
    config = _config()
    with pytest.raises(VectorIndexError, match=match):
        build_vector_index(
            client=client,
            articles=articles,
            embedder=FakeEmbedder(dimension=64),
            retrieval=config,
            recreate=True,
        )
    assert not client.collection_exists(config.collection)


def test_table_parent_payload_only_roundtrip_and_counts() -> None:
    client = QdrantClient(location=":memory:")
    config = _config()
    parent = _parent()
    rows = [_row(0, "计算机学院", "10"), _row(1, "软件学院", "20")]
    manifest, encoder = _build(client, [parent, *rows])

    assert manifest["schema_version"] == "1.2"
    assert manifest["points"] == 3
    assert manifest["indexable_points"] == 2
    assert manifest["stored_parent_points"] == 1
    assert manifest["block_types"] == {"section": 2, "table": 1}
    assert client.count(config.collection).count == 3
    assert verify_vector_manifest(client=client, manifest=manifest) == []

    stored = {
        point.id: point
        for point in client.retrieve(
            collection_name=config.collection,
            ids=[
                point_id(parent.article_id, parent.part_no),
                *[point_id(row.article_id, row.part_no) for row in rows],
            ],
            with_payload=True,
            with_vectors=True,
        )
    }
    assert not (stored[point_id(parent.article_id, parent.part_no)].vector or {})
    for row in rows:
        vector_names = set(
            stored[point_id(row.article_id, row.part_no)].vector or {}
        )
        assert vector_names == {config.dense_vector_name, config.sparse_vector_name}

    dense_hits = client.query_points(
        collection_name=config.collection,
        query=FakeEmbedder(dimension=64).embed_query("学院 名额"),
        using=config.dense_vector_name,
        limit=10,
        with_payload=True,
    ).points
    assert {hit.payload["article_id"] for hit in dense_hits} == set(ROW_IDS)

    sparse_hits = client.query_points(
        collection_name=config.collection,
        query=encoder.encode_query("计算机学院 名额"),
        using=config.sparse_vector_name,
        limit=10,
        with_payload=True,
    ).points
    assert sparse_hits
    assert PARENT_ID not in {hit.payload["article_id"] for hit in sparse_hits}

    parent_payload = article_payload(parent)
    assert parent_payload["is_indexable"] is False
    assert parent_payload["block_kind"] == "table_parent"
    assert parent_payload["table_continuation_of"] == "tbl-0"
    assert parent_payload["table_header_source_id"] == "tbl-0"
    assert parent_payload["table_header_source_page"] == 2
    assert parent_payload["header_paths"] == [["学院"], ["名额"]]

    row_payload = article_payload(rows[0])
    assert row_payload["is_indexable"] is True
    assert [cell["cell_id"] for cell in row_payload["cell_provenance"]] == [
        "tbl-1-r0-c0",
        "tbl-1-r0-c1",
    ]

    retriever = HybridRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        sparse_encoder=encoder,
        reranker=None,
        config=config,
        use_rerank=False,
    )
    loaded_parent = retriever.get_article(PARENT_ID)
    assert loaded_parent is not None
    assert loaded_parent.block_kind == "table_parent"
    assert loaded_parent.table_id == "tbl-1"
    assert loaded_parent.parent_table_id == "tbl-1"
    assert loaded_parent.table_headers == ["学院", "名额"]
    assert loaded_parent.header_paths == [["学院"], ["名额"]]
    assert loaded_parent.table_continuation_of == "tbl-0"
    assert loaded_parent.table_header_source_id == "tbl-0"
    assert loaded_parent.table_header_source_page == 2

    loaded_row = retriever.get_article(ROW_IDS[0])
    assert loaded_row is not None
    assert loaded_row.block_kind == "table_row"
    assert loaded_row.parent_article_id == PARENT_ID
    assert loaded_row.row_index == 0
    assert loaded_row.source_cell_ids == ["tbl-1-r0-c0", "tbl-1-r0-c1"]
    assert [cell.cell_id for cell in loaded_row.cell_provenance] == [
        "tbl-1-r0-c0",
        "tbl-1-r0-c1",
    ]
    assert [cell.col for cell in loaded_row.cell_provenance] == [0, 1]


def test_metadata_source_audit_fields_survive_payload_roundtrip() -> None:
    client = QdrantClient(location=":memory:")
    config = _config()
    audit_sha256 = "c" * 64
    raw_path = "data/raw/demo-policy/source.html"
    articles = [
        _copy(
            _parent(),
            metadata_source_status="review",
            metadata_source_audit_sha256=audit_sha256,
            text_raw_ref=raw_path,
        ),
        _copy(
            _row(0, "计算机学院", "10"),
            metadata_source_status="review",
            metadata_source_audit_sha256=audit_sha256,
            text_raw_ref=raw_path,
        ),
        _copy(
            _row(1, "软件学院", "20"),
            metadata_source_status="review",
            metadata_source_audit_sha256=audit_sha256,
            text_raw_ref=raw_path,
        ),
    ]
    _, encoder = _build(client, articles)

    for article in articles:
        payload = article_payload(article)
        assert payload["metadata_source_status"] == "review"
        assert payload["metadata_source_audit_sha256"] == audit_sha256
        payload_json = json.dumps(payload, ensure_ascii=False)
        assert "text_raw_ref" not in payload
        assert '"metadata_source_audit":' not in payload_json
        assert raw_path not in payload_json

    retriever = HybridRetriever(
        client=client,
        embedder=FakeEmbedder(dimension=64),
        sparse_encoder=encoder,
        reranker=None,
        config=config,
        use_rerank=False,
    )
    for article in articles:
        loaded = retriever.get_article(article.article_id)
        assert loaded is not None
        assert loaded.metadata_source_status == "review"
        assert loaded.metadata_source_audit_sha256 == audit_sha256
        public = retriever.public_article(loaded)
        assert public.metadata_source_status == "review"
        assert public.metadata_source_audit_sha256 == audit_sha256
        public_json = json.dumps(public.model_dump(mode="json"), ensure_ascii=False)
        assert "text_raw_ref" not in public.model_dump()
        assert '"metadata_source_audit":' not in public_json
        assert raw_path not in public_json


def test_indexable_only_fixture_remains_supported() -> None:
    client = QdrantClient(location=":memory:")
    config = _config()
    articles = [
        make_article(),
        make_article(
            article_id="demo-policy@v2021#第二条",
            article_no="第二条",
            article_index=2,
            text="奖学金评审应当公开公平公正。",
        ),
    ]
    manifest = build_vector_index(
        client=client,
        articles=articles,
        embedder=FakeEmbedder(dimension=64),
        retrieval=config,
        recreate=True,
    )

    assert manifest["points"] == 2
    assert manifest["indexable_points"] == 2
    assert manifest["stored_parent_points"] == 0
    assert manifest["block_types"] == {"article": 2}
    assert verify_vector_manifest(client=client, manifest=manifest) == []


def test_orphan_table_row_is_rejected_before_upsert() -> None:
    client = QdrantClient(location=":memory:")
    _assert_rejected_before_upsert(
        client,
        [_row(0, "计算机学院", "10"), _row(1, "软件学院", "20")],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("doc_id", "other-doc"),
        ("table_id", "tbl-2"),
        ("table_header_source_id", "tbl-9"),
        ("table_header_source_page", 4),
    ],
)
def test_conflicting_parent_binding_is_rejected_before_upsert(
    field: str,
    value: str | int,
) -> None:
    client = QdrantClient(location=":memory:")
    conflicting = _row(0, "计算机学院", "10").model_copy(update={field: value})
    _assert_rejected_before_upsert(
        client,
        [_parent(), conflicting, _row(1, "软件学院", "20")],
    )


def test_duplicate_point_key_is_rejected_before_upsert() -> None:
    client = QdrantClient(location=":memory:")
    duplicate = _row(0, "计算机学院", "10")
    _assert_rejected_before_upsert(
        client,
        [_parent(), duplicate, _row(1, "软件学院", "20"), duplicate],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha256", "b" * 64),
        ("source_url", "https://other.example.edu.cn/policy"),
    ],
)
def test_conflicting_source_binding_is_rejected_before_upsert(
    field: str,
    value: str,
) -> None:
    client = QdrantClient(location=":memory:")
    conflicting = _copy(_row(0, "计算机学院", "10"), **{field: value})
    _assert_rejected_before_upsert(
        client,
        [_parent(), conflicting, _row(1, "软件学院", "20")],
        match=field,
    )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"block_type": BlockType.ARTICLE}, "is_indexable"),
        ({"table_id": "", "parent_table_id": ""}, "table_id"),
        ({"parent_table_id": "tbl-2"}, "parent_table_id"),
        ({"parent_table_id": None}, "parent_table_id"),
    ],
)
def test_invalid_unreferenced_parent_is_rejected_before_upsert(
    updates: dict[str, object],
    reason: str,
) -> None:
    client = QdrantClient(location=":memory:")
    orphan = _copy(
        _copy(_parent(), **updates),
        article_id=f"{PARENT_ID}-unreferenced",
    )
    _assert_rejected_before_upsert(
        client,
        [make_article(), _parent(), orphan],
        match=reason,
    )


def test_duplicate_parent_article_id_with_other_part_no_is_rejected() -> None:
    client = QdrantClient(location=":memory:")
    second_parent = _copy(_parent(), part_no=2)
    _assert_rejected_before_upsert(
        client,
        [
            _parent(),
            second_parent,
            _row(0, "计算机学院", "10"),
            _row(1, "软件学院", "20"),
        ],
        match="article_id",
    )
