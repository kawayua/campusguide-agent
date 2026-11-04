"""C 类元数据：低置信日期依据与 2027 届推免文档必须显式可执行。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from campusguide.config import SourceDocument, SourceVersion, load_sources
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import Audience, PolicyRelation, ScopeStatus

ROOT = Path(__file__).resolve().parents[2]
DOC_NOTE_PREFIX = "document.audience_detail.note:"


def test_low_confidence_date_source_keeps_document_number_evidence() -> None:
    version = SourceVersion(
        version_id="v2010",
        url="https://example.edu.cn/policy",
        published_at=date(2010, 12, 31),
        notes="文号：华南农办〔2010〕70号；仅知印发年份（date_confidence=low）。",
    )

    assert version.date_confidence == "low"
    assert "华南农办〔2010〕70号" in (version.date_source or "")
    assert "date_confidence=low" not in (version.date_source or "")


def _document(**overrides) -> SourceDocument:
    payload = {
        "doc_id": "legacy-static-page",
        "title": "旧版静态页",
        "issuing_department": "测试学院",
        "audience": [Audience.PUBLIC],
        "audience_detail": AudienceDetail(
            note="静态页面；页面无明确发布日期，published_at 取核验日期（date_confidence=low）。"
        ),
        "versions": [
            SourceVersion(
                version_id="v2026",
                url="https://example.edu.cn/static",
                published_at=date(2026, 10, 21),
                notes="结构化转录自官网静态页。",
            )
        ],
    }
    payload.update(overrides)
    return SourceDocument(**payload)


def test_explicit_version_high_confidence_overrides_version_note() -> None:
    version = SourceVersion(
        version_id="v2026",
        url="https://example.edu.cn/policy",
        published_at=date(2026, 10, 21),
        date_confidence="high",
        date_source="正文落款明确标注 2026-10-21。",
        notes="仅知文号年份（date_confidence=low）。",
    )

    assert version.date_confidence == "high"
    assert version.date_source == "正文落款明确标注 2026-10-21。"


def test_document_level_low_note_is_inherited_by_versions() -> None:
    document = _document()

    version = document.versions[0]
    assert version.date_confidence == "low"
    assert (version.date_source or "").startswith(DOC_NOTE_PREFIX)
    assert "date_confidence=low" not in (version.date_source or "")
    assert "静态页面" in (version.date_source or "")


def test_explicit_version_confidence_overrides_document_note() -> None:
    document = _document(
        versions=[
            SourceVersion(
                version_id="v2026",
                url="https://example.edu.cn/static",
                published_at=date(2026, 10, 21),
                date_confidence="high",
                date_source="页面页脚明确标注 2026-10-21。",
            )
        ]
    )

    version = document.versions[0]
    assert version.date_confidence == "high"
    assert version.date_source == "页面页脚明确标注 2026-10-21。"


def test_all_audited_versions_materialize_low_date_evidence() -> None:
    config = load_sources(ROOT / "configs" / "sources.yaml")
    low_versions = [
        (document.doc_id, version)
        for document in config.documents
        for version in document.versions
        if version.date_confidence == "low"
    ]

    from campusguide.ingest.build_index import EXPECTED_LOW_CONFIDENCE_VERSIONS

    assert EXPECTED_LOW_CONFIDENCE_VERSIONS == 43
    assert len(low_versions) == EXPECTED_LOW_CONFIDENCE_VERSIONS
    assert all(version.date_source for _, version in low_versions)
    document_note_docs = {
        "info-profile",
        "info-leadership",
        "info-committees",
        "info-student-affairs",
        "info-supervisors",
        "info-teachers-math",
        "info-teachers-cs",
        "info-teachers-ie",
        "info-majors-undergrad",
        "info-majors-graduate",
        "info-org-structure",
        "info-practice-bases",
        "info-competitions",
        "info-scholarships",
        "info-teaching-centers",
        "info-industry-colleges",
        "info-admissions-undergrad",
        "info-admissions-graduate",
        "info-student-orgs",
        "agri-committee-academic",
        "agri-committee-degree",
        "agri-dept-plant-breeding",
        "agri-dept-crop-info",
        "agri-dept-crop-tech",
        "agri-dept-seed-engineering",
    }
    for doc_id, version in low_versions:
        if doc_id in document_note_docs:
            assert (version.date_source or "").startswith(DOC_NOTE_PREFIX), doc_id


def test_national_aid_2026_merit_records_publication_and_inferred_effective_anchor() -> None:
    config = load_sources(ROOT / "configs" / "sources.yaml")
    documents = {document.doc_id: document for document in config.documents}
    document = documents["national-aid-2026-info-merit"]
    versions = [
        version for version in document.versions if version.version_id == "v2026"
    ]

    assert len(versions) == 1
    version = versions[0]
    assert version.published_at == date(2026, 10, 10)
    assert version.effective_from == date(2026, 10, 9)
    assert version.date_confidence == "low"
    date_source = version.date_source or ""
    assert "网页实际发布时间 2026-10-10" in date_source
    assert "正文落款 2026-10-09" in date_source
    assert "effective_from 仅为落款日期锚点" in date_source
    assert "推断，非原文" in date_source


def test_admission_year_blocks_unrestricted_scope_status() -> None:
    detail = AudienceDetail(admission_year=2026)

    assert detail.has_scope_constraints is True
    with pytest.raises(ValidationError, match="scope_status=unrestricted"):
        AudienceDetail(admission_year=2026, scope_status=ScopeStatus.UNRESTRICTED)


def test_recommendation_2027_documents_keep_graduating_year_and_only_notice_has_basis() -> None:
    config = load_sources(ROOT / "configs" / "sources.yaml")
    documents = {document.doc_id: document for document in config.documents}

    notice = documents["recommendation-2027-notice"]
    assert notice.audience_detail is not None
    assert notice.audience_detail.graduating_years == [2027]
    assert notice.relation is PolicyRelation.BASIS
    assert notice.parent_doc_id == "postgraduate-recommendation"

    publicity = documents["recommendation-2027-publicity"]
    assert publicity.audience_detail is not None
    assert publicity.audience_detail.graduating_years == [2027]
    # 公示页没有上位依据原文，不能为了提高关系覆盖率编造 basis。
    assert publicity.relation is PolicyRelation.STANDALONE
    assert publicity.parent_doc_id is None
