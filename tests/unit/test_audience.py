from __future__ import annotations

import pytest
from pydantic import ValidationError

from campusguide.config import SourceDocument
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import AuthorityLevel


def doc_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "doc_id": "status-rules",
        "title": "本科学生学籍管理实施细则",
        "doc_type": "regulation",
        "issuing_department": "教务处",
        "audience": ["undergraduate"],
        "versions": [
            {
                "version_id": "v2025",
                "url": "https://jwc.scau.edu.cn/example/page.htm",
                "published_at": "2025-07-15",
                "effective_from": "2025-09-01",
                "status": "active",
                "format": "html",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_audience_detail_rejects_two_cohort_modes() -> None:
    with pytest.raises(ValidationError):
        AudienceDetail(intake_years=[2021], intake_from=2021)


def test_audience_detail_rejects_bad_cycle() -> None:
    with pytest.raises(ValidationError):
        AudienceDetail(cycle="2025/2026")


def test_student_constraints_require_student_role() -> None:
    with pytest.raises(ValidationError, match="非学生角色"):
        SourceDocument.model_validate(
            doc_payload(
                audience=["faculty"],
                audience_detail={"stages": ["year4"]},
            )
        )


def test_college_document_requires_issuer_college() -> None:
    with pytest.raises(ValidationError, match="issuer_college_id"):
        SourceDocument.model_validate(doc_payload(issuer_level="college"))


def test_implements_requires_parent_document() -> None:
    with pytest.raises(ValidationError, match="parent_doc_id"):
        SourceDocument.model_validate(doc_payload(relation="implements"))


def test_standalone_cannot_have_parent_document() -> None:
    with pytest.raises(ValidationError, match="standalone"):
        SourceDocument.model_validate(doc_payload(parent_doc_id="parent-rules"))


def test_all_cannot_be_mixed_with_specific_role() -> None:
    with pytest.raises(ValidationError, match="all"):
        SourceDocument.model_validate(doc_payload(audience=["all", "undergraduate"]))


def test_version_level_audience_override_is_allowed() -> None:
    document = SourceDocument.model_validate(
        doc_payload(
            audience_detail={"stages": ["year1"]},
            versions=[
                {
                    "version_id": "v2025",
                    "url": "https://jwc.scau.edu.cn/example/page.htm",
                    "published_at": "2025-07-15",
                    "effective_from": "2025-09-01",
                    "status": "active",
                    "audience": ["graduate"],
                    "audience_detail": {"stages": ["final_year"]},
                }
            ],
        )
    )
    assert document.versions[0].audience == ["graduate"]
    assert document.versions[0].audience_detail is not None
    assert document.versions[0].audience_detail.stages[0].value == "final_year"


def test_authority_level_follows_doc_type_by_default() -> None:
    regulation = SourceDocument.model_validate(doc_payload())
    notice = SourceDocument.model_validate(doc_payload(doc_type="notice"))
    report = SourceDocument.model_validate(doc_payload(doc_type="report"))
    assert regulation.effective_authority_level is AuthorityLevel.BINDING
    assert notice.effective_authority_level is AuthorityLevel.OPERATIONAL
    assert report.effective_authority_level is AuthorityLevel.INFORMATIONAL
