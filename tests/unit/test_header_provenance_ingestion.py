"""Header provenance handoff from ParsedStructure tables to retrieval Articles.

The structures are synthetic. These tests prove metadata transport and
traceability only; they do not claim real-document continuation accuracy.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from campusguide.domain.enums import ArticleStatus, Audience
from campusguide.domain.models import Article, ArticlePublic, DocumentVersion
from campusguide.ingest.build_index import _collect_quality_findings, build_index
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.segment import segment_document
from campusguide.ingest.structure import (
    ParsedStructure,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)

DOC_ID = "table-doc"
VERSION_ID = "v1"
TABLE_ONE = f"{DOC_ID}@{VERSION_ID}#table1"
TABLE_TWO = f"{DOC_ID}@{VERSION_ID}#table2"
SOURCE_URL = "https://example.edu/table"


def make_version() -> DocumentVersion:
    return DocumentVersion(
        doc_id=DOC_ID,
        version_id=VERSION_ID,
        title="课程表",
        doc_type="regulation",
        issuing_department="教务处",
        source_url=SOURCE_URL,
        published_at=date(2026, 10, 1),
        effective_from=date(2026, 9, 1),
        status=ArticleStatus.ACTIVE,
        source_sha256="a" * 64,
        fetched_at=datetime(2026, 10, 19, tzinfo=UTC),
        parser_version="test",
        audience=[Audience.UNDERGRADUATE],
    )


def _cell(table_id: str, row: int, col: int, text: str) -> TableCell:
    return TableCell(
        cell_id=f"{table_id}#r{row}c{col}",
        row=row,
        col=col,
        origin_row=row,
        origin_col=col,
        text=text,
    )


def _row(
    table_id: str,
    row_index: int,
    texts: list[str],
    *,
    page: int,
    is_header: bool = False,
) -> TableRow:
    return TableRow(
        row_id=f"{table_id}#row{row_index}",
        row_index=row_index,
        is_header=is_header,
        cells=[_cell(table_id, row_index, col, text) for col, text in enumerate(texts)],
        text=" | ".join(texts),
        locator=SourceLocator(format="pdf", page=page),
    )


def _table(
    table_id: str,
    *,
    table_index: int,
    page: int,
    rows: list[TableRow],
    header_row_indices: list[int],
    continuation_of: str | None = None,
    header_source_table_id: str | None = None,
    header_source_page: int | None = None,
) -> TableStructure:
    headers = ["课程", "学分"]
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=table_index,
        page=page,
        rows=rows,
        header_row_indices=header_row_indices,
        column_count=2,
        row_count=len(rows),
        column_headers=headers,
        header_paths=[[header] for header in headers],
        kind="data",
        source="pymupdf",
        locator=SourceLocator(format="pdf", page=page),
        continuation_of=continuation_of,
        header_source_table_id=header_source_table_id,
        header_source_page=header_source_page,
    )


def _header_table(
    table_id: str = TABLE_ONE,
    *,
    table_index: int = 1,
    page: int = 1,
) -> TableStructure:
    return _table(
        table_id,
        table_index=table_index,
        page=page,
        rows=[
            _row(table_id, 0, ["课程", "学分"], page=page, is_header=True),
            _row(table_id, 1, ["高等数学", "4"], page=page),
        ],
        header_row_indices=[0],
    )


def _continuation_table(
    *,
    table_id: str = TABLE_TWO,
    table_index: int = 2,
    page: int = 2,
    continuation_of: str | None = TABLE_ONE,
    header_source_table_id: str | None = TABLE_ONE,
    header_source_page: int | None = 1,
) -> TableStructure:
    return _table(
        table_id,
        table_index=table_index,
        page=page,
        rows=[_row(table_id, 0, ["线性代数", "3"], page=page)],
        header_row_indices=[],
        continuation_of=continuation_of,
        header_source_table_id=header_source_table_id,
        header_source_page=header_source_page,
    )


def _parsed(tables: list[TableStructure]) -> ParsedDocument:
    text = "第一条 课程安排见下表。\n"
    return ParsedDocument(
        doc_id=DOC_ID,
        version_id=VERSION_ID,
        title="课程表",
        text=text,
        format="pdf",
        source_sha256="a" * 64,
        fetched_at="2026-10-19T08:00:00+00:00",
        parser_version="test",
        cjk_count=len(text),
        char_count=len(text),
        structure=ParsedStructure(format="pdf", tables=tables),
    )


def _table_articles(result, parent_id: str):
    parent = next(
        article
        for article in result.articles
        if article.article_id == parent_id and article.block_kind == "table_parent"
    )
    rows = [article for article in result.articles if article.parent_article_id == parent_id]
    return parent, rows


def test_same_document_continuation_reaches_parent_and_row_articles() -> None:
    result = segment_document(_parsed([_header_table(), _continuation_table()]), make_version())

    old_parent, old_rows = _table_articles(result, TABLE_ONE)
    assert old_parent.table_continuation_of is None
    assert old_parent.table_header_source_id is None
    assert old_parent.table_header_source_page is None
    assert old_rows
    assert all(row.table_header_source_id is None for row in old_rows)

    parent, rows = _table_articles(result, TABLE_TWO)
    assert parent.page == 2
    assert parent.table_continuation_of == TABLE_ONE
    assert parent.table_header_source_id == TABLE_ONE
    assert parent.table_header_source_page == 1
    assert rows
    for row in rows:
        assert row.page == 2
        assert row.table_continuation_of == TABLE_ONE
        assert row.table_header_source_id == TABLE_ONE
        assert row.table_header_source_page == 1

    public = ArticlePublic.from_article(rows[0])
    assert public.table_header_source_id == TABLE_ONE
    assert public.table_header_source_page == 1
    assert not any("续表头来源不可追溯" in warning for warning in result.warnings)


def test_foreign_document_reference_is_dropped_and_reported() -> None:
    foreign_id = "other-doc@v9#table1"
    foreign = _header_table(foreign_id).model_copy(update={"block_id": f"{foreign_id}#block"})
    continuation = _continuation_table(
        continuation_of=foreign_id,
        header_source_table_id=foreign_id,
    )
    parsed = _parsed([foreign, continuation])
    result = segment_document(parsed, make_version())

    parent, rows = _table_articles(result, TABLE_TWO)
    for article in [parent, *rows]:
        assert article.table_continuation_of is None
        assert article.table_header_source_id is None
        assert article.table_header_source_page is None
    assert any(
        "续表头来源不可追溯" in warning and "已丢弃" in warning
        for warning in result.warnings
    )

    findings = _collect_quality_findings(
        parsed,
        result,
        label=f"{DOC_ID}@{VERSION_ID}",
        require_vision=False,
    )
    assert any("续表头来源不可追溯" in item for item in findings["review"])


def test_self_and_empty_references_are_not_inherited() -> None:
    table_three = _continuation_table(
        table_id=f"{DOC_ID}@{VERSION_ID}#table3",
        table_index=3,
        page=3,
        continuation_of="",
        header_source_table_id="",
        header_source_page=None,
    )
    parsed = _parsed(
        [
            _header_table(),
            _continuation_table(
                continuation_of=TABLE_TWO,
                header_source_table_id=TABLE_TWO,
                header_source_page=2,
            ),
            table_three,
        ]
    )
    result = segment_document(parsed, make_version())

    self_parent, _ = _table_articles(result, TABLE_TWO)
    empty_parent, _ = _table_articles(result, f"{DOC_ID}@{VERSION_ID}#table3")
    for parent in (self_parent, empty_parent):
        assert parent.table_continuation_of is None
        assert parent.table_header_source_id is None
        assert parent.table_header_source_page is None
    assert any("指向当前表自身" in warning for warning in result.warnings)
    assert any("continuation_of 为空" in warning for warning in result.warnings)


def test_source_page_is_preserved_and_mismatch_is_rejected() -> None:
    parsed = _parsed(
        [
            _header_table(),
            _continuation_table(header_source_page=2),
        ]
    )
    result = segment_document(parsed, make_version())

    parent, _ = _table_articles(result, TABLE_TWO)
    assert parent.page == 2
    assert parent.table_header_source_page is None
    assert any("与来源表实际页 1 不一致" in warning for warning in result.warnings)


def test_header_source_page_must_be_positive_on_article() -> None:
    result = segment_document(
        _parsed([_header_table(), _continuation_table()]), make_version()
    )
    article = next(item for item in result.articles if item.article_id == f"{TABLE_TWO}.row0")
    payload = article.model_dump()
    payload["table_header_source_page"] = 0

    with pytest.raises(ValidationError):
        Article.model_validate(payload)


def test_build_index_persists_header_source_metadata(tmp_path: Path) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "corpus_id": "scau",
                "school": "华南农业大学",
                "documents": [
                    {
                        "doc_id": DOC_ID,
                        "title": "课程表",
                        "doc_type": "regulation",
                        "issuing_department": "教务处",
                        "audience": ["undergraduate"],
                        "versions": [
                            {
                                "version_id": VERSION_ID,
                                "url": SOURCE_URL,
                                "published_at": "2026-10-01",
                                "effective_from": "2026-09-01",
                                "status": "active",
                                "format": "pdf",
                            }
                        ],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    parsed_path = tmp_path / "parsed" / DOC_ID / f"{VERSION_ID}.json"
    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    parsed_path.write_text(
        _parsed([_header_table(), _continuation_table()]).model_dump_json(indent=2),
        encoding="utf-8",
    )

    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )

    assert result.articles_path is not None
    articles = [
        json.loads(line)
        for line in result.articles_path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    parent = next(item for item in articles if item["article_id"] == TABLE_TWO)
    assert parent["page"] == 2
    assert parent["table_continuation_of"] == TABLE_ONE
    assert parent["table_header_source_id"] == TABLE_ONE
    assert parent["table_header_source_page"] == 1

    row = next(item for item in articles if item["article_id"] == f"{TABLE_TWO}.row0")
    assert row["page"] == 2
    assert row["table_continuation_of"] == TABLE_ONE
    assert row["table_header_source_id"] == TABLE_ONE
    assert row["table_header_source_page"] == 1
