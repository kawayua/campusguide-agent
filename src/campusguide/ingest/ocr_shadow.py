"""影子分块桥：把区域 OCR 路由结果映射为不可发布的 Article 记录。

只做诊断性桥接：输入是已完成的布局/路由结果和权威 ``DocumentVersion``，
输出是不可发布的影子 Bundle。不调用模型、不访问网络、不写生产解析目录、
不生成向量，也不把未核验 OCR 伪装成 ``vision_first``/``native_verified``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from campusguide.domain.models import Article, DocumentVersion
from campusguide.domain.text import count_cjk, normalize_text
from campusguide.ingest.ocr_layout import LayoutPageResult, LayoutRegion, RegionCrop
from campusguide.ingest.ocr_router import RoutedRegionResult
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.redact import redact_text
from campusguide.ingest.segment import segment_document
from campusguide.ingest.structure import (
    PageStructure,
    ParsedStructure,
    SourceLocator,
    StructureBlock,
    TableStructure,
)

SHADOW_SCHEMA_VERSION = "ocr-shadow/1"
SHADOW_PARSER_VERSION = "ocr-shadow/1"
SHADOW_BRIDGE_VERSION = "ocr-shadow-bridge/1"
SHADOW_NOTE = (
    "诊断性影子分块：不是可信生产解析，publishable=False、visually_verified=False，"
    "不得直接发布或写入生产索引"
)
_IGNORED_KINDS = frozenset({"header", "footer"})
_PENDING_KINDS = frozenset({"figure", "unknown"})
_MEANINGFUL_REJECTIONS = frozenset(
    {"invalid_detection", "reversed_or_zero_bbox", "outside_page_bbox"}
)
_TABLE_ARTICLE_KINDS = frozenset({"table_parent", "table_row", "matrix_row"})
_MATCH_MIN_CHARS = 40
_ARTICLE_PREFIX_RE = re.compile(
    r"^第[一二三四五六七八九十百千零〇两0-9]{1,12}条(?:之[一二三四五六七八九十0-9]{1,3})?[\s：:、.．]*"
)
_VERSION_FIELDS = (
    "doc_id",
    "version_id",
    "doc_type",
    "authority_level",
    "issuer_level",
    "issuer_college_id",
    "parent_doc_id",
    "parent_version_id",
    "relation",
    "issuing_department",
    "published_at",
    "effective_from",
    "effective_to",
    "status",
    "source_url",
    "date_confidence",
    "date_source",
    "audience",
)


class ShadowValidationError(ValueError):
    """页面、区域、路由结果或表格证据之间的绑定不满足影子桥约束。"""


class OcrShadowBundle(BaseModel):
    """不可发布的影子输出；schema 层面禁止可发布、已核验和直接发布。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ocr-shadow/1"] = SHADOW_SCHEMA_VERSION
    bridge_version: str = SHADOW_BRIDGE_VERSION
    parser_version: Literal["ocr-shadow/1"] = SHADOW_PARSER_VERSION
    status: Literal["diagnostic_bridge"] = "diagnostic_bridge"
    diagnostic_only: Literal[True] = True
    trusted_parse: Literal[False] = False
    shadow: Literal[True] = True
    publishable: Literal[False] = False
    visually_verified: Literal[False] = False
    release_target: Literal["none"] = "none"

    version: DocumentVersion
    articles: list[Article]
    article_evidence: dict[str, dict[str, Any]]
    pending_regions: list[dict[str, Any]]
    pending_pages: list[dict[str, Any]]
    page_audits: list[dict[str, Any]]
    ignored_regions: list[dict[str, Any]]
    result_refs: list[dict[str, Any]]
    warnings: list[str]
    redaction_counts: dict[str, int]
    source_sha256: str
    source_image_hashes: dict[int, str]
    pages_seen: list[int]
    expected_pages: list[int] | None
    missing_pages: list[int]
    extra_pages: list[int]
    page_coverage_complete: bool
    coverage_partial: bool
    coverage_complete: bool
    page_count: int

    @model_validator(mode="after")
    def _shadow_guards(self) -> OcrShadowBundle:
        flags = (
            self.shadow,
            self.publishable,
            self.visually_verified,
            self.trusted_parse,
            self.diagnostic_only,
            self.release_target,
        )
        if flags != (True, False, False, False, True, "none"):
            raise ValueError("影子 Bundle 不能声明可发布、已核验或直接发布")
        if self.version.source_sha256 != self.source_sha256:
            raise ValueError("Bundle source_sha256 与 DocumentVersion 不一致")
        if self.page_count != len(self.pages_seen) or len(set(self.pages_seen)) != len(
            self.pages_seen
        ):
            raise ValueError("page_count/pages_seen 不一致或存在重复页")
        if set(self.source_image_hashes) != set(self.pages_seen):
            raise ValueError("source_image_hashes 必须覆盖且仅覆盖 pages_seen")
        audit_pages = [item.get("page") for item in self.page_audits]
        if (
            sorted(audit_pages) != self.pages_seen
            or len(set(audit_pages)) != len(audit_pages)
        ):
            raise ValueError("page_audits 必须覆盖且仅覆盖 pages_seen")
        expected_pending = {
            item.get("page")
            for item in self.page_audits
            if item.get("pending_reasons")
        }
        if {item.get("page") for item in self.pending_pages} != expected_pending:
            raise ValueError("pending_pages 必须与 page_audits 中的待处理页一致")
        if any(
            item.get("status") != "pending" or not item.get("pending_reasons")
            for item in self.pending_pages
        ):
            raise ValueError("pending_pages 必须携带 pending 状态和待处理原因")
        if set(self.article_evidence) != {item.article_id for item in self.articles}:
            raise ValueError("article_evidence 必须覆盖且仅覆盖 articles 中的 article_id")
        for article in self.articles:
            if article.doc_title != self.version.title:
                raise ValueError(f"{article.article_id} 的 doc_title 与版本不一致")
            for name in _VERSION_FIELDS:
                if getattr(article, name) != getattr(self.version, name):
                    raise ValueError(f"{article.article_id} 的 {name} 未保持版本权威值")
            if article.source_sha256 != self.source_sha256:
                raise ValueError(f"{article.article_id} 的 source_sha256 不一致")
        for key, evidence in self.article_evidence.items():
            if (
                evidence.get("article_id") != key
                or evidence.get("article_bbox") is not None
                or evidence.get("char_offsets") is not None
            ):
                raise ValueError(f"article_evidence[{key}] 不能伪造单一 bbox 或字符偏移")
        if self.coverage_complete == self.coverage_partial:
            raise ValueError("coverage_complete 与 coverage_partial 必须互斥")
        if self.page_coverage_complete and (
            self.expected_pages is None or self.missing_pages or self.extra_pages
        ):
            raise ValueError("页覆盖不完整时不能声明 page_coverage_complete")
        if self.coverage_complete and (
            not self.page_coverage_complete
            or self.pending_regions
            or self.pending_pages
        ):
            raise ValueError("存在待处理区域/页面或页缺失时不能声明全量完成")
        return self


