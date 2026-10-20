from __future__ import annotations

import pytest
from pydantic import ValidationError

from campusguide.config import (
    College,
    Major,
    OrganizationConfig,
    SourceDocument,
    SourcesConfig,
    validate_organization_refs,
)


def make_document(
    doc_id: str,
    *,
    issuer_level: str = "college",
    issuer_college_id: str | None = "engineering",
    parent_doc_id: str | None = None,
    relation: str = "standalone",
    audience_detail: dict[str, object] | None = None,
) -> SourceDocument:
    return SourceDocument.model_validate(
        {
            "doc_id": doc_id,
            "title": f"{doc_id} 实施细则",
            "doc_type": "regulation",
            "issuer_level": issuer_level,
            "issuer_college_id": issuer_college_id,
            "parent_doc_id": parent_doc_id,
            "relation": relation,
            "issuing_department": "工程学院",
            "audience": ["undergraduate"],
            "audience_detail": audience_detail
            or {"colleges": ["engineering"], "majors": ["computer-science"]},
            "versions": [
                {
                    "version_id": "v2025",
                    "url": "https://example.scau.edu.cn/page.htm",
                    "published_at": "2025-07-15",
                    "effective_from": "2025-09-01",
                }
            ],
        }
    )


def make_organization() -> OrganizationConfig:
    return OrganizationConfig(
        colleges=[College(college_id="engineering", name="工程学院")],
        majors=[
            Major(
                major_id="computer-science",
                name="计算机科学与技术",
                college_id="engineering",
                major_code="080901",
            )
        ],
    )


def test_major_must_belong_to_registered_college() -> None:
    with pytest.raises(ValidationError, match="未登记的学院"):
        OrganizationConfig(
            colleges=[College(college_id="engineering", name="工程学院")],
            majors=[
                Major(
                    major_id="computer-science",
                    name="计算机科学与技术",
                    college_id="science",
                )
            ],
        )


def test_valid_registry_has_no_reference_errors() -> None:
    document = make_document("engineering-rules")
    errors = validate_organization_refs(
        SourcesConfig(documents=[document]),
        make_organization(),
    )
    assert errors == []


def test_unknown_college_is_reported() -> None:
    document = make_document(
        "engineering-rules",
        audience_detail={"colleges": ["unknown"]},
    )
    errors = validate_organization_refs(
        SourcesConfig(documents=[document]),
        make_organization(),
    )
    assert any("未登记的学院" in error for error in errors)


def test_unknown_major_is_reported() -> None:
    document = make_document(
        "engineering-rules",
        audience_detail={"majors": ["unknown-major"]},
    )
    errors = validate_organization_refs(
        SourcesConfig(documents=[document]),
        make_organization(),
    )
    assert any("未登记的专业" in error for error in errors)


def test_major_outside_listed_college_is_reported() -> None:
    document = make_document(
        "engineering-rules",
        audience_detail={
            "colleges": ["science"],
            "majors": ["computer-science"],
        },
    )
    errors = validate_organization_refs(
        SourcesConfig(documents=[document]),
        make_organization(),
    )
    assert any("不属于所列学院" in error for error in errors)


def test_missing_parent_is_reported() -> None:
    document = make_document(
        "engineering-rules",
        relation="implements",
        parent_doc_id="missing-parent",
    )
    errors = validate_organization_refs(
        SourcesConfig(documents=[document]),
        make_organization(),
    )
    assert any("parent_doc_id 不存在" in error for error in errors)


def test_parent_cycle_is_reported() -> None:
    first = make_document(
        "engineering-rules-a",
        relation="implements",
        parent_doc_id="engineering-rules-b",
    )
    second = make_document(
        "engineering-rules-b",
        relation="implements",
        parent_doc_id="engineering-rules-a",
    )
    errors = validate_organization_refs(
        SourcesConfig(documents=[first, second]),
        make_organization(),
    )
    assert any("循环" in error for error in errors)


def test_university_document_cannot_implement_college_document() -> None:
    parent = make_document("engineering-rules")
    child = make_document(
        "university-rules",
        issuer_level="university",
        issuer_college_id=None,
        relation="implements",
        parent_doc_id="engineering-rules",
    )
    errors = validate_organization_refs(
        SourcesConfig(documents=[parent, child]),
        make_organization(),
    )
    assert any("不能以院/系级文件" in error for error in errors)
