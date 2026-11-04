"""来源元数据审计接入构建流程的端到端测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from campusguide.domain.models import Article, ArticlePublic
from campusguide.ingest.build_index import BuildError, build_index
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.structure import (
    ParsedStructure,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)

URL = "https://info.scau.edu.cn/2026/1101/c1a1/page.htm"
TITLE = "数学与信息学院2026年国家奖学金评审通知"
PARENT_TITLE = "华南农业大学国家奖学金评审办法"
SOURCE_TEXT = f"""{TITLE}
第一条 学院开展评审工作。
第二条 申请材料提交至学院办公室。
"""


def _write_organization(tmp_path: Path) -> Path:
    path = tmp_path / "organization.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "corpus_id": "scau",
                "colleges": [
                    {"college_id": "info", "name": "数学与信息学院"},
                    {"college_id": "agri", "name": "农学院"},
                ],
                "majors": [
                    {
                        "major_id": "computer-science",
                        "name": "计算机科学与技术",
                        "college_id": "info",
                    },
                    {"major_id": "agronomy", "name": "农学", "college_id": "agri"},
                ],
                "class_types": [
                    {
                        "class_id": "new-agronomy-innovation",
                        "name": "新农学创新班",
                        "level": "undergraduate",
                        "majors": ["agronomy"],
                        "aliases": ["新农学本博班"],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_source(
    tmp_path: Path,
    *,
    text: str = SOURCE_TEXT,
    sidecar: bool = True,
) -> tuple[Path, str]:
    source = tmp_path / "source.html"
    source.write_bytes(text.encode("utf-8"))
    sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    if sidecar:
        payload = {
            "doc_id": "audit-doc",
            "version_id": "v2026",
            "source_url": URL,
            "source_sha256": sha256,
            "fetched_at": "2026-11-01T11:21:46+00:00",
            "http_status": 200,
            "byte_size": source.stat().st_size,
            "local_path": str(source),
        }
        (tmp_path / "acquisition.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    return source, sha256


def _write_sources(
    tmp_path: Path,
    source: Path,
    *,
    document_updates: dict[str, object] | None = None,
    include_parent: bool = False,
) -> Path:
    document: dict[str, object] = {
        "doc_id": "audit-doc",
        "title": TITLE,
        "doc_type": "notice",
        "issuing_department": "数学与信息学院",
        "audience": ["undergraduate"],
        "versions": [
            {
                "version_id": "v2026",
                "url": URL,
                "local_path": str(source),
                "published_at": "2026-10-09",
                "effective_from": "2026-10-09",
                "status": "active",
                "format": "html",
            }
        ],
    }
    if document_updates:
        document.update(document_updates)
    documents = [document]
    if include_parent:
        documents.append(
            {
                "doc_id": "parent-notice",
                "title": PARENT_TITLE,
                "doc_type": "regulation",
                "issuing_department": "学生工作部",
                "audience": ["undergraduate"],
                "versions": [
                    {
                        "version_id": "v2026",
                        "url": "https://example.edu.cn/parent",
                        "published_at": "2026-09-01",
                        "status": "active",
                        "format": "html",
                    }
                ],
            }
        )
    path = tmp_path / "sources.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "corpus_id": "scau",
                "school": "华南农业大学",
                "documents": documents,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_parsed(
    tmp_path: Path,
    source: Path,
    sha256: str,
    *,
    parsed_dir: Path | None = None,
    include_local_path: bool = True,
    text: str = SOURCE_TEXT,
    source_sha256: str | None = None,
    structure: ParsedStructure | None = None,
) -> Path:
    parsed = ParsedDocument(
        doc_id="audit-doc",
        version_id="v2026",
        title=TITLE,
        text=text,
        format="html",
        source_sha256=source_sha256 or sha256,
        source_url=URL,
        local_path=str(source) if include_local_path else None,
        fetched_at="2026-11-01T11:21:46+00:00",
        parser_version="test-metadata-wiring",
        cjk_count=len(text),
        char_count=len(text),
        structure=structure,
    )
    path = (parsed_dir or (tmp_path / "parsed")) / "audit-doc" / "v2026.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    return path


def _write_raw_snapshot(
    tmp_path: Path,
    *,
    source_file: str = "source.html",
    meta_updates: dict[str, object] | None = None,
    write_meta: bool = True,
) -> Path:
    version_dir = tmp_path / "data" / "raw" / "audit-doc" / "v2026"
    version_dir.mkdir(parents=True, exist_ok=True)
    raw_source = version_dir / source_file
    raw_source.write_bytes(SOURCE_TEXT.encode("utf-8"))
    meta: dict[str, object] = {
        "schema_version": "1.0",
        "doc_id": "audit-doc",
        "version_id": "v2026",
        "title": TITLE,
        "format": "html",
        "source_file": source_file,
        "sha256": hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest(),
        "source_url": URL,
        "fetched_at": "2026-11-01T11:21:46+00:00",
        "local_path": None,
    }
    if meta_updates:
        meta.update(meta_updates)
    if write_meta:
        (version_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
    return raw_source


def _table_structure() -> ParsedStructure:
    header_cells = [
        TableCell(
            cell_id="c0",
            row=0,
            col=0,
            origin_row=0,
            origin_col=0,
            text="班级",
            is_header=True,
        ),
        TableCell(
            cell_id="c1",
            row=0,
            col=1,
            origin_row=0,
            origin_col=1,
            text="名额",
            is_header=True,
        ),
    ]
    data_cells = [
        TableCell(
            cell_id="c2",
            row=1,
            col=0,
            origin_row=1,
            origin_col=0,
            text="农学普通班",
        ),
        TableCell(
            cell_id="c3",
            row=1,
            col=1,
            origin_row=1,
            origin_col=1,
            text="10",
        ),
    ]
    table = TableStructure(
        table_id="tbl-1",
        block_id="block-table",
        table_index=1,
        page=1,
        rows=[
            TableRow(
                row_id="r0",
                row_index=0,
                is_header=True,
                cells=header_cells,
                text="班级 | 名额",
                locator=SourceLocator(format="html"),
            ),
            TableRow(
                row_id="r1",
                row_index=1,
                cells=data_cells,
                text="农学普通班 | 10",
                locator=SourceLocator(format="html"),
            ),
        ],
        header_row_indices=[0],
        column_count=2,
        row_count=2,
        column_headers=["班级", "名额"],
        header_paths=[["班级"], ["名额"]],
        locator=SourceLocator(format="html"),
    )
    return ParsedStructure(extractor_version="test", format="html", tables=[table])


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sentinel_outputs(output_dir: Path) -> dict[str, bytes]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "articles.jsonl": b"old-articles\n",
        "documents.jsonl": b"old-documents\n",
        "manifest.json": b'{"old": true}\n',
    }
    for name, content in payloads.items():
        (output_dir / name).write_bytes(content)
    return payloads


def _assert_outputs_unchanged(output_dir: Path, expected: dict[str, bytes]) -> None:
    for name, content in expected.items():
        assert (output_dir / name).read_bytes() == content


def test_audit_status_and_sha_survive_to_public_payload(tmp_path: Path) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(tmp_path, source)
    _write_parsed(tmp_path, source, sha256, structure=_table_structure())

    result = build_index(
        sources_path=sources,
        metadata_organization_path=organization,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )
    manifest = result.manifest
    audit = manifest["sources"][0]["metadata_source_audit"]
    assert audit["status"] == "verified"
    assert audit["source"]["path"] == str(source)
    assert audit["evidence"]
    assert manifest["metadata_source_audit"]["enabled"] is True
    assert manifest["metadata_source_audit"]["audited_versions"] == 1
    assert manifest["metadata_source_audit"]["status_counts"]["verified"] == 1

    canonical = json.dumps(
        audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    expected_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert result.documents_path is not None
    document = _read_jsonl(result.documents_path)[0]
    assert document["metadata_source_status"] == "verified"
    assert document["metadata_source_audit_sha256"] == expected_sha

    assert result.articles_path is not None
    articles = _read_jsonl(result.articles_path)
    kinds = {article["block_kind"] for article in articles}
    assert {"text", "table_parent", "table_row"} <= kinds
    for article in articles:
        assert article["metadata_source_status"] == "verified"
        assert article["metadata_source_audit_sha256"] == expected_sha
        public = ArticlePublic.from_article(Article.model_validate(article))
        assert public.metadata_source_status == "verified"
        assert public.metadata_source_audit_sha256 == expected_sha
        public_dict = public.model_dump(mode="json")
        public_payload = json.dumps(public_dict, ensure_ascii=False)
        # 公共投影只保留审计状态与摘要哈希；完整 evidence、source.path 与
        # 审计对象只留在 manifest。audience_detail.scope_evidence 是合法的
        # 受众范围依据，不能用子串匹配整体禁用。
        assert str(source) not in public_payload
        assert "metadata_source_audit" not in public_dict
        assert "evidence" not in public_dict
        assert "source" not in public_dict
        assert "path" not in public_dict


def test_fake_explicit_class_fails_closed_before_outputs_replaced(
    tmp_path: Path,
) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(
        tmp_path,
        source,
        document_updates={
            "audience_detail": {"class_types": ["new-agronomy-innovation"]}
        },
    )
    _write_parsed(tmp_path, source, sha256)
    output_dir = tmp_path / "out"
    before = _sentinel_outputs(output_dir)

    with pytest.raises(BuildError, match="class_type_source_missing"):
        build_index(
            sources_path=sources,
            metadata_organization_path=organization,
            parsed_dir=tmp_path / "parsed",
            output_dir=output_dir,
            strict=False,
        )

    _assert_outputs_unchanged(output_dir, before)


def test_source_sha_tampering_fails_closed_before_outputs_replaced(
    tmp_path: Path,
) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(tmp_path, source)
    _write_parsed(tmp_path, source, sha256, source_sha256="0" * 64)
    output_dir = tmp_path / "out"
    before = _sentinel_outputs(output_dir)

    with pytest.raises(BuildError, match="source_sha256_mismatch"):
        build_index(
            sources_path=sources,
            metadata_organization_path=organization,
            parsed_dir=tmp_path / "parsed",
            output_dir=output_dir,
            strict=False,
        )

    _assert_outputs_unchanged(output_dir, before)


def test_unverified_relationship_review_stays_visible(tmp_path: Path) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(
        tmp_path,
        source,
        document_updates={
            "relation": "basis",
            "parent_doc_id": "parent-notice",
            "parent_version_id": "v2026",
        },
        include_parent=True,
    )
    _write_parsed(tmp_path, source, sha256)

    result = build_index(
        sources_path=sources,
        metadata_organization_path=organization,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
        strict=False,
    )

    audit = result.manifest["sources"][0]["metadata_source_audit"]
    assert audit["status"] == "review"
    assert "relation_citation_unverified" in {
        item["code"] for item in audit["review"]
    }
    summary = result.manifest["metadata_source_audit"]
    assert summary["status_counts"]["review"] == 1
    assert summary["status_counts"]["verified"] == 0
    assert summary["review_versions"] == ["audit-doc@v2026"]

    assert result.documents_path is not None
    document = _read_jsonl(result.documents_path)[0]
    assert document["metadata_source_status"] == "review"
    assert result.articles_path is not None
    assert {
        article["metadata_source_status"]
        for article in _read_jsonl(result.articles_path)
    } == {"review"}


def test_configured_audience_constraints_do_not_upgrade_unknown_scope(
    tmp_path: Path,
) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(
        tmp_path,
        source,
        document_updates={
            "audience_detail": {"colleges": ["info"], "stages": ["year1"]}
        },
    )
    _write_parsed(tmp_path, source, sha256)

    result = build_index(
        sources_path=sources,
        metadata_organization_path=organization,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )

    assert result.documents_path is not None
    document = _read_jsonl(result.documents_path)[0]
    assert document["audience_detail"]["scope_status"] == "unknown"
    assert document["audience_detail"]["scope_evidence"] is None
    assert document["metadata_source_status"] == "verified"

    assert result.articles_path is not None
    articles = _read_jsonl(result.articles_path)
    assert articles
    assert {
        article["audience_detail"]["scope_status"] for article in articles
    } == {"unknown"}


def test_disabled_audit_records_not_checked(tmp_path: Path) -> None:
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(tmp_path, source)
    _write_parsed(tmp_path, source, sha256)

    result = build_index(
        sources_path=sources,
        parsed_dir=tmp_path / "parsed",
        output_dir=tmp_path / "out",
    )

    summary = result.manifest["metadata_source_audit"]
    assert summary["enabled"] is False
    assert summary["audited_versions"] == 0
    assert summary["versions"] == 1
    assert summary["status_counts"] == {
        "not_checked": 1,
        "verified": 0,
        "review": 0,
        "failed": 0,
    }
    assert result.manifest["sources"][0]["metadata_source_audit"] == {
        "status": "not_checked"
    }

    assert result.documents_path is not None
    document = _read_jsonl(result.documents_path)[0]
    assert document["metadata_source_status"] == "not_checked"
    assert document["metadata_source_audit_sha256"] is None
    assert result.articles_path is not None
    for article in _read_jsonl(result.articles_path):
        assert article["metadata_source_status"] == "not_checked"
        assert article["metadata_source_audit_sha256"] is None


@pytest.mark.parametrize("source_file", ["source.html", "snapshot.html"])
def test_missing_parsed_local_path_uses_standard_raw_snapshot(
    tmp_path: Path,
    source_file: str,
) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(tmp_path, source)
    parsed_dir = tmp_path / "data" / "processed" / "parsed"
    _write_parsed(
        tmp_path,
        source,
        sha256,
        parsed_dir=parsed_dir,
        include_local_path=False,
    )
    raw_source = _write_raw_snapshot(tmp_path, source_file=source_file)

    result = build_index(
        sources_path=sources,
        metadata_organization_path=organization,
        parsed_dir=parsed_dir,
        output_dir=tmp_path / "out",
    )

    audit = result.manifest["sources"][0]["metadata_source_audit"]
    review_codes = {item["code"] for item in audit["review"]}
    assert audit["source"]["path"] == str(raw_source.resolve())
    assert audit["source"]["sha256"] == sha256
    assert audit["status"] == "review"
    assert "raw_meta_acquisition_method_unlabeled" in review_codes
    assert "parsed_local_path_missing" not in review_codes
    assert result.manifest["metadata_source_audit"]["audited_versions"] == 1

    assert result.documents_path is not None
    document = _read_jsonl(result.documents_path)[0]
    assert document["metadata_source_status"] == "review"


@pytest.mark.parametrize(
    ("meta_updates", "match"),
    [
        pytest.param({"sha256": "0" * 64}, "sha256", id="sha256"),
        pytest.param(
            {"source_url": "https://example.edu.cn/forged"},
            "source_url",
            id="source_url",
        ),
        pytest.param(
            {"source_file": "../escaped.html"},
            "source_file",
            id="source_file",
        ),
    ],
)
def test_tampered_raw_snapshot_meta_fails_closed_before_outputs_replaced(
    tmp_path: Path,
    meta_updates: dict[str, object],
    match: str,
) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(tmp_path, source)
    parsed_dir = tmp_path / "data" / "processed" / "parsed"
    _write_parsed(tmp_path, source, sha256, parsed_dir=parsed_dir)
    _write_raw_snapshot(tmp_path, meta_updates=meta_updates)
    if "source_file" in meta_updates:
        # 越界目标真实存在且字节一致，仍必须按路径规则拒绝。
        escaped = tmp_path / "data" / "raw" / "audit-doc" / "escaped.html"
        escaped.write_bytes(SOURCE_TEXT.encode("utf-8"))
    output_dir = tmp_path / "out"
    before = _sentinel_outputs(output_dir)

    with pytest.raises(BuildError, match=match):
        build_index(
            sources_path=sources,
            metadata_organization_path=organization,
            parsed_dir=parsed_dir,
            output_dir=output_dir,
        )

    _assert_outputs_unchanged(output_dir, before)


def test_existing_raw_snapshot_without_meta_fails_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    organization = _write_organization(tmp_path)
    source, sha256 = _write_source(tmp_path)
    sources = _write_sources(tmp_path, source)
    parsed_dir = tmp_path / "data" / "processed" / "parsed"
    _write_parsed(tmp_path, source, sha256, parsed_dir=parsed_dir)
    _write_raw_snapshot(tmp_path, write_meta=False)
    output_dir = tmp_path / "out"
    before = _sentinel_outputs(output_dir)

    with pytest.raises(BuildError, match=r"meta\.json"):
        build_index(
            sources_path=sources,
            metadata_organization_path=organization,
            parsed_dir=parsed_dir,
            output_dir=output_dir,
        )

    _assert_outputs_unchanged(output_dir, before)