@dataclass
class _TextBlock:
    block_id: str
    text: str
    normalized: str
    redacted_normalized: str
    region: LayoutRegion
    result: RoutedRegionResult
    result_ref: int


@dataclass
class _TableRecord:
    table: TableStructure
    region: LayoutRegion
    result: RoutedRegionResult
    result_ref: int


@dataclass
class _Planned:
    region: LayoutRegion
    result: RoutedRegionResult | None
    result_ref: int | None
    action: Literal["content", "pending", "ignored"]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ShadowValidationError(message)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value: str | None) -> dict[str, Any] | None:
    return None if value is None else {"chars": len(value), "sha256": _sha256(value)}


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _summary(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]


def _page_set(value: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ShadowValidationError("expected_pages 必须是整数列表或 None")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value):
        raise ShadowValidationError("expected_pages 只能包含正整数")
    if len(set(value)) != len(value):
        raise ShadowValidationError("expected_pages 不能包含重复页")
    return sorted(value)


def _check_pages(
    version: DocumentVersion, pages: list[LayoutPageResult]
) -> tuple[list[LayoutPageResult], str]:
    _require(isinstance(pages, list) and bool(pages), "pages 必须是非空 LayoutPageResult 列表")
    checked: list[LayoutPageResult] = []
    numbers: set[int] = set()
    region_ids: set[str] = set()
    source_format: str | None = None
    for item in pages:
        _require(isinstance(item, LayoutPageResult), "pages 只能包含 LayoutPageResult")
        try:
            page = LayoutPageResult.model_validate(item.model_dump(mode="python"))
        except Exception as exc:
            raise ShadowValidationError(f"页面结果重新校验失败: {_summary(exc)}") from exc
        evidence = page.page
        _require(
            (evidence.doc_id, evidence.version_id) == (version.doc_id, version.version_id),
            "页面 doc_id/version_id 与 DocumentVersion 不一致",
        )
        _require(evidence.source_sha256 == version.source_sha256, "页面 source_sha256 与版本不一致")
        _require(source_format in (None, evidence.source_format), "同一文档的页面 source_format 不一致")
        source_format = source_format or evidence.source_format
        _require(evidence.page not in numbers, f"重复的页码: {evidence.page}")
        numbers.add(evidence.page)
        for region in page.regions:
            _require(region.region_id not in region_ids, f"跨页重复的 region_id: {region.region_id}")
            region_ids.add(region.region_id)
        checked.append(page)
    return checked, source_format or "pdf"


def _check_local(local: Any, crop: RegionCrop) -> None:
    page = crop.page
    parent_fields = (
        ("doc_id", page.doc_id, False),
        ("version_id", page.version_id, False),
        ("source_format", page.source_format, True),
        ("source_sha256", page.source_sha256, True),
        ("page", page.page, True),
    )
    # 路由器把区域裁剪图交给本地 OCR：图像身份属于 crop，文档身份属于父页面。
    # 适配器会把裁剪路径解析为绝对路径，因此路径按 Path 同一性比较；
    # 字节身份仍由 SHA-256 与宽高严格绑定。
    _require(
        Path(local.image_path) == crop.image_path,
        "本地 OCR 结果的 image_path 与区域裁剪不一致",
    )
    _require(
        Path(local.evidence.image_path) == crop.image_path,
        "本地 OCR evidence.image_path 与区域裁剪不一致",
    )
    for name, value, in_evidence in parent_fields:
        _require(getattr(local, name) == value, f"本地 OCR 结果的 {name} 与父页面不一致")
        if in_evidence:
            _require(
                getattr(local.evidence, name) == value,
                f"本地 OCR evidence.{name} 与父页面不一致",
            )
    crop_fields = (
        ("image_sha256", crop.image_sha256),
        ("image_width", crop.width),
        ("image_height", crop.height),
    )
    for name, value in crop_fields:
        _require(getattr(local, name) == value, f"本地 OCR 结果的 {name} 与区域裁剪不一致")
        _require(
            getattr(local.evidence, name) == value,
            f"本地 OCR evidence.{name} 与区域裁剪不一致",
        )


