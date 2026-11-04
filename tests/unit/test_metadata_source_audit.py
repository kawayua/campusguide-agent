"""来源元数据审计：真实临时文件字节与解析结构绑定。"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

import pytest

from campusguide.config import (
    ClassType,
    College,
    Major,
    OrganizationConfig,
    SourceDocument,
    SourceVersion,
)
from campusguide.domain.audience import AudienceDetail, AudienceScope
from campusguide.domain.enums import Audience, PolicyRelation
from campusguide.ingest.metadata_source import audit_metadata_source
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.structure import ParsedStructure, SourceLocator, StructureBlock

URL = "https://info.scau.edu.cn/2026/1010/c3772a448289/page.htm"
TITLE = "关于做好我院2026年本科生国家奖学金评审工作的通知"
PARENT_TITLE = "关于做好国家奖学金评审工作的通知"


def _organization() -> OrganizationConfig:
    return OrganizationConfig(
        colleges=[
            College(college_id="agri", name="农学院"),
            College(college_id="info", name="数学与信息学院"),
        ],
        majors=[
            Major(major_id="agronomy", name="农学", college_id="agri"),
            Major(
                major_id="seed-science",
                name="种子科学与工程",
                college_id="agri",
            ),
            Major(
                major_id="computer-science",
                name="计算机科学与技术",
                college_id="info",
            ),
        ],
        class_types=[
            ClassType(
                class_id="agronomy-general",
                name="农学普通班",
                majors=["agronomy"],
                aliases=["农学班", "普通农学"],
            ),
            ClassType(
                class_id="dingying-innovation",
                name="丁颖创新班",
                majors=["agronomy"],
                aliases=["丁颖班"],
            ),
            ClassType(
                class_id="new-agronomy-innovation",
                name="新农学创新班",
                majors=["agronomy"],
                aliases=["新农学本博班"],
            ),
            ClassType(
                class_id="info-innovation",
                name="信息类创新班",
                majors=["computer-science"],
                aliases=["信息类实验班"],
            ),
        ],
    )


def _version(
    *,
    local_path: str | None = None,
    url: str = URL,
    fmt: Literal["html", "pdf"] = "html",
) -> SourceVersion:
    return SourceVersion(
        version_id="v2026",
        url=url,
        local_path=local_path,
        published_at=date(2026, 10, 9),
        effective_from=date(2026, 10, 9),
        format=fmt,
    )


def _document(
    version: SourceVersion,
    *,
    doc_id: str = "audit-doc",
    title: str = TITLE,
    audience_detail: AudienceDetail | None = None,
    scopes: list[AudienceScope] | None = None,
    relation: PolicyRelation = PolicyRelation.STANDALONE,
    parent_doc_id: str | None = None,
    parent_version_id: str | None = None,
) -> SourceDocument:
    return SourceDocument(
        doc_id=doc_id,
        title=title,
        doc_type="notice",
        issuer_level="college",
        issuer_college_id="info",
        issuing_department="数学与信息学院、软件学院",
        audience=[Audience.UNDERGRADUATE],
        audience_detail=audience_detail,
        audience_scopes=scopes or [],
        relation=relation,
        parent_doc_id=parent_doc_id,
        parent_version_id=parent_version_id,
        versions=[version],
    )


def _parent_document() -> SourceDocument:
    return _document(
        _version(url="https://example.edu.cn/parent"),
        doc_id="parent-notice",
        title=PARENT_TITLE,
    )


def _write_source(
    tmp_path: Path,
    text: str,
    *,
    sidecar: bool = True,
    sidecar_updates: dict[str, object] | None = None,
    sidecar_text: str | None = None,
    meta: bool = False,
    meta_updates: dict[str, object] | None = None,
    meta_local_path: str | None = None,
    meta_http_status: int | None = 200,
    suffix: str = ".html",
) -> Path:
    source = tmp_path / f"source{suffix}"
    source.write_text(text, encoding="utf-8")
    if sidecar:
        if sidecar_text is not None:
            (tmp_path / "acquisition.json").write_text(
                sidecar_text,
                encoding="utf-8",
            )
        else:
            payload: dict[str, object] = {
                "doc_id": "audit-doc",
                "version_id": "v2026",
                "source_url": URL,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "fetched_at": "2026-11-01T11:21:46+00:00",
                "http_status": 200,
                "byte_size": source.stat().st_size,
                "local_path": str(source),
            }
            if sidecar_updates:
                payload.update(sidecar_updates)
            (tmp_path / "acquisition.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
    if meta:
        meta_payload: dict[str, object] = {
            "schema_version": "1.0",
            "doc_id": "audit-doc",
            "version_id": "v2026",
            "title": TITLE,
            "source_url": URL,
            "local_path": meta_local_path,
            "final_url": URL if meta_http_status == 200 else None,
            "http_status": meta_http_status,
            "content_type": "text/html",
            "format": "pdf" if suffix == ".pdf" else "html",
            "source_file": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "byte_size": source.stat().st_size,
            "fetched_at": "2026-11-01T11:21:46+00:00",
            "parser_version": "test-fetch",
        }
        if meta_updates:
            meta_payload.update(meta_updates)
        (tmp_path / "meta.json").write_text(
            json.dumps(meta_payload, ensure_ascii=False),
            encoding="utf-8",
        )
    return source


def _parsed(
    source: Path,
    *,
    title: str = TITLE,
    text: str | None = None,
    blocks: list[StructureBlock] | None = None,
    doc_id: str = "audit-doc",
    version_id: str = "v2026",
    source_sha256: str | None = None,
    include_local_path: bool = True,
    fmt: Literal["html", "pdf"] = "html",
) -> ParsedDocument:
    raw = source.read_bytes()
    return ParsedDocument(
        doc_id=doc_id,
        version_id=version_id,
        title=title,
        text=text if text is not None else title,
        format=fmt,
        source_sha256=source_sha256 or hashlib.sha256(raw).hexdigest(),
        source_url=URL,
        local_path=str(source) if include_local_path else None,
        parser_version="test-metadata-1",
        structure=(
            ParsedStructure(
                extractor_version="test",
                format=fmt,
                blocks=blocks,
            )
            if blocks is not None
            else None
        ),
    )


def _block(
    block_id: str,
    order: int,
    kind: str,
    text: str,
    level: int | None = None,
    *,
    fmt: Literal["html", "pdf"] = "html",
    page: int | None = None,
    bbox: list[float] | None = None,
) -> StructureBlock:
    return StructureBlock(
        block_id=block_id,
        order=order,
        kind=kind,
        text=text,
        level=level,
        locator=SourceLocator(format=fmt, page=page, bbox=bbox),
    )


def _run_audit(
    tmp_path: Path,
    text: str,
    *,
    doc_title: str = TITLE,
    parsed_title: str = TITLE,
    parsed_text: str | None = None,
    audience_detail: AudienceDetail | None = None,
    scopes: list[AudienceScope] | None = None,
    relation: PolicyRelation = PolicyRelation.STANDALONE,
    parent_doc_id: str | None = None,
    parent_version_id: str | None = None,
    blocks: list[StructureBlock] | None = None,
    parsed_updates: dict[str, object] | None = None,
    sidecar: bool = True,
    sidecar_updates: dict[str, object] | None = None,
    sidecar_text: str | None = None,
    meta: bool = False,
    meta_updates: dict[str, object] | None = None,
    remote_source: bool = False,
    source_documents_extra: dict[str, SourceDocument] | None = None,
    fmt: Literal["html", "pdf"] = "html",
) -> dict:
    suffix = ".pdf" if fmt == "pdf" else ".html"
    intended_source_path = tmp_path / f"source{suffix}"
    source = _write_source(
        tmp_path,
        text,
        sidecar=sidecar,
        sidecar_updates=sidecar_updates,
        sidecar_text=sidecar_text,
        meta=meta,
        meta_updates=meta_updates,
        meta_local_path=None if remote_source else str(intended_source_path),
        meta_http_status=200 if remote_source else None,
        suffix=suffix,
    )
    version = _version(
        local_path=None if remote_source else str(source),
        fmt=fmt,
    )
    doc = _document(
        version,
        title=doc_title,
        audience_detail=audience_detail,
        scopes=scopes,
        relation=relation,
        parent_doc_id=parent_doc_id,
        parent_version_id=parent_version_id,
    )
    parsed = _parsed(
        source,
        title=parsed_title,
        text=parsed_text if parsed_text is not None else text,
        blocks=blocks,
        include_local_path=not remote_source,
        fmt=fmt,
        **(parsed_updates or {}),
    )
    documents = {doc.doc_id: doc}
    if source_documents_extra:
        documents.update(source_documents_extra)
    return audit_metadata_source(
        doc,
        version,
        parsed,
        source_path=source,
        organization=_organization(),
        source_documents=documents,
    )


def _codes(result: dict, bucket: str) -> set[str]:
    return {item["code"] for item in result[bucket]}


def _find_evidence(result: dict, field: str) -> dict:
    for item in result["evidence"]:
        if item["field"] == field:
            return item
    raise AssertionError(f"缺少证据记录：{field}")


def test_source_sha256_mismatch_is_fatal(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文内容。",
        parsed_updates={"source_sha256": "f" * 64},
    )

    assert result["status"] == "failed"
    assert "source_sha256_mismatch" in _codes(result, "fatal")
    assert _find_evidence(result, "source.sha256")["status"] == "fatal"


def test_config_title_must_match_parsed_source_title(tmp_path: Path) -> None:
    source_title = "真实的官网标题"
    result = _run_audit(
        tmp_path,
        f"{source_title}\n正文。",
        doc_title="伪造的配置标题",
        parsed_title=source_title,
    )

    assert result["status"] == "review"
    assert "title_mismatch" in _codes(result, "review")
    evidence = _find_evidence(result, "title.source")
    assert evidence["status"] == "declared_unverified"
    assert evidence["quote"] == source_title


def test_known_innovation_class_verified_from_heading(tmp_path: Path) -> None:
    text = f"{TITLE}\n新农学创新班评审安排\n学院开展评审。"
    blocks = [
        _block("b1", 1, "heading", "新农学创新班评审安排", 2),
        _block("b2", 2, "paragraph", "学院开展评审。"),
    ]
    result = _run_audit(
        tmp_path,
        text,
        audience_detail=AudienceDetail(class_types=["new-agronomy-innovation"]),
        blocks=blocks,
    )

    assert result["status"] == "verified"
    support = _find_evidence(
        result,
        "document.audience_detail.class_types[0].source_support",
    )
    assert support["status"] == "verified"
    assert support["method"] == "heading_token_match"
    assert "新农学创新班" in support["quote"]
    assert support["quote"] in text


def test_body_class_negation_does_not_verify_document_class(tmp_path: Path) -> None:
    title = "农学专业人才培养方案"
    result = _run_audit(
        tmp_path,
        f"{title}\n本方案不适用于丁颖创新班。",
        doc_title=title,
        parsed_title=title,
        audience_detail=AudienceDetail(class_types=["dingying-innovation"]),
    )

    assert result["status"] == "failed"
    assert "class_type_source_missing" in _codes(result, "fatal")


def test_class_heading_match_normalizes_whitespace_but_quote_is_verbatim(
    tmp_path: Path,
) -> None:
    text = f"{TITLE}\n新农学 创新班评审安排\n学院开展评审。"
    blocks = [
        _block("b1", 1, "heading", "新农学 创新班评审安排", 2),
        _block("b2", 2, "paragraph", "学院开展评审。"),
    ]
    result = _run_audit(
        tmp_path,
        text,
        audience_detail=AudienceDetail(class_types=["new-agronomy-innovation"]),
        blocks=blocks,
    )

    assert result["status"] == "verified"
    support = _find_evidence(
        result,
        "document.audience_detail.class_types[0].source_support",
    )
    assert support["status"] == "verified"
    assert support["quote"] == "新农学 创新班"
    assert support["quote"] in text


def test_general_agronomy_requires_explicit_heading_phrase(tmp_path: Path) -> None:
    text = f"{TITLE}\n农学专业人才培养方案\n培养目标与课程设置。"
    blocks = [
        _block("b1", 1, "heading", "农学专业人才培养方案", 2),
        _block("b2", 2, "paragraph", "培养目标与课程设置。"),
    ]
    result = _run_audit(
        tmp_path,
        text,
        audience_detail=AudienceDetail(class_types=["agronomy-general"]),
        blocks=blocks,
    )

    assert result["status"] == "verified"
    support = _find_evidence(
        result,
        "document.audience_detail.class_types[0].source_support",
    )
    assert support["status"] == "verified"
    assert support["method"] == "general_class_heading_phrase"
    assert "农学专业人才培养方案" in support["quote"]


def test_general_agronomy_not_inferred_from_absence_of_innovation_words(
    tmp_path: Path,
) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n本方案面向农学专业学生，未设创新班。",
        audience_detail=AudienceDetail(class_types=["agronomy-general"]),
    )

    assert result["status"] == "failed"
    assert "class_type_source_missing" in _codes(result, "fatal")


def test_general_class_name_heading_alone_is_not_enough(tmp_path: Path) -> None:
    text = f"{TITLE}\n农学普通班\n正文。"
    blocks = [
        _block("b1", 1, "heading", "农学普通班", 2),
        _block("b2", 2, "paragraph", "正文。"),
    ]
    result = _run_audit(
        tmp_path,
        text,
        audience_detail=AudienceDetail(class_types=["agronomy-general"]),
        blocks=blocks,
    )

    assert result["status"] == "failed"
    assert "class_type_source_missing" in _codes(result, "fatal")


def test_scope_match_must_appear_in_parsed_headings(tmp_path: Path) -> None:
    text = f"{TITLE}\n培养目标\n本方案适用于农学专业。\n课程设置"
    blocks = [
        _block("b1", 1, "heading", "培养目标", 2),
        _block("b2", 2, "paragraph", "本方案适用于农学专业。"),
        _block("b3", 3, "heading", "课程设置", 2),
    ]
    scope = AudienceScope(
        match="适用于农学专业",
        majors=["agronomy"],
        class_types=["agronomy-general"],
        evidence="本方案适用于农学专业。",
    )
    result = _run_audit(tmp_path, text, scopes=[scope], blocks=blocks)

    assert result["status"] == "failed"
    assert "scope_match_not_in_headings" in _codes(result, "fatal")


def test_scope_evidence_outside_matched_section_is_fatal(tmp_path: Path) -> None:
    text = (
        f"{TITLE}\n"
        "培养目标\n"
        "本方案适用于农学专业。\n"
        "其他说明\n"
        "农学普通班按本方案执行。\n"
    )
    blocks = [
        _block("b1", 1, "heading", "培养目标", 2),
        _block("b2", 2, "paragraph", "本方案适用于农学专业。"),
        _block("b3", 3, "heading", "其他说明", 2),
        _block("b4", 4, "paragraph", "农学普通班按本方案执行。"),
    ]
    scope = AudienceScope(
        match="培养目标",
        majors=["agronomy"],
        class_types=[],
        evidence="农学普通班按本方案执行。",
    )
    result = _run_audit(tmp_path, text, scopes=[scope], blocks=blocks)

    assert result["status"] == "failed"
    assert "scope_evidence_outside_section" in _codes(result, "fatal")
    assert _find_evidence(result, "audience_scopes[0].evidence")["status"] == "fatal"


def test_scope_other_class_body_mention_does_not_verify_class(
    tmp_path: Path,
) -> None:
    title = "农学专业人才培养方案"
    text = f"{title}\n培养目标\n本方案不适用于丁颖创新班。\n课程设置"
    blocks = [
        _block("b1", 1, "heading", title, 2),
        _block("b2", 2, "heading", "培养目标", 2),
        _block("b3", 3, "paragraph", "本方案不适用于丁颖创新班。"),
        _block("b4", 4, "heading", "课程设置", 2),
    ]
    scope = AudienceScope(
        match="培养目标",
        majors=["agronomy"],
        class_types=["dingying-innovation"],
        evidence="本方案不适用于丁颖创新班。",
    )
    result = _run_audit(
        tmp_path,
        text,
        doc_title=title,
        parsed_title=title,
        scopes=[scope],
        blocks=blocks,
    )

    assert result["status"] == "failed"
    assert "scope_class_type_source_missing" in _codes(result, "fatal")


def test_empty_class_list_is_valid_shared_unknown(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        audience_detail=AudienceDetail(class_types=[]),
    )

    assert result["status"] == "verified"
    assert not result["fatal"]
    assert not result["review"]
    evidence = _find_evidence(result, "document.audience_detail.class_types")
    assert evidence["value"] == []
    assert evidence["status"] == "not_applicable"


def test_unknown_class_id_is_fatal_even_if_token_exists(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n未知班型 not-in-registry。",
        audience_detail=AudienceDetail(class_types=["not-in-registry"]),
    )

    assert result["status"] == "failed"
    assert "unknown_class_type" in _codes(result, "fatal")
    support = _find_evidence(
        result,
        "document.audience_detail.class_types[0].source_support",
    )
    assert support["status"] == "fatal"
    assert support["method"] == "registry_lookup"


def test_genuine_parent_citation_verified_but_relation_type_not_proven(
    tmp_path: Path,
) -> None:
    parent = _parent_document()
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n依据《{PARENT_TITLE}》开展评审。",
        relation=PolicyRelation.BASIS,
        parent_doc_id=parent.doc_id,
        parent_version_id="v2026",
        source_documents_extra={parent.doc_id: parent},
    )

    assert result["status"] == "verified"
    assert not result["review"]
    citation = _find_evidence(result, "relation.citation")
    assert citation["status"] == "verified"
    assert citation["citation_verified"] is True
    assert citation["relation_verified"] is False
    relation = _find_evidence(result, "relation")
    assert relation["value"] == "basis"
    assert relation["status"] == "declared_unverified"


def test_same_topic_without_named_parent_citation_needs_review(tmp_path: Path) -> None:
    parent = _parent_document()
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n国家奖学金评审工作安排如下，请各年级按要求执行。",
        relation=PolicyRelation.BASIS,
        parent_doc_id=parent.doc_id,
        parent_version_id="v2026",
        source_documents_extra={parent.doc_id: parent},
    )

    assert result["status"] == "review"
    assert "relation_citation_unverified" in _codes(result, "review")
    citation = _find_evidence(result, "relation.citation")
    assert citation["status"] == "declared_unverified"
    assert citation["citation_verified"] is False
    assert citation["relation_verified"] is False


def test_absent_parent_is_fatal(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        relation=PolicyRelation.BASIS,
        parent_doc_id="missing-parent",
        parent_version_id="v2026",
    )

    assert result["status"] == "failed"
    assert "relation_parent_missing" in _codes(result, "fatal")
    assert _find_evidence(result, "relation.parent")["status"] == "fatal"


def test_evidence_quotes_are_real_source_substrings(tmp_path: Path) -> None:
    text = (
        f"{TITLE}\n"
        "农学专业人才培养方案\n"
        "新农学创新班评审安排\n"
        "本方案适用于农学专业普通班学生。\n"
        "课程设置"
    )
    blocks = [
        _block("b1", 1, "heading", "农学专业人才培养方案", 2),
        _block("b2", 2, "heading", "新农学创新班评审安排", 3),
        _block("b3", 3, "paragraph", "本方案适用于农学专业普通班学生。"),
        _block("b4", 4, "heading", "课程设置", 2),
    ]
    scope = AudienceScope(
        match="农学专业人才培养方案",
        majors=["agronomy"],
        class_types=["agronomy-general"],
        evidence="本方案适用于农学专业普通班学生。",
    )
    result = _run_audit(
        tmp_path,
        text,
        audience_detail=AudienceDetail(
            class_types=["new-agronomy-innovation", "agronomy-general"]
        ),
        scopes=[scope],
        blocks=blocks,
    )

    assert result["status"] == "verified"
    source_strings = [text, TITLE, *(block.text for block in blocks)]
    for record in result["evidence"]:
        assert record["doc_id"] == "audit-doc"
        assert record["version_id"] == "v2026"
        assert record["source_sha256"]
        assert record["source_url"] == URL
        if record["quote"] is not None:
            assert any(record["quote"] in source for source in source_strings), record
    json.dumps(result, ensure_ascii=False)


def test_missing_acquisition_sidecar_is_review(tmp_path: Path) -> None:
    result = _run_audit(tmp_path, f"{TITLE}\n正文。", sidecar=False)

    assert result["status"] == "review"
    assert "acquisition_sidecar_missing" in _codes(result, "review")
    assert _find_evidence(result, "source.provenance")["status"] == "declared_unverified"


def test_acquisition_sidecar_sha_mismatch_is_fatal(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        sidecar_updates={"source_sha256": "0" * 64},
    )

    assert result["status"] == "failed"
    assert "acquisition_sha256_mismatch" in _codes(result, "fatal")
    assert _find_evidence(result, "source.provenance")["status"] == "fatal"


def test_remote_source_without_parsed_local_path_and_raw_meta_is_verified(
    tmp_path: Path,
) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        sidecar=False,
        meta=True,
        remote_source=True,
    )

    assert result["status"] == "verified"
    assert not result["fatal"]
    assert not result["review"]
    provenance = _find_evidence(result, "source.provenance")
    assert provenance["status"] == "verified"
    assert provenance["method"] == "raw_fetch_meta"
    assert provenance["value"]["source_sha256"] == result["source"]["sha256"]
    assert provenance["value"]["http_status"] == 200
    local_path = _find_evidence(result, "parsed.local_path")
    assert local_path["status"] == "not_applicable"
    assert result["source"]["provenance_method"] == "raw_fetch_meta"
    assert result["source"]["acquisition_status"] == "verified"


def test_local_copy_raw_meta_remains_declared_review(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        sidecar=False,
        meta=True,
    )

    assert result["status"] == "review"
    assert not result["fatal"]
    assert "raw_meta_local_copy" in _codes(result, "review")
    provenance = _find_evidence(result, "source.provenance")
    assert provenance["status"] == "declared_unverified"
    assert provenance["method"] == "raw_meta_local_copy"
    assert provenance["value"]["http_status"] is None
    assert provenance["value"]["local_path"] == str(tmp_path / "source.html")
    assert result["source"]["acquisition_status"] == "verified_local_copy"


@pytest.mark.parametrize(
    ("meta_updates", "code"),
    [
        pytest.param({"sha256": "0" * 64}, "raw_meta_sha256_mismatch", id="sha256"),
        pytest.param(
            {"source_url": "https://example.edu.cn/forged"},
            "raw_meta_url_mismatch",
            id="source_url",
        ),
        pytest.param(
            {"source_file": "../escaped.html"},
            "raw_meta_source_file_unsafe",
            id="source_file_traversal",
        ),
        pytest.param(
            {"source_file": "snapshot.html"},
            "raw_meta_source_file_mismatch",
            id="source_file_mismatch",
        ),
    ],
)
def test_inconsistent_raw_meta_is_rejected(
    tmp_path: Path,
    meta_updates: dict[str, object],
    code: str,
) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        sidecar=False,
        meta=True,
        meta_updates=meta_updates,
        remote_source=True,
    )

    assert result["status"] == "failed"
    assert code in _codes(result, "fatal")
    assert _find_evidence(result, "source.provenance")["status"] == "fatal"


def test_absolute_raw_meta_source_file_is_rejected(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        sidecar=False,
        meta=True,
        meta_updates={"source_file": str(tmp_path / "escaped.html")},
        remote_source=True,
    )

    assert result["status"] == "failed"
    assert "raw_meta_source_file_unsafe" in _codes(result, "fatal")


def test_malformed_acquisition_does_not_fall_back_to_raw_meta(
    tmp_path: Path,
) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        sidecar_text="{not-json",
        meta=True,
        remote_source=True,
    )

    assert result["status"] == "failed"
    assert "acquisition_sidecar_unreadable" in _codes(result, "fatal")
    provenance = _find_evidence(result, "source.provenance")
    assert provenance["status"] == "fatal"
    assert provenance["method"] == "acquisition_sidecar"
    assert result["source"]["provenance_method"] == "acquisition_sidecar"


def test_parsed_document_version_identity_is_enforced(tmp_path: Path) -> None:
    result = _run_audit(
        tmp_path,
        f"{TITLE}\n正文。",
        parsed_updates={"version_id": "v2025"},
    )

    assert result["status"] == "failed"
    assert "parsed_document_identity_mismatch" in _codes(result, "fatal")


def test_scope_evidence_description_quote_is_verified_without_fatal(tmp_path: Path) -> None:
    text = f"{TITLE}\n正文。"
    detail = AudienceDetail(scope_evidence=f"文件标题“{TITLE}”")

    result = _run_audit(tmp_path, text, audience_detail=detail)

    assert result["status"] == "verified"
    assert not result["fatal"]
    config_evidence = _find_evidence(result, "audience.scope_evidence")
    assert config_evidence["status"] == "declared_unverified"
    source_evidence = _find_evidence(result, "audience.scope_evidence.source")
    assert source_evidence["status"] == "verified"
    assert source_evidence["quote"] == TITLE
    assert source_evidence["quote"] in text


def test_unverifiable_scope_evidence_description_is_review(tmp_path: Path) -> None:
    detail = AudienceDetail(scope_evidence="文件标题“不存在的2026年通知”")

    result = _run_audit(tmp_path, f"{TITLE}\n正文。", audience_detail=detail)

    assert result["status"] == "review"
    assert not result["fatal"]
    assert "scope_evidence_description_unverified" in _codes(result, "review")
    source_evidence = _find_evidence(result, "audience.scope_evidence.source")
    assert source_evidence["status"] == "declared_unverified"
    assert source_evidence["quote"] is None


def test_paragraph_title_sections_bound_scopes_without_contamination(
    tmp_path: Path,
) -> None:
    title_a = "农学专业人才培养方案"
    title_b = "农学（丁颖创新班）人才培养方案"
    title_c = "种子科学与工程专业人才培养方案"
    body_a = "农学普通班执行本方案。"
    other_scope_mention = "种子科学与工程专业另见其他章节。"
    body_b = "丁颖创新班单独成班。"
    title_mention = "农学专业人才培养方案以本节为准。"
    body_c = "种子科学与工程实践环节。"
    trailing_mention = "农学专业人才培养方案在别处另有说明。"
    text = "\n".join(
        [
            TITLE,
            title_a,
            body_a,
            other_scope_mention,
            title_b,
            body_b,
            title_mention,
            title_c,
            body_c,
            trailing_mention,
        ]
    )
    blocks = [
        _block("b1", 1, "paragraph", title_a, fmt="pdf", page=1, bbox=[10, 20, 300, 40]),
        _block("b2", 2, "paragraph", body_a, fmt="pdf", page=2),
        _block("b3", 3, "paragraph", other_scope_mention, fmt="pdf", page=3),
        _block("b4", 4, "paragraph", title_b, fmt="pdf", page=15, bbox=[10, 20, 320, 40]),
        _block("b5", 5, "paragraph", body_b, fmt="pdf", page=16),
        _block("b6", 6, "paragraph", title_mention, fmt="pdf", page=17),
        _block("b7", 7, "paragraph", title_c, fmt="pdf", page=29, bbox=[10, 20, 340, 40]),
        _block("b8", 8, "paragraph", body_c, fmt="pdf", page=30),
        _block("b9", 9, "paragraph", trailing_mention, fmt="pdf", page=31),
    ]
    scopes = [
        AudienceScope(
            match="丁颖创新班",
            majors=["agronomy"],
            class_types=["dingying-innovation"],
            evidence=f"章节标题“{title_b}”",
        ),
        AudienceScope(
            match="种子科学与工程",
            majors=["seed-science"],
            evidence=f"章节标题“{title_c}”",
        ),
        AudienceScope(
            match="农学专业人才培养方案",
            majors=["agronomy"],
            class_types=["agronomy-general"],
            evidence=body_a,
        ),
    ]

    result = _run_audit(tmp_path, text, scopes=scopes, blocks=blocks, fmt="pdf")

    assert result["status"] == "verified"
    assert not result["fatal"]
    match_a = _find_evidence(result, "audience_scopes[0].match.source")
    assert match_a["location"] == "paragraph_title:b4"
    assert match_a["quote"] == "丁颖创新班"
    assert match_a["page"] == 15
    match_b = _find_evidence(result, "audience_scopes[1].match.source")
    assert match_b["location"] == "paragraph_title:b7"
    assert match_b["quote"] == "种子科学与工程"
    assert match_b["page"] == 29
    match_c = _find_evidence(result, "audience_scopes[2].match.source")
    assert match_c["location"] == "paragraph_title:b1"
    assert match_c["quote"] == title_a
    assert match_c["page"] == 1
    assert match_c["bbox"] == [10.0, 20.0, 300.0, 40.0]
    evidence_a = _find_evidence(result, "audience_scopes[0].evidence")
    assert evidence_a["status"] == "verified"
    assert evidence_a["quote"] == title_b
    assert evidence_a["location"] == "section:b4:b4"
    evidence_b = _find_evidence(result, "audience_scopes[1].evidence")
    assert evidence_b["status"] == "verified"
    assert evidence_b["quote"] == title_c
    assert evidence_b["location"] == "section:b7:b7"
    evidence_c = _find_evidence(result, "audience_scopes[2].evidence")
    assert evidence_c["status"] == "verified"
    assert evidence_c["quote"] == body_a
    assert evidence_c["location"] == "section:b1:b2"
    support_a = _find_evidence(
        result,
        "audience_scopes[0].class_types[0].source_support",
    )
    assert support_a["status"] == "verified"
    assert support_a["quote"] == "丁颖创新班"
    assert support_a["location"] == "section:b4:b4"
    support_c = _find_evidence(
        result,
        "audience_scopes[2].class_types[0].source_support",
    )
    assert support_c["status"] == "verified"
    assert support_c["quote"] == title_a
    assert support_c["location"] == "section:b1:b1"


def test_fake_parsed_title_hint_cannot_validate_class(tmp_path: Path) -> None:
    fake_title = "新农学创新班人才培养方案"
    html = f"<html><head><title>{TITLE}</title></head><body><p>正文。</p></body></html>"

    result = _run_audit(
        tmp_path,
        html,
        doc_title=fake_title,
        parsed_title=fake_title,
        parsed_text=f"{TITLE}\n正文。",
        audience_detail=AudienceDetail(class_types=["new-agronomy-innovation"]),
    )

    assert result["status"] == "failed"
    assert "class_type_source_missing" in _codes(result, "fatal")
    assert "parsed_title_not_in_source" in _codes(result, "review")
    title_evidence = _find_evidence(result, "title.source")
    assert title_evidence["status"] == "declared_unverified"
    assert title_evidence["quote"] is None


def test_html_title_tag_verifies_parsed_title_and_class(tmp_path: Path) -> None:
    title = "新农学创新班人才培养方案"
    html = (
        f"<html><head><title>{title} - 华南农业大学</title></head>"
        "<body><p>正文。</p></body></html>"
    )

    result = _run_audit(
        tmp_path,
        html,
        doc_title=title,
        parsed_title=title,
        parsed_text="正文。",
        audience_detail=AudienceDetail(class_types=["new-agronomy-innovation"]),
    )

    assert result["status"] == "verified"
    title_evidence = _find_evidence(result, "title.source")
    assert title_evidence["status"] == "verified"
    assert title_evidence["method"] == "html_title_element_match"
    assert title_evidence["quote"] == title
    assert title_evidence["location"] == "html.title"
    support = _find_evidence(
        result,
        "document.audience_detail.class_types[0].source_support",
    )
    assert support["status"] == "verified"
    assert support["quote"] == "新农学创新班"
    assert support["location"] == "html.title"


def test_pdf_paragraph_title_block_verifies_parsed_title(tmp_path: Path) -> None:
    title = "农学专业人才培养方案"
    blocks = [
        _block("b1", 1, "paragraph", title, fmt="pdf", page=1, bbox=[10, 20, 300, 40]),
    ]

    result = _run_audit(
        tmp_path,
        f"{title}\n正文。",
        doc_title=title,
        parsed_title=title,
        blocks=blocks,
        fmt="pdf",
    )

    assert result["status"] == "verified"
    title_evidence = _find_evidence(result, "title.source")
    assert title_evidence["status"] == "verified"
    assert title_evidence["method"] == "structural_title_match"
    assert title_evidence["quote"] == title
    assert title_evidence["location"] == "paragraph_title:b1"
    assert title_evidence["page"] == 1
