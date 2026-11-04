"""Shadow OCR bridge tests.

Synthetic evidence only: these tests prove binding, coverage, redaction and
writer contracts. They do not claim OCR accuracy or visual verification.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from campusguide.domain.audience import AudienceDetail, AudienceScope
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    AudienceStage,
    DegreeType,
    IssuerLevel,
    PolicyRelation,
    ScopeStatus,
)
from campusguide.domain.models import DocumentVersion
from campusguide.ingest.ocr_layout import (
    LayoutPageResult,
    LayoutRegion,
    PageImageEvidence,
    RegionCrop,
)
from campusguide.ingest.ocr_router import FallbackResult, RoutedRegionResult
from campusguide.ingest.ocr_shadow import (
    OcrShadowBundle,
    ShadowValidationError,
    build_shadow_bundle,
    write_shadow_bundle,
)
from campusguide.ingest.structure import SourceLocator, TableCell, TableRow, TableStructure
from campusguide.ingest.xiaomi_ocr import OcrPageEvidence, OcrPageResult, OcrUsage

DOC_ID = "shadow-doc"
VERSION_ID = "v1"
SOURCE_SHA = "a" * 64
SOURCE_URL = "https://example.edu/shadow"
DETECTOR_ID = "test-layout"
DETECTOR_REVISION = "rev1"
TABLE_KINDS = {"table_parent", "table_row", "matrix_row"}


def _version() -> DocumentVersion:
    return DocumentVersion(
        doc_id=DOC_ID,
        version_id=VERSION_ID,
        title="影子测试办法",
        doc_type="regulation",
        authority_level="operational",
        issuer_level=IssuerLevel.UNIVERSITY,
        parent_doc_id="parent-doc",
        parent_version_id="parent-v1",
        relation=PolicyRelation.IMPLEMENTS,
        issuing_department="教务处",
        source_url=SOURCE_URL,
        published_at=date(2026, 9, 1),
        effective_from=date(2026, 9, 2),
        status=ArticleStatus.ACTIVE,
        source_sha256=SOURCE_SHA,
        fetched_at=datetime(2026, 10, 30, tzinfo=UTC),
        parser_version="seed-parser",
        audience=[Audience.UNDERGRADUATE],
        audience_detail=AudienceDetail(
            majors=["agronomy"],
            intake_from=2024,
            stages=[AudienceStage.YEAR2],
            degree_types=[DegreeType.ACADEMIC],
        ),
        audience_scopes=[
            AudienceScope(
                match="农学专业",
                majors=["agronomy"],
                class_types=["dingying-class"],
                evidence="培养方案原文",
            )
        ],
        date_confidence="low",
        date_source="培养方案落款",
    )


def _page(number: int) -> PageImageEvidence:
    return PageImageEvidence(
        doc_id=DOC_ID,
        version_id=VERSION_ID,
        source_format="pdf",
        source_sha256=SOURCE_SHA,
        page=number,
        image_path=Path("shadow-fixtures") / f"page{number}.png",
        image_sha256=hashlib.sha256(f"page:{number}".encode()).hexdigest(),
        width=600,
        height=800,
    )


def _region(page: PageImageEvidence, kind: str, bbox: list[float], order: int) -> LayoutRegion:
    label = kind
    payload = {
        "doc_id": page.doc_id,
        "version_id": page.version_id,
        "page": page.page,
        "detector_id": DETECTOR_ID,
        "detector_revision": DETECTOR_REVISION,
        "label": label,
        "kind": kind,
        "bbox_px": [float(value) for value in bbox],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    region_id = (
        f"{page.doc_id}@{page.version_id}#ocr-page{page.page}#region-{digest[:32]}"
    )
    return LayoutRegion(
        region_id=region_id,
        page=page.page,
        label=label,
        kind=kind,
        score=0.9,
        bbox_px=[float(value) for value in bbox],
        order=order,
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
    )


def _layout(
    page: PageImageEvidence,
    regions: list[LayoutRegion],
    *,
    needs_review: bool | None = None,
    warnings: list[str] | None = None,
    rejected: list[dict] | None = None,
) -> LayoutPageResult:
    return LayoutPageResult(
        page=page,
        regions=regions,
        rejected_detections=rejected or [],
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
        warnings=warnings or [],
        needs_review=(not regions) if needs_review is None else needs_review,
    )


def _crop(page: PageImageEvidence, region: LayoutRegion) -> RegionCrop:
    left, top, right, bottom = (int(value) for value in region.bbox_px)
    return RegionCrop(
        page=page,
        region=region,
        crop_bbox_px=[left, top, right, bottom],
        image_path=Path("shadow-fixtures") / f"{region.region_id[-8:]}.png",
        image_sha256=hashlib.sha256(f"crop:{region.region_id}".encode()).hexdigest(),
        width=right - left,
        height=bottom - top,
        crop_to_page=[1.0, 0.0, float(left), 0.0, 1.0, float(top)],
    )


def _local(crop: RegionCrop, markdown: str, *, success: bool = True) -> OcrPageResult:
    page = crop.page
    return OcrPageResult(
        doc_id=page.doc_id,
        version_id=page.version_id,
        source_format=page.source_format,
        source_sha256=page.source_sha256,
        page=page.page,
        image_path=str(crop.image_path),
        image_sha256=crop.image_sha256,
        image_width=crop.width,
        image_height=crop.height,
        evidence=OcrPageEvidence(
            source_format=page.source_format,
            source_sha256=page.source_sha256,
            page=page.page,
            image_path=str(crop.image_path),
            image_sha256=crop.image_sha256,
            image_width=crop.width,
            image_height=crop.height,
        ),
        model="local-ocr",
        model_revision="local-r1",
        backend="test-backend",
        prompt_revision="p1",
        preprocess_revision="p0",
        postprocess_revision="p0",
        max_tokens=4096,
        markdown=markdown,
        success=success,
    )


def _result(
    crop: RegionCrop,
    *,
    markdown: str | None = None,
    tables: list[TableStructure] | None = None,
    success: bool = True,
    selected: str = "local",
    local: OcrPageResult | None = None,
    fallback: FallbackResult | None = None,
    errors: list[str] | None = None,
) -> RoutedRegionResult:
    if selected == "local" and local is None:
        local = _local(crop, markdown or "")
    return RoutedRegionResult(
        crop=crop,
        selected_source=selected,
        local_result=local,
        fallback_result=fallback,
        markdown=markdown,
        tables=tables or [],
        reason_codes=[],
        errors=errors or [],
        needs_review=True,
        success=success,
        usage=OcrUsage(local_requests=1 if selected == "local" else 0),
        historical_usage=None,
    )


def _vision(crop: RegionCrop, ordinal: int = 0) -> dict[str, object]:
    return {
        "bbox_unit": "page_pixels",
        "region_id": crop.region.region_id,
        "region_kind": crop.region.kind,
        "region_bbox_px": list(crop.region.bbox_px),
        "crop_bbox_px": list(crop.crop_bbox_px),
        "doc_id": crop.page.doc_id,
        "version_id": crop.page.version_id,
        "source_format": crop.page.source_format,
        "source_sha256": crop.page.source_sha256,
        "page_image_path": str(crop.page.image_path),
        "page_image_sha256": crop.page.image_sha256,
        "page": crop.page.page,
        "crop_image_path": str(crop.image_path),
        "crop_sha256": crop.image_sha256,
        "crop_to_page": list(crop.crop_to_page),
        "table_index_in_region": ordinal,
    }


def _table(
    crop: RegionCrop,
    *,
    headers: list[str],
    rows: list[list[str]],
    continuation_of: str | None = None,
) -> TableStructure:
    region = crop.region
    table_id = f"{region.region_id}#table1"
    table_rows: list[TableRow] = []
    for row_index, texts in enumerate([headers, *rows]):
        cells = [
            TableCell(
                cell_id=f"{table_id}#r{row_index}c{col}",
                row=row_index,
                col=col,
                origin_row=row_index,
                origin_col=col,
                text=text,
                is_header=row_index == 0,
            )
            for col, text in enumerate(texts)
        ]
        table_rows.append(
            TableRow(
                row_id=f"{table_id}#row{row_index}",
                row_index=row_index,
                is_header=row_index == 0,
                cells=cells,
                text=" | ".join(texts),
                locator=SourceLocator(format="pdf", page=region.page),
            )
        )
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=0,
        page=region.page,
        bbox=None,
        rows=table_rows,
        header_row_indices=[0],
        column_count=len(headers),
        row_count=len(table_rows),
        column_headers=list(headers),
        header_paths=[],
        kind="data",
        source="vision",
        locator=SourceLocator(format="pdf", page=region.page),
        vision=_vision(crop),
        continuation_of=continuation_of,
        header_source_table_id=continuation_of,
        header_source_page=region.page if continuation_of else None,
    )


def _text_articles(bundle: OcrShadowBundle) -> list:
    return [article for article in bundle.articles if article.block_kind not in TABLE_KINDS]


def test_multi_page_bundle_preserves_ids_evidence_and_authoritative_metadata() -> None:
    version = _version()
    page1, page2 = _page(1), _page(2)
    title = _region(page1, "title", [10, 10, 300, 50], 0)
    text1 = _region(page1, "text", [10, 60, 300, 140], 1)
    table1 = _region(page1, "table", [10, 150, 300, 260], 2)
    header2 = _region(page2, "header", [10, 10, 300, 30], 0)
    text2 = _region(page2, "text", [10, 40, 300, 160], 1)
    table2 = _region(page2, "table", [10, 170, 300, 280], 2)
    pages = [
        _layout(page1, [title, text1, table1]),
        _layout(page2, [header2, text2, table2]),
    ]
    crop1, crop2 = _crop(page1, table1), _crop(page2, table2)
    caller_table1 = _table(
        crop1,
        headers=["项目", "学号", "联系电话"],
        rows=[["学籍证明", "2023123456", "13800138000"]],
        continuation_of="seed#table0",
    )
    caller_table2 = _table(
        crop2,
        headers=["项目", "学号", "联系电话"],
        rows=[["成绩单", "2023123457", "13900139000"]],
    )
    results = [
        _result(_crop(page1, title), markdown="农学专业培养方案"),
        _result(_crop(page1, text1), markdown="第一条 为规范管理，制定本办法。"),
        _result(crop1, tables=[caller_table1]),
        _result(
            _crop(page2, text2),
            markdown=(
                "第二条 学生应当提交申请材料。联系人：张三，联系电话：13800138000，"
                "邮箱：zhangsan@example.com，学号：20231234。"
            ),
        ),
        _result(crop2, tables=[caller_table2]),
    ]

    bundle = build_shadow_bundle(version, pages, results, expected_pages=[1, 2])

    assert bundle.shadow is True
    assert bundle.publishable is False
    assert bundle.visually_verified is False
    assert bundle.trusted_parse is False
    assert bundle.release_target == "none"
    assert bundle.schema_version == "ocr-shadow/1"
    assert bundle.page_coverage_complete is True
    assert bundle.coverage_complete is True
    assert bundle.coverage_partial is False
    assert bundle.pending_regions == []
    assert [item["region_id"] for item in bundle.ignored_regions] == [header2.region_id]
    assert bundle.ignored_regions[0]["reason"] == "header_footer_excluded"

    parent_ids = {f"{DOC_ID}@{VERSION_ID}#table1", f"{DOC_ID}@{VERSION_ID}#table2"}
    row_ids = {f"{DOC_ID}@{VERSION_ID}#table1.row1", f"{DOC_ID}@{VERSION_ID}#table2.row1"}
    parents = {article.article_id: article for article in bundle.articles if article.block_kind == "table_parent"}
    rows = {article.article_id: article for article in bundle.articles if article.block_kind == "table_row"}
    assert set(parents) == parent_ids
    assert set(rows) == row_ids
    assert parent_ids.isdisjoint(row_ids)
    assert len({article.article_id for article in bundle.articles}) == len(bundle.articles)

    region_table_ids = {caller_table1.table_id, caller_table2.table_id}
    for article in [*parents.values(), *rows.values()]:
        assert article.table_id in region_table_ids
        assert article.parent_table_id == article.table_id
        assert article.bbox is None
        assert article.page in {1, 2}
        for cell in article.cell_provenance:
            assert cell.bbox is None
    assert all(cell.cell_id.startswith(article.table_id or "") for article in rows.values() for cell in article.cell_provenance)
    assert all(article.source_cell_ids for article in rows.values())

    parent_one = parents[f"{DOC_ID}@{VERSION_ID}#table1"]
    row_one = rows[f"{DOC_ID}@{VERSION_ID}#table1.row1"]
    parent_evidence = bundle.article_evidence[parent_one.article_id]
    row_evidence = bundle.article_evidence[row_one.article_id]
    assert parent_evidence["table"]["bbox"] is None
    assert parent_evidence["table"]["bbox_unit"] == "page_pixels"
    assert parent_evidence["table"]["table_index"] == 1
    assert parent_evidence["regions"][0]["page_image_sha256"] == page1.image_sha256
    assert parent_evidence["regions"][0]["crop_sha256"] == crop1.image_sha256
    assert parent_evidence["regions"][0]["selected_source"] == "local"
    assert parent_evidence["regions"][0]["detector_id"] == DETECTOR_ID
    assert parent_evidence["regions"][0]["local_model"] == "local-ocr"
    assert row_evidence["source_kind"] == "table_row"
    assert row_evidence["row"]["row_index"] == 1
    cell_ids = {cell.cell_id for cell in row_one.cell_provenance}
    assert set(row_evidence["cell_geometry"]) == cell_ids
    first_cell = row_one.cell_provenance[0]
    assert row_evidence["cell_geometry"][first_cell.cell_id][:2] == [1, 0]

    assert caller_table1.table_index == 0
    assert caller_table2.table_index == 0
    assert caller_table1.continuation_of == "seed#table0"
    assert caller_table1.header_source_table_id == "seed#table0"
    assert parent_one.table_continuation_of is None
    assert any("不继承表头" in warning for warning in bundle.warnings)

    assert parent_one.audience_detail is not None
    assert parent_one.audience_detail.scope_status is ScopeStatus.EXACT
    assert parent_one.audience_detail.class_types == ["dingying-class"]
    assert parent_one.audience_detail.intake_from == 2024
    assert parent_one.audience_detail.scope_evidence == "培养方案原文"

    text_articles = _text_articles(bundle)
    assert text_articles
    assert all(article.audience_detail == version.audience_detail for article in text_articles)
    for article in bundle.articles:
        assert article.relation is PolicyRelation.IMPLEMENTS
        assert (article.parent_doc_id, article.parent_version_id) == ("parent-doc", "parent-v1")
        assert article.effective_from == version.effective_from
        assert article.effective_to == version.effective_to
        assert article.date_source == version.date_source
        assert article.date_confidence == version.date_confidence
        assert article.audience == version.audience

    joined = "\n".join(article.text for article in bundle.articles)
    for secret in ("13800138000", "13900139000", "zhangsan@example.com", "20231234", "张三"):
        assert secret not in joined
    for placeholder in ("[手机号已脱敏]", "[个人邮箱已脱敏]", "[姓名已脱敏]", "[学号已脱敏]"):
        assert placeholder in joined
    assert bundle.redaction_counts.get("student_id", 0) > 0
    assert bundle.redaction_counts.get("mobile", 0) > 0

    dumped = json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False)
    for secret in ("13800138000", "13900139000", "zhangsan@example.com", "20231234", "张三"):
        assert secret not in dumped


def test_pending_and_ignored_regions_exclude_unverified_text() -> None:
    version = _version()
    page = _page(1)
    header = _region(page, "header", [10, 10, 300, 30], 0)
    text = _region(page, "text", [10, 40, 300, 100], 1)
    failed = _region(page, "text", [10, 110, 300, 170], 2)
    figure = _region(page, "figure", [10, 180, 300, 260], 3)
    unknown = _region(page, "unknown", [10, 270, 300, 320], 4)
    missing_table = _region(page, "table", [10, 330, 300, 430], 5)
    pages = [_layout(page, [header, text, failed, figure, unknown, missing_table])]
    failure = FallbackResult(
        markdown="云端失败输出",
        raw_response="RAW-FAILED",
        uncertain=True,
        model="cloud-ocr",
        model_revision="cloud-r1",
        prompt_revision="p1",
        errors=["timeout"],
    )
    results = [
        _result(_crop(page, header), markdown="页眉不应进入正文"),
        _result(_crop(page, text), markdown="第一条 正常内容。"),
        _result(
            _crop(page, failed),
            markdown="失败区域不应进入正文",
            selected="none",
            success=False,
            fallback=failure,
            errors=["timeout"],
        ),
        _result(_crop(page, figure), markdown="图片说明不应进入正文"),
        _result(_crop(page, unknown), markdown="未知区域不应进入正文"),
    ]

    bundle = build_shadow_bundle(version, pages, results, expected_pages=[1])

    pending = {item["region_id"]: item for item in bundle.pending_regions}
    ignored = {item["region_id"]: item for item in bundle.ignored_regions}
    assert set(pending) == {failed.region_id, figure.region_id, unknown.region_id, missing_table.region_id}
    assert pending[failed.region_id]["reason"] == "router_not_successful"
    assert pending[figure.region_id]["reason"] == "figure_region_not_indexed"
    assert pending[unknown.region_id]["reason"] == "unknown_region_not_indexed"
    assert pending[missing_table.region_id]["reason"] == "missing_router_result"
    assert set(ignored) == {header.region_id}
    assert ignored[header.region_id]["reason"] == "header_footer_excluded"
    assert bundle.page_coverage_complete is True
    assert bundle.coverage_complete is False
    assert bundle.coverage_partial is True

    detected = {header.region_id, text.region_id, failed.region_id, figure.region_id, unknown.region_id, missing_table.region_id}
    evidenced = {region_id for item in bundle.article_evidence.values() for region_id in item["region_ids"]}
    assert detected == evidenced | set(pending) | set(ignored)

    joined = "\n".join(article.text for article in bundle.articles)
    for forbidden in (
        "页眉不应进入正文",
        "失败区域不应进入正文",
        "图片说明不应进入正文",
        "未知区域不应进入正文",
        "云端失败输出",
        "RAW-FAILED",
    ):
        assert forbidden not in joined
    dumped = json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False)
    for forbidden in ("失败区域不应进入正文", "图片说明不应进入正文", "未知区域不应进入正文", "RAW-FAILED"):
        assert forbidden not in dumped

    failed_ref = bundle.result_refs[2]
    assert failed_ref["raw_result_ref"] == "results[2]"
    assert failed_ref["success"] is False
    assert failed_ref["errors"] == ["timeout"]
    assert failed_ref["fallback"]["uncertain"] is True
    assert failed_ref["fallback"]["markdown"]["chars"] == len("云端失败输出")
    assert failed_ref["fallback"]["raw_response"]["chars"] == len("RAW-FAILED")
    assert pending[failed.region_id]["result_ref"] == 2
    assert pending[failed.region_id]["raw_output_ref"] == "results[2]"


def _minimal_inputs() -> tuple[list[LayoutPageResult], list[RoutedRegionResult]]:
    page = _page(1)
    region = _region(page, "text", [10, 10, 300, 100], 0)
    return (
        [_layout(page, [region])],
        [_result(_crop(page, region), markdown="第一条 正常内容。")],
    )


def test_expected_pages_gate_full_coverage() -> None:
    version = _version()
    pages, results = _minimal_inputs()

    without_expected = build_shadow_bundle(version, pages, results)
    assert without_expected.expected_pages is None
    assert without_expected.page_coverage_complete is False
    assert without_expected.coverage_complete is False
    assert without_expected.coverage_partial is True
    assert any("expected_pages" in warning for warning in without_expected.warnings)

    missing = build_shadow_bundle(version, pages, results, expected_pages=[1, 2])
    assert missing.missing_pages == [2]
    assert missing.extra_pages == []
    assert missing.page_coverage_complete is False
    assert missing.coverage_complete is False
    assert missing.coverage_partial is True

    full = build_shadow_bundle(version, pages, results, expected_pages=[1])
    assert full.page_coverage_complete is True
    assert full.coverage_complete is True
    assert full.coverage_partial is False


def test_empty_layout_page_cannot_claim_full_completion() -> None:
    version = _version()
    page = _page(1)

    bundle = build_shadow_bundle(version, [_layout(page, [])], [], expected_pages=[1])

    assert bundle.articles == []
    assert bundle.pending_regions == []
    assert bundle.ignored_regions == []
    assert bundle.page_coverage_complete is True
    assert bundle.coverage_complete is False
    assert bundle.coverage_partial is True
    assert [item["page"] for item in bundle.pending_pages] == [1]
    assert bundle.pending_pages[0]["status"] == "pending"
    assert bundle.pending_pages[0]["pending_reasons"] == ["empty_layout"]
    assert bundle.page_audits[0]["needs_review"] is True
    assert bundle.page_audits[0]["region_count"] == 0
    assert any("页面级待处理" in warning for warning in bundle.warnings)


def test_only_header_footer_page_is_pending_even_when_regions_succeed() -> None:
    version = _version()
    page = _page(1)
    header = _region(page, "header", [10, 10, 300, 30], 0)
    footer = _region(page, "footer", [10, 380, 300, 400], 1)
    results = [
        _result(_crop(page, header), markdown="页眉内容"),
        _result(_crop(page, footer), markdown="页脚内容"),
    ]

    bundle = build_shadow_bundle(
        version, [_layout(page, [header, footer])], results, expected_pages=[1]
    )

    assert bundle.pending_regions == []
    assert {item["region_id"] for item in bundle.ignored_regions} == {
        header.region_id,
        footer.region_id,
    }
    assert bundle.page_coverage_complete is True
    assert bundle.coverage_complete is False
    assert bundle.coverage_partial is True
    assert bundle.pending_pages[0]["pending_reasons"] == ["only_header_footer"]
    assert bundle.page_audits[0]["ignored_region_count"] == 2


def test_review_flag_with_accepted_region_keeps_shadow_content_but_blocks_completion() -> None:
    version = _version()
    page = _page(1)
    text = _region(page, "text", [10, 10, 300, 100], 0)

    bundle = build_shadow_bundle(
        version,
        [_layout(page, [text], needs_review=True)],
        [_result(_crop(page, text), markdown="第一条 正常内容。")],
        expected_pages=[1],
    )

    articles = _text_articles(bundle)
    assert any(article.article_no == "第一条" for article in articles)
    assert any(article.text == "正常内容。" for article in articles)
    assert bundle.pending_regions == []
    assert bundle.page_coverage_complete is True
    assert bundle.coverage_complete is False
    assert bundle.coverage_partial is True
    assert bundle.pending_pages[0]["pending_reasons"] == ["layout_needs_review"]
    assert bundle.page_audits[0]["status"] == "pending"


def test_rejected_structural_detection_is_propagated_to_page_audit() -> None:
    version = _version()
    page = _page(1)
    text = _region(page, "text", [10, 10, 300, 100], 0)
    rejected = [
        {
            "index": 0,
            "reason": "invalid_detection",
            "error": "score: Input should be a valid number",
        },
        {
            "index": 1,
            "reason": "outside_page_bbox",
            "detection": {
                "label": "table",
                "score": 0.42,
                "bbox_px": [-5.0, 10.0, 120.0, 80.0],
                "order": 2,
            },
        },
        {
            "index": 2,
            "reason": "below_min_score",
            "detection": {
                "label": "text",
                "score": 0.01,
                "bbox_px": [10.0, 140.0, 300.0, 200.0],
                "order": 3,
            },
        },
    ]
    layout = _layout(
        page,
        [text],
        warnings=["layout_low_score_detection: index 2 score 0.010 < 0.300000"],
        rejected=rejected,
    )

    bundle = build_shadow_bundle(
        version,
        [layout],
        [_result(_crop(page, text), markdown="第一条 正常内容。")],
        expected_pages=[1],
    )

    audit = bundle.page_audits[0]
    assert audit["status"] == "pending"
    assert audit["pending_reasons"] == ["rejected_structural_detection"]
    assert audit["rejected_detection_count"] == 3
    assert audit["meaningful_rejected_count"] == 2
    assert audit["rejected_reasons"] == {
        "invalid_detection": 1,
        "outside_page_bbox": 1,
        "below_min_score": 1,
    }
    assert audit["rejected_detections"][0]["reason"] == "invalid_detection"
    assert audit["rejected_detections"][1]["reason"] == "outside_page_bbox"
    assert audit["rejected_detections"][2]["bbox_px"] == [10.0, 140.0, 300.0, 200.0]
    assert audit["rejected_detections"][2]["order"] == 3
    assert audit["warnings"] == ["layout_low_score_detection: index 2 score 0.010 < 0.300000"]
    assert audit["warning_count"] == 1
    assert bundle.pending_pages[0]["page"] == 1
    assert bundle.page_coverage_complete is True
    assert bundle.coverage_complete is False
    assert any("结构性 2 条" in warning for warning in bundle.warnings)
    articles = _text_articles(bundle)
    assert any(article.article_no == "第一条" for article in articles)
    assert any(article.text == "正常内容。" for article in articles)


def test_low_score_only_rejections_do_not_block_full_completion() -> None:
    version = _version()
    page = _page(1)
    text = _region(page, "text", [10, 10, 300, 100], 0)
    rejected = [
        {
            "index": 1,
            "reason": "below_min_score",
            "detection": {
                "label": "footer",
                "score": 0.0115,
                "bbox_px": [129.4, 545.1, 590.9, 598.6],
            },
        }
    ]

    bundle = build_shadow_bundle(
        version,
        [_layout(page, [text], rejected=rejected)],
        [_result(_crop(page, text), markdown="第一条 正常内容。")],
        expected_pages=[1],
    )

    assert bundle.pending_pages == []
    assert bundle.coverage_complete is True
    assert bundle.coverage_partial is False
    assert bundle.page_audits[0]["status"] == "accepted"
    assert bundle.page_audits[0]["rejected_detection_count"] == 1
    assert bundle.page_audits[0]["meaningful_rejected_count"] == 0
    assert bundle.page_audits[0]["rejected_detections"][0]["reason"] == "below_min_score"
    assert any("拒绝检测 1 条" in warning for warning in bundle.warnings)


def test_local_ocr_result_binds_crop_identity_not_parent_page_identity() -> None:
    version = _version()
    page = _page(1)
    region = _region(page, "text", [120, 260, 480, 380], 0)
    crop = _crop(page, region)
    assert (crop.width, crop.height) == (360, 120)
    assert (crop.width, crop.height) != (page.width, page.height)
    assert crop.image_sha256 != page.image_sha256

    local = _local(crop, "第一条 正常内容。")
    assert (local.image_path, local.image_sha256) == (
        str(crop.image_path),
        crop.image_sha256,
    )
    assert (local.image_width, local.image_height) == (crop.width, crop.height)
    assert (local.doc_id, local.version_id, local.source_sha256, local.page) == (
        page.doc_id,
        page.version_id,
        page.source_sha256,
        page.page,
    )
    assert local.evidence.image_sha256 == crop.image_sha256

    bundle = build_shadow_bundle(
        version,
        [_layout(page, [region])],
        [_result(crop, markdown=local.markdown, local=local)],
        expected_pages=[1],
    )
    assert bundle.coverage_complete is True
    assert bundle.article_evidence


def test_text_article_spanning_regions_combines_evidence_without_bbox() -> None:
    version = _version()
    page = _page(1)
    first = _region(page, "text", [10, 10, 300, 80], 0)
    second = _region(page, "text", [10, 90, 300, 160], 1)
    pages = [_layout(page, [first, second])]
    results = [
        _result(_crop(page, first), markdown="第一条 为规范管理，制定本办法。"),
        _result(_crop(page, second), markdown="学生应当提交申请材料。"),
    ]

    bundle = build_shadow_bundle(version, pages, results, expected_pages=[1])
    articles = _text_articles(bundle)
    assert len(articles) == 1
    evidence = bundle.article_evidence[articles[0].article_id]
    assert evidence["match"] == "combined_regions"
    assert set(evidence["region_ids"]) == {first.region_id, second.region_id}
    assert evidence["article_bbox"] is None
    assert evidence["char_offsets"] is None
    assert len(evidence["regions"]) == 2


def test_source_and_binding_mismatches_are_rejected() -> None:
    version = _version()
    page = _page(1)
    region = _region(page, "text", [10, 10, 300, 100], 0)
    layout = _layout(page, [region])
    crop = _crop(page, region)
    valid = _result(crop, markdown="第一条 正常内容。")

    bad_source = page.model_copy(update={"source_sha256": "d" * 64})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [_layout(bad_source, [])], [])

    bad_doc = page.model_copy(update={"doc_id": "other-doc"})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [_layout(bad_doc, [])], [])

    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout, layout], [])

    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid, valid])

    other_region = _region(page, "text", [400, 400, 500, 500], 99)
    tampered_crop = _result(
        _crop(page, other_region), markdown="第一条 正常内容。"
    )
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [tampered_crop])

    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid], expected_pages=[1, 1])

    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid], expected_pages=[0])

    bad_local = valid.local_result.model_copy(update={"image_sha256": "e" * 64})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"local_result": bad_local})])

    page_identity = {
        "image_path": str(page.image_path),
        "image_sha256": page.image_sha256,
        "image_width": page.width,
        "image_height": page.height,
    }
    swapped_result = valid.local_result.model_copy(update=page_identity)
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(
            version, [layout], [valid.model_copy(update={"local_result": swapped_result})]
        )

    swapped_evidence = valid.local_result.evidence.model_copy(update=page_identity)
    swapped_local = valid.local_result.model_copy(update={"evidence": swapped_evidence})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(
            version, [layout], [valid.model_copy(update={"local_result": swapped_local})]
        )


def test_tampered_table_ids_vision_and_boxes_are_rejected() -> None:
    version = _version()
    page = _page(1)
    region = _region(page, "table", [10, 10, 300, 200], 0)
    layout = _layout(page, [region])
    crop = _crop(page, region)
    table = _table(crop, headers=["项目", "学号"], rows=[["学籍证明", "2023123456"]])
    valid = _result(crop, tables=[table])

    bad_id = table.model_copy(update={"table_id": "evil-table"})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"tables": [bad_id]})])

    bad_vision = table.model_copy(update={"vision": {**_vision(crop), "region_id": "evil-region"}})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"tables": [bad_vision]})])

    swapped_vision = table.model_copy(
        update={"vision": {**_vision(crop), "crop_sha256": page.image_sha256}}
    )
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"tables": [swapped_vision]})])

    bad_bbox = table.model_copy(update={"bbox": [0.0, 0.0, 10.0, 10.0]})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"tables": [bad_bbox]})])

    row = table.rows[1].model_copy(update={"row_id": "evil-row"})
    bad_rows = table.model_copy(update={"rows": [table.rows[0], row]})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"tables": [bad_rows]})])

    first_cell = table.rows[1].cells[0].model_copy(update={"bbox": [0.0, 0.0, 5.0, 5.0]})
    bad_cells = table.rows[1].model_copy(update={"cells": [first_cell, *table.rows[1].cells[1:]]})
    bad_cell_table = table.model_copy(update={"rows": [table.rows[0], bad_cells]})
    with pytest.raises(ShadowValidationError):
        build_shadow_bundle(version, [layout], [valid.model_copy(update={"tables": [bad_cell_table]})])


def test_writer_manifest_and_schema_guard(tmp_path: Path) -> None:
    version = _version()
    pages, results = _minimal_inputs()
    bundle = build_shadow_bundle(version, pages, results, expected_pages=[1])
    out_dir = tmp_path / "shadow-out"

    paths = write_shadow_bundle(bundle, out_dir)
    assert set(paths) == {"articles", "evidence", "manifest"}
    assert {path.name for path in out_dir.iterdir()} == {
        "shadow_articles.jsonl",
        "shadow_evidence.json",
        "shadow_document.json",
    }

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["shadow"] is True
    assert manifest["publishable"] is False
    assert manifest["visually_verified"] is False
    assert manifest["trusted_parse"] is False
    assert manifest["status"] == "diagnostic_bridge"
    assert manifest["release_target"] == "none"
    assert manifest["counts"]["articles"] == len(bundle.articles)
    assert manifest["counts"]["pending_regions"] == 0
    assert manifest["counts"]["pending_pages"] == 0
    assert manifest["counts"]["page_audits"] == 1
    assert manifest["files"]["shadow_articles.jsonl"] == {
        "bytes": paths["articles"].stat().st_size,
        "sha256": hashlib.sha256(paths["articles"].read_bytes()).hexdigest(),
    }
    assert manifest["files"]["shadow_evidence.json"] == {
        "bytes": paths["evidence"].stat().st_size,
        "sha256": hashlib.sha256(paths["evidence"].read_bytes()).hexdigest(),
    }
    assert paths["articles"].read_text(encoding="utf-8").count("\n") == len(bundle.articles)

    with pytest.raises(ValidationError):
        OcrShadowBundle(**{**bundle.model_dump(mode="python"), "publishable": True})
    with pytest.raises(ValidationError):
        OcrShadowBundle(**{**bundle.model_dump(mode="python"), "release_target": "qdrant"})
    with pytest.raises(ShadowValidationError):
        write_shadow_bundle(bundle.model_copy(update={"publishable": True}), tmp_path / "tampered")