def _expected_vision(crop: RegionCrop) -> dict[str, Any]:
    page, region = crop.page, crop.region
    return {
        "bbox_unit": "page_pixels", "region_id": region.region_id, "region_kind": region.kind,
        "region_bbox_px": list(region.bbox_px), "crop_bbox_px": list(crop.crop_bbox_px),
        "doc_id": page.doc_id, "version_id": page.version_id, "source_format": page.source_format,
        "source_sha256": page.source_sha256, "page_image_path": str(page.image_path),
        "page_image_sha256": page.image_sha256, "page": page.page,
        "crop_image_path": str(crop.image_path), "crop_sha256": crop.image_sha256,
        "crop_to_page": list(crop.crop_to_page),
    }


def _check_result_tables(result: RoutedRegionResult, crop: RegionCrop) -> None:
    _require(not (result.selected_source != "none" and not result.success), "非 none 的 selected_source 必须对应 success=True")
    _require(not (result.success and result.selected_source == "none"), "success=True 不能没有 selected_source")
    if result.selected_source == "local":
        _require(result.local_result is not None and result.local_result.success, "selected_source=local 缺少成功的本地结果")
    if result.selected_source == "gemini":
        fallback = result.fallback_result
        _require(
            fallback is not None and not fallback.uncertain and not fallback.errors,
            "selected_source=gemini 缺少确定且无错误的回退结果",
        )
    if result.local_result is not None:
        _check_local(result.local_result, crop)
    region, expected = crop.region, _expected_vision(crop)
    x0, y0, x1, y1 = region.bbox_px
    _require(
        0 <= x0 < x1 <= crop.page.width and 0 <= y0 < y1 <= crop.page.height,
        "区域 bbox_px 超出父页面",
    )
    table_ids: set[str] = set()
    row_ids: set[str] = set()
    cell_ids: set[str] = set()
    for ordinal, table in enumerate(result.tables, start=1):
        table_id = f"{region.region_id}#table{ordinal}"
        _require(table.table_id == table_id and table.block_id == f"{table_id}#block", f"表格 ID 未绑定实际区域: {table.table_id}")
        _require(table_id not in table_ids, f"重复的 table_id: {table_id}")
        table_ids.add(table_id)
        _require(table.page == region.page, f"表格 {table_id} 页码与区域不一致")
        _require(table.locator.page in (None, region.page), f"表格 {table_id} locator 页码不一致")
        _require(table.bbox is None and table.locator.bbox is None, f"表格 {table_id} 不能携带物理 bbox")
        _require(isinstance(table.vision, dict), f"表格 {table_id} 缺少路由 vision 证据")
        _require(
            all(table.vision.get(key) == value for key, value in expected.items()),
            f"表格 {table_id} vision 与区域证据不一致",
        )
        _require(table.vision.get("table_index_in_region") == ordinal - 1, f"表格 {table_id} 区域序号被篡改")
        for row in table.rows:
            _require(
                row.row_id == f"{table_id}#row{row.row_index}" and row.row_id not in row_ids,
                f"行 ID 未绑定实际表格或重复: {row.row_id}",
            )
            row_ids.add(row.row_id)
            _require(row.locator.bbox is None, f"行 {row.row_id} 不能携带物理 bbox")
            for cell in row.cells:
                _require(
                    cell.cell_id == f"{table_id}#r{cell.row}c{cell.col}" and cell.cell_id not in cell_ids,
                    f"单元格 ID 未绑定实际表格或重复: {cell.cell_id}",
                )
                cell_ids.add(cell.cell_id)
                _require(cell.bbox is None, f"单元格 {cell.cell_id} 不能携带物理 bbox")


def _check_results(
    results: list[RoutedRegionResult],
    pages: dict[int, LayoutPageResult],
    regions: dict[str, LayoutRegion],
) -> list[RoutedRegionResult]:
    _require(isinstance(results, list), "results 必须是 RoutedRegionResult 列表")
    checked: list[RoutedRegionResult] = []
    seen: set[str] = set()
    for index, item in enumerate(results):
        _require(isinstance(item, RoutedRegionResult), "results 只能包含 RoutedRegionResult")
        try:
            result = RoutedRegionResult.model_validate(item.model_dump(mode="python"))
        except Exception as exc:
            raise ShadowValidationError(f"results[{index}] 重新校验失败: {_summary(exc)}") from exc
        crop = result.crop
        page = pages.get(crop.page.page)
        _require(
            page is not None and crop.page.model_dump(mode="python") == page.page.model_dump(mode="python"),
            f"results[{index}] 的页面证据被篡改或未提供",
        )
        region = regions.get(crop.region.region_id)
        _require(
            region is not None and crop.region.model_dump(mode="python") == region.model_dump(mode="python"),
            f"results[{index}] 的区域证据被篡改或未检测到",
        )
        _require(region.region_id not in seen, f"同一区域存在重复结果: {region.region_id}")
        seen.add(region.region_id)
        _check_result_tables(result, crop)
        checked.append(result)
    return checked


def _usable(tables: list[TableStructure]) -> list[TableStructure]:
    return [table for table in tables if table.kind != "layout" and table.rows]


def _redacted_error(value: str) -> str:
    return redact_text(value, redact_contacts=True).text


def _audit(
    region: LayoutRegion,
    result: RoutedRegionResult | None,
    result_ref: int | None,
    status: Literal["pending", "ignored"],
    reason: str,
) -> dict[str, Any]:
    return {
        "status": status, "reason": reason, "region_id": region.region_id,
        "page": region.page, "kind": region.kind, "label": region.label,
        "order": region.order, "bbox_px": list(region.bbox_px), "result_ref": result_ref,
        "raw_output_ref": f"results[{result_ref}]" if result_ref is not None else None,
        "selected_source": result.selected_source if result else None,
        "success": result.success if result else None,
        "reason_codes": list(result.reason_codes) if result else [],
        "errors": [_redacted_error(item) for item in result.errors] if result else [],
    }


def _plan(
    pages: list[LayoutPageResult],
    by_region: dict[str, RoutedRegionResult],
    indices: dict[str, int],
    warnings: list[str],
) -> tuple[list[_Planned], list[dict[str, Any]], list[dict[str, Any]]]:
    plans: list[_Planned] = []
    pending: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for page in sorted(pages, key=lambda item: item.page.page):
        for region in page.regions:
            result, result_ref = by_region.get(region.region_id), indices.get(region.region_id)
            action: Literal["content", "pending", "ignored"] = "pending"
            reason = ""
            if region.kind in _IGNORED_KINDS:
                action, reason = "ignored", "header_footer_excluded"
            elif result is None:
                reason = (
                    f"{region.kind}_region_not_indexed"
                    if region.kind in _PENDING_KINDS
                    else "missing_router_result"
                )
            elif region.kind in _PENDING_KINDS:
                reason = f"{region.kind}_region_not_indexed"
            elif not result.success or result.selected_source == "none":
                reason = "router_not_successful"
            elif region.kind == "table":
                action = "content" if _usable(result.tables) else "pending"
                reason = "" if action == "content" else "table_region_without_usable_tables"
            elif (result.markdown or "").strip():
                action = "content"
            else:
                reason = "non_table_region_without_text"
            if result is not None and region.kind not in _IGNORED_KINDS | {"table"} and result.tables:
                warnings.append(
                    f"区域 {region.region_id} 非表格类但返回 {len(result.tables)} 个表格，"
                    "影子桥未索引这些表格，避免正文重复"
                )
            if action == "pending":
                pending.append(_audit(region, result, result_ref, "pending", reason))
            elif action == "ignored":
                if result is not None and ((result.markdown or "").strip() or result.tables):
                    warnings.append(f"页眉/页脚区域 {region.region_id} 有路由输出，按规则排除出可检索正文")
                ignored.append(_audit(region, result, result_ref, "ignored", reason))
            plans.append(_Planned(region, result, result_ref, action))
    return plans, pending, ignored


def _meaningful_rejection(item: Any) -> bool:
    return isinstance(item, dict) and item.get("reason") in _MEANINGFUL_REJECTIONS


def _safe_audit_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _rejected_summary(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"reason": "invalid_rejected_audit_entry"}
    detection = item.get("detection")
    summary = {
        "index": _safe_audit_value(item.get("index")),
        "reason": _safe_audit_value(item.get("reason")),
        "region_id": _safe_audit_value(item.get("region_id")),
    }
    if isinstance(detection, dict):
        for key in ("label", "score", "bbox_px", "order"):
            value = detection.get(key)
            if isinstance(value, (str, int, float)):
                summary[key] = value
            elif isinstance(value, list) and all(
                isinstance(entry, (int, float)) and not isinstance(entry, bool)
                for entry in value
            ):
                summary[key] = list(value)
    return summary


def _page_audit(
    page: LayoutPageResult,
    plans: list[_Planned],
    warnings: list[str],
) -> dict[str, Any]:
    number = page.page.page
    actions = {
        plan.region.region_id: plan.action
        for plan in plans
        if plan.region.page == number
    }
    ignored = [region for region in page.regions if region.kind in _IGNORED_KINDS]
    region_warnings = [
        f"{region.region_id}: {warning}"
        for region in page.regions
        for warning in region.warnings
    ]
    rejected = [_rejected_summary(item) for item in page.rejected_detections]
    meaningful = [
        item for item in page.rejected_detections if _meaningful_rejection(item)
    ]
    rejected_reasons: dict[str, int] = {}
    for summary in rejected:
        key = str(summary.get("reason"))
        rejected_reasons[key] = rejected_reasons.get(key, 0) + 1
    reasons: list[str] = []
    if not page.regions:
        reasons.append("empty_layout")
    elif len(ignored) == len(page.regions):
        reasons.append("only_header_footer")
    elif page.needs_review:
        reasons.append("layout_needs_review")
    if meaningful:
        reasons.append("rejected_structural_detection")
    if page.warnings:
        warnings.append(f"第{number}页布局警告 {len(page.warnings)} 条，详见 page_audits")
    if region_warnings:
        warnings.append(f"第{number}页区域警告 {len(region_warnings)} 条，详见 page_audits")
    if rejected:
        warnings.append(
            f"第{number}页拒绝检测 {len(rejected)} 条"
            f"（结构性 {len(meaningful)} 条），详见 page_audits"
        )
    for reason in reasons:
        warnings.append(f"第{number}页页面级待处理：{reason}")
    return {
        "page": number,
        "detector_id": page.detector_id,
        "detector_revision": page.detector_revision,
        "status": "pending" if reasons else "accepted",
        "pending_reasons": reasons,
        "reason": ",".join(reasons) if reasons else None,
        "needs_review": page.needs_review,
        "region_count": len(page.regions),
        "content_region_count": sum(action == "content" for action in actions.values()),
        "pending_region_count": sum(action == "pending" for action in actions.values()),
        "ignored_region_count": len(ignored),
        "warnings": list(page.warnings),
        "warning_count": len(page.warnings),
        "region_warnings": region_warnings,
        "rejected_detection_count": len(rejected),
        "meaningful_rejected_count": len(meaningful),
        "rejected_reasons": rejected_reasons,
        "rejected_detections": rejected,
    }


def _clear_header_chain(table: TableStructure, warnings: list[str]) -> None:
    if (
        table.continuation_of is not None
        or table.header_source_table_id is not None
        or table.header_source_page is not None
    ):
        warnings.append(f"表格 {table.table_id} 的未核验续表头来源已清空，不继承表头")
        table.continuation_of = None
        table.header_source_table_id = None
        table.header_source_page = None


def _content(
    plans: list[_Planned], warnings: list[str], source_format: str
) -> tuple[str, list[_TextBlock], list[_TableRecord], list[StructureBlock]]:
    parts: list[str] = []
    cursor = 0
    text_blocks: list[_TextBlock] = []
    records: list[_TableRecord] = []
    blocks: list[StructureBlock] = []
    table_index = 0

    def append(content: str) -> tuple[int, int]:
        nonlocal cursor
        if parts:
            parts.append("\n\n")
            cursor += 2
        start = cursor
        parts.append(content)
        cursor += len(content)
        return start, cursor

    for plan in plans:
        if plan.action != "content" or plan.result is None or plan.result_ref is None:
            continue
        result, region = plan.result, plan.region
        if region.kind == "table":
            usable = _usable(result.tables)
            if len(usable) != len(result.tables):
                warnings.append(
                    f"区域 {region.region_id} 有 {len(result.tables) - len(usable)} 个"
                    "空表或 layout 表未进入影子结构"
                )
            for candidate in usable:
                table = candidate.model_copy(deep=True)
                table_index += 1
                table.table_index = table_index
                table.column_headers = [redact_text(value, redact_contacts=True).text for value in table.column_headers]
                table.header_paths = [
                    [redact_text(value, redact_contacts=True).text for value in path]
                    for path in table.header_paths
                ]
                _clear_header_chain(table, warnings)
                if not table.column_headers:
                    warnings.append(f"表格 {table.table_id} 未识别到列头，行块不补造列名")
                marker = f"【影子表格占位：{table.table_id}】"
                start, end = append(marker)
                blocks.append(
                    StructureBlock(
                        block_id=table.block_id,
                        order=len(blocks),
                        kind="table",
                        text=marker,
                        locator=SourceLocator(
                            format=source_format,
                            page=region.page,
                            bbox=None,
                            char_start=start,
                            char_end=end,
                        ),
                        table_id=table.table_id,
                    )
                )
                records.append(_TableRecord(table, region, result, plan.result_ref))
            continue
        markdown = (result.markdown or "").strip()
        if not markdown:
            continue
        start, end = append(markdown)
        block_id = f"{region.region_id}#{region.kind}"
        blocks.append(
            StructureBlock(
                block_id=block_id,
                order=len(blocks),
                kind="heading" if region.kind == "title" else "paragraph",
                text=markdown,
                locator=SourceLocator(
                    format=source_format,
                    page=region.page,
                    bbox=None,
                    char_start=start,
                    char_end=end,
                ),
            )
        )
        text_blocks.append(
            _TextBlock(
                block_id,
                markdown,
                normalize_text(markdown),
                normalize_text(redact_text(markdown, redact_contacts=True).text),
                region,
                result,
                plan.result_ref,
            )
        )
    return "".join(parts), text_blocks, records, blocks


def _parsed(
    version: DocumentVersion,
    pages: list[LayoutPageResult],
    source_format: str,
    text: str,
    text_blocks: list[_TextBlock],
    records: list[_TableRecord],
    blocks: list[StructureBlock],
    coverage: dict[str, Any],
    warnings: list[str],
) -> ParsedDocument:
    page_chars: dict[int, int] = {}
    for block in text_blocks:
        page_chars[block.region.page] = page_chars.get(block.region.page, 0) + len(block.text)
    for record in records:
        marker_chars = len(record.table.table_id) + len("【影子表格占位：】")
        page_chars[record.region.page] = page_chars.get(record.region.page, 0) + marker_chars
    pages_meta = [
        PageStructure(
            page=page.page.page, width=float(page.page.width), height=float(page.page.height),
            text_chars=page_chars.get(page.page.page, 0), has_text_layer=False, needs_ocr=True,
            ocr_available=True, rendered=True, render_sha256=page.page.image_sha256,
            vision_status="shadow_diagnostic", warnings=list(page.warnings),
        )
        for page in sorted(pages, key=lambda item: item.page.page)
    ]
    tables = [record.table for record in records]
    structure = ParsedStructure(
        extractor_version=SHADOW_PARSER_VERSION, format=source_format, blocks=blocks,
        tables=tables, pages=pages_meta, reading_order=[block.block_id for block in blocks],
        warnings=list(warnings), coverage=coverage,
        vision={
            "shadow": True, "status": "diagnostic_bridge", "parser_version": SHADOW_PARSER_VERSION,
            "publishable": False, "visually_verified": False,
        },
    )
    return ParsedDocument(
        doc_id=version.doc_id, version_id=version.version_id, title=version.title,
        text=text, format=source_format, source_sha256=version.source_sha256,
        source_url=str(version.source_url), local_path=None,
        fetched_at=version.fetched_at.isoformat(), parser_version=SHADOW_PARSER_VERSION,
        warnings=list(warnings), tables=[table.table_id for table in tables],
        page_count=len(pages), cjk_count=count_cjk(text), char_count=len(text),
        ocr_used=True, ocr_confidence=None, structure=structure,
        vision={
            "shadow": True, "status": "diagnostic_bridge", "publishable": False,
            "visually_verified": False,
            "ingestion_mode_note": (
                "ingestion_mode=legacy 仅因现有枚举没有 shadow；本 ParsedDocument "
                "只存在于内存，禁止写入生产 parsed 目录"
            ),
        },
        ingestion_mode="legacy",
    )


def _source(result: RoutedRegionResult, region: LayoutRegion, result_ref: int, match: str) -> dict[str, Any]:
    crop, page = result.crop, result.crop.page
    local, fallback = result.local_result, result.fallback_result
    return {
        "result_ref": result_ref, "region_id": region.region_id, "page": region.page,
        "kind": region.kind, "label": region.label, "order": region.order,
        "region_bbox_px": list(region.bbox_px), "detector_id": region.detector_id,
        "detector_revision": region.detector_revision, "source_format": page.source_format,
        "source_sha256": page.source_sha256, "page_image_path": str(page.image_path),
        "page_image_sha256": page.image_sha256, "crop_image_path": str(crop.image_path),
        "crop_sha256": crop.image_sha256, "crop_bbox_px": list(crop.crop_bbox_px),
        "crop_to_page": list(crop.crop_to_page), "selected_source": result.selected_source,
        "needs_review": result.needs_review, "local_model": local.model if local else None,
        "local_model_revision": local.model_revision if local else None,
        "fallback_model": fallback.model if fallback else None,
        "fallback_model_revision": fallback.model_revision if fallback else None, "match": match,
    }


def _result_ref(index: int, result: RoutedRegionResult) -> dict[str, Any]:
    local, fallback = result.local_result, result.fallback_result
    return {
        "result_index": index, "raw_result_ref": f"results[{index}]",
        "region_id": result.crop.region.region_id, "page": result.crop.page.page,
        "selected_source": result.selected_source, "success": result.success,
        "needs_review": result.needs_review, "reason_codes": list(result.reason_codes),
        "errors": [_redacted_error(item) for item in result.errors], "markdown": _digest(result.markdown),
        "table_ids": [table.table_id for table in result.tables],
        "local": None if local is None else {
            "model": local.model, "model_revision": local.model_revision,
            "success": local.success, "errors": [_redacted_error(item) for item in local.errors],
            "raw_response": _digest(local.raw_response), "markdown": _digest(local.markdown),
        },
        "fallback": None if fallback is None else {
            "model": fallback.model, "model_revision": fallback.model_revision,
            "uncertain": fallback.uncertain, "errors": [_redacted_error(item) for item in fallback.errors],
            "raw_response": _digest(fallback.raw_response), "markdown": _digest(fallback.markdown),
        },
    }


def _match(article_norm: str, block: _TextBlock) -> str | None:
    if not block.normalized or not article_norm:
        return None
    if any(candidate and candidate in article_norm for candidate in (block.normalized, block.redacted_normalized)):
        return "full"
    candidate = block.redacted_normalized or block.normalized
    if article_norm in candidate:
        return "partial"
    stripped = _ARTICLE_PREFIX_RE.sub("", candidate)
    if stripped and stripped in article_norm:
        return "partial"
    if len(candidate) >= _MATCH_MIN_CHARS and (
        candidate[:_MATCH_MIN_CHARS] in article_norm or candidate[-_MATCH_MIN_CHARS:] in article_norm
    ):
        return "partial"
    return None


def _text_detail(article: Article, blocks: list[_TextBlock], cursor: int) -> tuple[dict[str, Any], int]:
    article_norm = article.text_normalized or normalize_text(article.text)
    matches: list[tuple[_TextBlock, str]] = []
    full: list[int] = []
    partial: list[int] = []
    for index in range(cursor, len(blocks)):
        match = _match(article_norm, blocks[index])
        if match is None:
            continue
        matches.append((blocks[index], match))
        (full if match == "full" else partial).append(index)
    next_cursor = max(full) + 1 if full else min(partial) if partial else cursor
    sources = [_source(block.result, block.region, block.result_ref, match) for block, match in matches]
    return (
        {
            "article_id": article.article_id,
            "part_no": article.part_no,
            "block_kind": article.block_kind,
            "indexable": article.is_indexable,
            "source_kind": "text",
            "match": "unmatched" if not sources else "single_region",
            "article_bbox": None,
            "char_offsets": None,
            "regions": sources,
            "region_ids": [source["region_id"] for source in sources],
            "result_refs": sorted({source["result_ref"] for source in sources}),
            "table": None,
            "row": None,
            "cell_geometry": {},
            "source_cell_ids": [],
            "warnings": [] if sources else ["无法回源到 OCR 区块，未伪造几何或字符偏移"],
            "text_sha256": _sha256(article.text),
        },
        next_cursor,
    )


def _redacted_label(value: str) -> str:
    return redact_text(value, redact_contacts=True).text


def _table_detail(article: Article, tables: dict[str, _TableRecord]) -> dict[str, Any]:
    record = tables.get(article.table_id or "")
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    table_payload: dict[str, Any] | None = None
    if record is None:
        warnings.append("表格文章缺少可追溯的 OCR 表格记录")
    else:
        sources.append(_source(record.result, record.region, record.result_ref, "table"))
        table, vision = record.table, record.table.vision or {}
        table_payload = {
            "table_id": table.table_id, "table_index": table.table_index,
            "region_id": record.region.region_id, "page": table.page, "source": table.source,
            "kind": table.kind, "row_count": table.row_count, "column_count": table.column_count,
            "header_row_indices": list(table.header_row_indices),
            "column_headers": [_redacted_label(value) for value in table.column_headers],
            "header_paths": [[_redacted_label(value) for value in path] for path in table.header_paths],
            "continuation_of": table.continuation_of,
            "header_source_table_id": table.header_source_table_id,
            "header_source_page": table.header_source_page, "bbox": None, "locator_bbox": None,
            "bbox_unit": vision.get("bbox_unit"), "region_bbox_px": vision.get("region_bbox_px"),
            "crop_bbox_px": vision.get("crop_bbox_px"),
        }
    return {
        "article_id": article.article_id, "part_no": article.part_no,
        "block_kind": article.block_kind, "indexable": article.is_indexable,
        "source_kind": "table_row" if article.row_id is not None else "table_parent",
        "match": "single_region" if sources else "unmatched", "article_bbox": None,
        "char_offsets": None, "regions": sources,
        "region_ids": [source["region_id"] for source in sources],
        "result_refs": sorted({source["result_ref"] for source in sources}),
        "table": table_payload,
        "row": None if article.row_id is None else {
            "row_id": article.row_id, "row_index": article.row_index,
            "cell_ids": list(article.source_cell_ids),
        },
        "cell_geometry": {
            cell.cell_id: [
                cell.row, cell.col, cell.rowspan, cell.colspan,
                cell.origin_row, cell.origin_col, cell.covered, cell.is_header,
            ]
            for cell in article.cell_provenance
        },
        "source_cell_ids": list(article.source_cell_ids),
        "warnings": warnings, "text_sha256": _sha256(article.text),
    }


def _evidence(
    articles: list[Article],
    blocks: list[_TextBlock],
    records: list[_TableRecord],
) -> dict[str, dict[str, Any]]:
    tables = {record.table.table_id: record for record in records}
    details: list[dict[str, Any]] = []
    cursor = 0
    for article in articles:
        if article.table_id and article.block_kind in _TABLE_ARTICLE_KINDS:
            details.append(_table_detail(article, tables))
        else:
            detail, cursor = _text_detail(article, blocks, cursor)
            details.append(detail)
    grouped: dict[str, dict[str, Any]] = {}
    for detail in details:
        article_id = detail["article_id"]
        part_no = detail.pop("part_no", 1)
        text_sha = detail.pop("text_sha256", None)
        group = grouped.get(article_id)
        if group is None:
            group = {**detail, "part_nos": [], "text_sha256s": []}
            grouped[article_id] = group
        group["part_nos"].append(part_no)
        group["text_sha256s"].append(text_sha)
        known = {region["region_id"] for region in group["regions"]}
        group["regions"].extend(region for region in detail["regions"] if region["region_id"] not in known)
        group["region_ids"] = _dedupe(group["region_ids"] + detail["region_ids"])
        group["result_refs"] = sorted(set(group["result_refs"] + detail["result_refs"]))
        group["source_cell_ids"] = _dedupe(group["source_cell_ids"] + detail["source_cell_ids"])
        group["cell_geometry"] = {**group["cell_geometry"], **detail["cell_geometry"]}
        group["warnings"] = _dedupe(group["warnings"] + detail["warnings"])
        group["indexable"] = group["indexable"] and detail["indexable"]
    for group in grouped.values():
        region_ids = {region["region_id"] for region in group["regions"]}
        group["match"] = (
            "unmatched"
            if not region_ids
            else "combined_regions"
            if len(region_ids) > 1
            else "single_region"
        )
    return grouped


def build_shadow_bundle(
    version: DocumentVersion,
    pages: list[LayoutPageResult],
    results: list[RoutedRegionResult],
    *,
    expected_pages: list[int] | None = None,
) -> OcrShadowBundle:
    """构建不可发布的影子 Bundle；不读模型、不联网、不写生产解析目录。"""

    _require(isinstance(version, DocumentVersion), "version 必须是 DocumentVersion")
    expected = _page_set(expected_pages)
    checked_pages, source_format = _check_pages(version, pages)
    by_page = {page.page.page: page for page in checked_pages}
    by_region = {region.region_id: region for page in checked_pages for region in page.regions}
    checked_results = _check_results(results, by_page, by_region)
    results_by_region = {result.crop.region.region_id: result for result in checked_results}
    indices = {result.crop.region.region_id: index for index, result in enumerate(checked_results)}
    warnings: list[str] = [SHADOW_NOTE]
    plans, pending, ignored = _plan(checked_pages, results_by_region, indices, warnings)
    page_audits = [
        _page_audit(page, plans, warnings)
        for page in sorted(checked_pages, key=lambda item: item.page.page)
    ]
    pending_pages = [audit for audit in page_audits if audit["pending_reasons"]]
    text, text_blocks, records, blocks = _content(plans, warnings, source_format)
    pages_seen = sorted(by_page)
    expected_set = set(expected) if expected is not None else None
    missing = sorted(expected_set - set(pages_seen)) if expected_set is not None else []
    extra = sorted(set(pages_seen) - expected_set) if expected_set is not None else []
    page_complete = expected_set is not None and not missing and not extra
    if expected is None:
        warnings.append("未提供 expected_pages：只能声明部分页覆盖，不能声明全量文档完成")
    elif missing:
        warnings.append("缺失预期页面：" + "、".join(map(str, missing)))
    elif extra:
        warnings.append("出现预期之外页面：" + "、".join(map(str, extra)))
    if page_complete and pending:
        warnings.append("页集合完整，但仍有待处理区域，不能声明全量完成")
    if pending_pages:
        warnings.append(
            f"有 {len(pending_pages)} 页存在页面级待处理事项，不能声明全量完成"
            "（详见 page_audits）"
        )
    complete = page_complete and not pending and not pending_pages
    partial = not complete
    if pending:
        warnings.append(
            f"有 {len(pending)} 个区域待人工/后续处理，未进入影子正文"
            "（原始结果引用保留在 result_refs）"
        )
    if ignored:
        warnings.append(f"页眉/页脚区域 {len(ignored)} 个已排除出可检索正文（审计保留）")
    coverage = {
        "expected_pages": expected, "pages_seen": pages_seen, "missing_pages": missing,
        "extra_pages": extra, "page_coverage_complete": page_complete,
        "coverage_complete": complete, "coverage_partial": partial,
        "pending_region_count": len(pending), "pending_page_count": len(pending_pages),
        "ignored_region_count": len(ignored),
    }
    parsed = _parsed(
        version, checked_pages, source_format, text, text_blocks, records, blocks, coverage, warnings
    )
    segment = segment_document(parsed, version, redact_contacts=True)
    articles = list(segment.articles)
    warnings.extend(segment.warnings)
    return OcrShadowBundle(
        version=version,
        articles=articles,
        article_evidence=_evidence(articles, text_blocks, records),
        pending_regions=pending,
        pending_pages=pending_pages,
        page_audits=page_audits,
        ignored_regions=ignored,
        result_refs=[_result_ref(index, result) for index, result in enumerate(checked_results)],
        warnings=_dedupe(warnings),
        redaction_counts=dict(segment.redaction_counts),
        source_sha256=version.source_sha256,
        source_image_hashes={
            page.page.page: page.page.image_sha256
            for page in sorted(checked_pages, key=lambda item: item.page.page)
        },
        pages_seen=pages_seen,
        expected_pages=expected,
        missing_pages=missing,
        extra_pages=extra,
        page_coverage_complete=page_complete,
        coverage_partial=partial,
        coverage_complete=complete,
        page_count=len(pages_seen),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_shadow_bundle(bundle: OcrShadowBundle, out_dir: Path | str) -> dict[str, Path]:
    """把影子 Bundle 写入显式目录；不生成向量、不发布、不动生产索引。"""

    _require(isinstance(bundle, OcrShadowBundle), "bundle 必须是 OcrShadowBundle")
    try:
        checked = OcrShadowBundle.model_validate(bundle.model_dump(mode="python"))
    except Exception as exc:
        raise ShadowValidationError(f"Bundle 重新校验失败: {_summary(exc)}") from exc
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    article_bytes = (
        "\n".join(
            json.dumps(article.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for article in checked.articles
        )
        + ("\n" if checked.articles else "")
    ).encode("utf-8")
    evidence_bytes = (
        json.dumps(
            checked.model_dump(mode="json", exclude={"version", "articles"}),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    article_path = target / "shadow_articles.jsonl"
    evidence_path = target / "shadow_evidence.json"
    manifest_path = target / "shadow_document.json"
    _atomic_write(article_path, article_bytes)
    _atomic_write(evidence_path, evidence_bytes)
    manifest = checked.model_dump(
        mode="json",
        include={
            "schema_version", "bridge_version", "parser_version", "status",
            "diagnostic_only", "trusted_parse", "shadow", "publishable",
            "visually_verified", "release_target", "version", "source_sha256",
            "source_image_hashes", "page_count", "pages_seen", "expected_pages",
            "missing_pages", "extra_pages", "page_coverage_complete",
            "coverage_partial", "coverage_complete", "warnings", "pending_pages",
        },
    )
    manifest["counts"] = {
        "articles": len(checked.articles),
        "article_evidence": len(checked.article_evidence),
        "pending_regions": len(checked.pending_regions),
        "pending_pages": len(checked.pending_pages),
        "page_audits": len(checked.page_audits),
        "ignored_regions": len(checked.ignored_regions),
        "result_refs": len(checked.result_refs),
    }
    manifest["files"] = {
        article_path.name: {
            "bytes": len(article_bytes), "sha256": hashlib.sha256(article_bytes).hexdigest()
        },
        evidence_path.name: {
            "bytes": len(evidence_bytes), "sha256": hashlib.sha256(evidence_bytes).hexdigest()
        },
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(manifest_path, manifest_bytes)
    return {"articles": article_path, "evidence": evidence_path, "manifest": manifest_path}


__all__ = [
    "SHADOW_BRIDGE_VERSION",
    "SHADOW_PARSER_VERSION",
    "SHADOW_SCHEMA_VERSION",
    "OcrShadowBundle",
    "ShadowValidationError",
    "build_shadow_bundle",
    "write_shadow_bundle",
]
