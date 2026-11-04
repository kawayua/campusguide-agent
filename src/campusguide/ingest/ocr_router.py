"""区域 OCR 路由：严格类型 + 本地优先、显式配置的云端回退。

所有证据通过 ``RegionCrop`` 绑定；本模块不生成、不推断任何页面或单元格
几何，也不把页面像素写入旧 PDF bbox 字段。导入本模块不会加载模型、读取
环境变量/凭据或发起网络请求；Pillow 只在调用 ``route_region`` 时惰性导入。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

from campusguide.ingest.ocr_layout import RegionCrop
from campusguide.ingest.structure import TableCell, TableRow, TableStructure
from campusguide.ingest.visual import VisionBudgetExceeded, VisionRequestBudget
from campusguide.ingest.xiaomi_ocr import (
    OcrBackend,
    OcrBackendRequest,
    OcrBackendResult,
    OcrPageRequest,
    OcrPageResult,
    OcrUsage,
    run_page_ocr,
)
from campusguide.observability.errors import safe_error_summary

ROUTER_SCHEMA_VERSION = "ocr-router/1"
_CACHE_RECORD_VERSION = "ocr-router-cache/3"
MAX_ROUTER_TOKENS = 16_384
PYDANTIC_CONFIG = ConfigDict(extra="forbid")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LOCAL_BACKEND_ID = "ocr-router-local"
_OFFLINE_BACKEND_ID = "ocr-router-offline"
_OFFLINE_REVISION = "ocr-router-offline/1"
_ACCEPTED_FINISH_REASONS = frozenset({"stop", "eos"})
_KIND_PROMPTS = {
    "text": "Extract the text in the image.",
    "title": "Extract the text in the image.",
    "table": "Parse the table in the image into OTSL.",
    "formula": (
        "Identify the formula in the image and represent it using LATEX format."
    ),
}
_TOKEN_FIELDS = {
    "text": "max_text_tokens",
    "title": "max_text_tokens",
    "table": "max_table_tokens",
    "formula": "max_formula_tokens",
}
_OTSL_MARKER_RE = re.compile(
    r"<\s*/?\s*(?:fcel|ecel|lcel|ucel|xcel|nl)\b[^>]*>", re.IGNORECASE
)
_MATH_RE = re.compile(
    r"\\[A-Za-z]+|\$[^$\n]+\$|\\[()\[\]]|\^|_\{|[=<>]"
)
_WHITESPACE_RE = re.compile(r"\s+")


class RouterValidationError(ValueError):
    """路由输入、源绑定或裁剪像素无效；在调用任何后端前抛出。"""


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("必须是布尔值")
    return value


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("必须是正整数（bool 不接受）")
    return value


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("必须是非负整数（bool 不接受）")
    return value


def _token_limit(value: object) -> int:
    number = _positive_int(value)
    if number > MAX_ROUTER_TOKENS:
        raise ValueError(f"token 上限必须在 1-{MAX_ROUTER_TOKENS} 之间")
    return number


def _non_negative_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("必须是非负有限浮点数")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("必须是非负有限浮点数")
    return number


def _sha256_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise ValueError(f"{name} 必须是 64 位十六进制 SHA-256")
    return value.strip().lower()


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} 不能为空")
    return text


def _string_list(value: object, name: str) -> list[str]:
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{name} 必须是字符串列表")
    return list(value)


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串或 null")
    return value


StrictBool = Annotated[bool, BeforeValidator(_strict_bool)]
PositiveInt = Annotated[int, BeforeValidator(_positive_int)]
NonNegativeInt = Annotated[int, BeforeValidator(_non_negative_int)]
TokenLimit = Annotated[int, BeforeValidator(_token_limit)]
NonNegativeFloat = Annotated[float, BeforeValidator(_non_negative_float)]


class RouterConfig(BaseModel):
    """显式 opt-in 的路由配置；``from_yaml`` 只读取调用方给出的路径。"""

    model_config = PYDANTIC_CONFIG

    schema_version: Literal["ocr-router/1"] = ROUTER_SCHEMA_VERSION
    enabled: StrictBool = True
    mode: Literal["shadow"] = "shadow"
    primary: Literal["xiaomi-local"] = "xiaomi-local"
    secondary: Literal["gemini"] = "gemini"
    difficult_only: StrictBool = True
    fallback_enabled: StrictBool = True
    max_cloud_requests_per_region: PositiveInt = 2
    max_text_tokens: TokenLimit = 1024
    max_table_tokens: TokenLimit = 4096
    max_formula_tokens: TokenLimit = 1024

    @classmethod
    def from_yaml(cls, path: Path | str) -> RouterConfig:
        import yaml

        config_path = Path(path).expanduser()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("RouterConfig YAML 顶层必须是对象")
        return cls.model_validate(raw)


class NumericExpectation(BaseModel):
    """一条源绑定的数值期望：在 ``row_key`` 的 ``key_col`` 定位行后读 ``col``。"""

    model_config = PYDANTIC_CONFIG

    row_key: str
    key_col: NonNegativeInt
    col: NonNegativeInt
    value: str

    @field_validator("row_key", mode="before")
    @classmethod
    def _row_key(cls, value: object) -> str:
        return _non_empty_text(value, "row_key")

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("value 必须是字符串")
        return value


class RegionExpectation(BaseModel):
    """裁剪区域的源绑定与内容期望；本批次只校验字段形状，不做实际比对。"""

    model_config = PYDANTIC_CONFIG

    source_sha256: str
    page_image_sha256: str
    page: PositiveInt
    region_id: str
    crop_sha256: str
    expected_table_column_count: PositiveInt | None = None
    min_table_rows: NonNegativeInt | None = None
    numeric_checks: list[NumericExpectation] = Field(default_factory=list)
    expected_text_anchors: list[str] = Field(default_factory=list)

    @field_validator(
        "source_sha256", "page_image_sha256", "crop_sha256", mode="before"
    )
    @classmethod
    def _hashes(cls, value: object, info: ValidationInfo) -> str:
        return _sha256_text(value, info.field_name)

    @field_validator("region_id", mode="before")
    @classmethod
    def _region_id(cls, value: object) -> str:
        return _non_empty_text(value, "region_id")

    @field_validator("numeric_checks", mode="before")
    @classmethod
    def _numeric_checks(cls, value: object) -> list[NumericExpectation]:
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list):
            raise ValueError("numeric_checks 必须是列表")
        return value

    @field_validator("expected_text_anchors", mode="before")
    @classmethod
    def _anchors(cls, value: object) -> list[str]:
        return _string_list(value, "expected_text_anchors")


class FallbackResult(BaseModel):
    """云端/回退后端的原始候选；``uncertain`` 默认保守为 true。"""

    model_config = PYDANTIC_CONFIG

    markdown: str | None = None
    raw_response: str | None = None
    uncertain: StrictBool = True
    finish_reason: str | None = None
    model: str
    model_revision: str
    prompt_revision: str
    usage: OcrUsage = Field(default_factory=OcrUsage)
    errors: list[str] = Field(default_factory=list)

    @field_validator("markdown", "raw_response", mode="before")
    @classmethod
    def _candidate_text(cls, value: object, info: ValidationInfo) -> str | None:
        return _optional_text(value, info.field_name)

    @field_validator("finish_reason", mode="before")
    @classmethod
    def _finish_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("finish_reason 必须是字符串或 null")
        return value.strip().lower() or None

    @field_validator("model", "model_revision", "prompt_revision", mode="before")
    @classmethod
    def _identities(cls, value: object, info: ValidationInfo) -> str:
        return _non_empty_text(value, info.field_name)

    @field_validator("errors", mode="before")
    @classmethod
    def _errors(cls, value: object) -> list[str]:
        return _string_list(value, "errors")


class FallbackBackend(Protocol):
    """回退后端协议；``model``/``version``/``prompt_revision`` 是缓存身份。"""

    model: str
    version: str
    prompt_revision: str

    def __call__(
        self,
        crop: RegionCrop,
        *,
        kind: str,
        budget: VisionRequestBudget,
    ) -> FallbackResult: ...


class RoutedRegionResult(BaseModel):
    """单区域路由结果；成功仅是结构性的，不可发布、未视觉核验。"""

    model_config = PYDANTIC_CONFIG

    schema_version: Literal["ocr-router/1"] = ROUTER_SCHEMA_VERSION
    crop: RegionCrop
    selected_source: Literal["local", "gemini", "none"] = "none"
    local_result: OcrPageResult | None
    fallback_result: FallbackResult | None
    markdown: str | None
    tables: list[TableStructure]
    reason_codes: list[str]
    errors: list[str]
    needs_review: StrictBool = True
    success: StrictBool = False
    publishable: Literal[False] = False
    visually_verified: Literal[False] = False
    usage: OcrUsage
    historical_usage: OcrUsage | None
    cache: Literal["disabled", "hit", "miss"] = "disabled"
    elapsed_seconds: NonNegativeFloat = 0.0
    historical_elapsed_seconds: NonNegativeFloat | None = None

    @field_validator("markdown", mode="before")
    @classmethod
    def _markdown(cls, value: object) -> str | None:
        return _optional_text(value, "markdown")

    @field_validator("tables", mode="before")
    @classmethod
    def _tables(cls, value: object) -> list[TableStructure]:
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list):
            raise ValueError("tables 必须是列表")
        return value

    @field_validator("reason_codes", "errors", mode="before")
    @classmethod
    def _string_lists(cls, value: object, info: ValidationInfo) -> list[str]:
        return _string_list(value, info.field_name)


class _RegionCloudBudget(VisionRequestBudget):
    """同时受父共享剩余量与单区域上限约束的子预算。"""

    def __init__(self, parent: VisionRequestBudget, limit: int) -> None:
        super().__init__(limit=limit)
        self._parent = parent

    def reserve(
        self,
        *,
        page_no: int,
        stage: str,
        image_sha256: str,
        image_path: str = "",
        attempt: int = 1,
        bbox: list[float] | None = None,
        provider: str = "",
        model: str = "",
        prompt_sha256: str = "",
    ) -> None:
        if len(self.requests) >= self.limit:
            raise VisionBudgetExceeded(
                f"单区域云请求上限 {self.limit} 已用尽（第 {page_no} 页，{stage}）"
            )
        if self._parent.remaining <= 0:
            raise VisionBudgetExceeded(
                f"共享视觉预算已用尽（第 {page_no} 页，{stage}）"
            )
        self._parent.reserve(
            page_no=page_no,
            stage=stage,
            image_sha256=image_sha256,
            image_path=image_path,
            attempt=attempt,
            bbox=bbox,
            provider=provider,
            model=model,
            prompt_sha256=prompt_sha256,
        )
        super().reserve(
            page_no=page_no,
            stage=stage,
            image_sha256=image_sha256,
            image_path=image_path,
            attempt=attempt,
            bbox=bbox,
            provider=provider,
            model=model,
            prompt_sha256=prompt_sha256,
        )


class _OfflineCandidateBackend:
    """把已取得的原始响应喂给 run_page_ocr 做离线结构校验。"""

    def __init__(self, text: str, *, finish_reason: str | None = "stop") -> None:
        self.text = text
        self.finish_reason = finish_reason

    def __call__(self, request: OcrBackendRequest) -> OcrBackendResult:
        return OcrBackendResult(
            text=self.text, finish_reason=self.finish_reason
        )


def _identity(text: str) -> str:
    return text


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _finish_ok(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _ACCEPTED_FINISH_REASONS


def _math_present(text: str | None) -> bool:
    return bool(text and _MATH_RE.search(text))


def _has_content(markdown: str | None, tables: list[TableStructure]) -> bool:
    return bool((markdown or "").strip() or tables)


def _assemble_result(
    crop: RegionCrop,
    *,
    started: float,
    reasons: list[str],
    selected_source: Literal["local", "gemini", "none"] = "none",
    local_result: OcrPageResult | None = None,
    fallback_result: FallbackResult | None = None,
    markdown: str | None = None,
    tables: list[TableStructure] | None = None,
    errors: list[str] | None = None,
    needs_review: bool = True,
    success: bool = False,
    usage: OcrUsage | None = None,
) -> RoutedRegionResult:
    return RoutedRegionResult(
        crop=crop,
        selected_source=selected_source,
        local_result=local_result,
        fallback_result=fallback_result,
        markdown=markdown,
        tables=tables or [],
        reason_codes=_dedupe(reasons),
        errors=_dedupe(errors or []),
        needs_review=needs_review,
        success=success,
        usage=usage or OcrUsage(),
        historical_usage=None,
        cache="disabled",
        elapsed_seconds=round(time.monotonic() - started, 6),
    )


def _validate_crop(crop: RegionCrop) -> None:
    if not isinstance(crop, RegionCrop):
        raise RouterValidationError("crop 必须是 RegionCrop")
    page = crop.page
    if crop.region.page != page.page:
        raise RouterValidationError("裁剪区域页码与页面证据不一致")
    left, top, right, bottom = crop.crop_bbox_px
    if not (
        0 <= left < right <= page.width
        and 0 <= top < bottom <= page.height
    ):
        raise RouterValidationError("crop_bbox_px 超出父页面或宽高为零")
    if crop.width != right - left or crop.height != bottom - top:
        raise RouterValidationError("裁剪尺寸与 crop_bbox_px 不一致")
    expected_affine = [1.0, 0.0, float(left), 0.0, 1.0, float(top)]
    if list(crop.crop_to_page) != expected_affine:
        raise RouterValidationError("crop_to_page 与 crop_bbox_px 平移不一致")
    x0, y0, x1, y1 = crop.region.bbox_px
    if not (0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height):
        raise RouterValidationError("区域 bbox 超出父页面")
    try:
        page.verify_current()
    except Exception as exc:
        raise RouterValidationError(
            f"父页面证据已失效: {safe_error_summary(exc)}"
        ) from exc
    try:
        payload = crop.image_path.read_bytes()
    except OSError as exc:
        raise RouterValidationError(
            f"无法读取裁剪 PNG: {safe_error_summary(exc)}"
        ) from exc
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != crop.image_sha256:
        raise RouterValidationError("裁剪 PNG 实际哈希与 RegionCrop 声明不一致")
    try:
        from PIL import Image
    except Exception as exc:
        raise RouterValidationError(
            f"无法导入 Pillow: {safe_error_summary(exc)}"
        ) from exc
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "PNG":
                raise RouterValidationError("裁剪文件不是 PNG")
            if image.size != (crop.width, crop.height):
                raise RouterValidationError("裁剪 PNG 尺寸与 RegionCrop 声明不一致")
            crop_pixels = image.convert("RGB").tobytes()
        with Image.open(page.image_path) as parent:
            if parent.size != (page.width, page.height):
                raise RouterValidationError("父页面 PNG 尺寸与证据不一致")
            parent_pixels = (
                parent.convert("RGB").crop((left, top, right, bottom)).tobytes()
            )
    except RouterValidationError:
        raise
    except Exception as exc:
        raise RouterValidationError(
            f"无法解码裁剪/父页面 PNG: {safe_error_summary(exc)}"
        ) from exc
    if crop_pixels != parent_pixels:
        raise RouterValidationError("裁剪 PNG 像素与父页面同区域不一致")


def _check_expectation_binding(
    crop: RegionCrop, expectation: RegionExpectation | None
) -> None:
    if expectation is None:
        return
    if not isinstance(expectation, RegionExpectation):
        raise RouterValidationError("expectation 必须是 RegionExpectation")
    checks = (
        ("source_sha256", expectation.source_sha256, crop.page.source_sha256),
        ("page_image_sha256", expectation.page_image_sha256, crop.page.image_sha256),
        ("page", expectation.page, crop.page.page),
        ("region_id", expectation.region_id, crop.region.region_id),
        ("crop_sha256", expectation.crop_sha256, crop.image_sha256),
    )
    for name, expected, actual in checks:
        if expected != actual:
            raise RouterValidationError(
                f"RegionExpectation 与裁剪证据不一致: {name}"
            )


def _validate_route_inputs(
    crop: RegionCrop,
    *,
    config: RouterConfig | None,
    expectation: RegionExpectation | None,
) -> tuple[RegionCrop, RouterConfig, RegionExpectation | None]:
    """在进入缓存或未缓存分支前统一重新校验全部公共入口输入。"""
    if not isinstance(crop, RegionCrop):
        raise RouterValidationError("crop 必须是 RegionCrop")
    try:
        checked_crop = RegionCrop.model_validate(crop.model_dump(mode="python"))
    except Exception as exc:
        raise RouterValidationError(
            f"裁剪证据重新校验失败: {safe_error_summary(exc)}"
        ) from exc
    if config is None:
        checked_config = RouterConfig()
    elif not isinstance(config, RouterConfig):
        raise RouterValidationError("config 必须是 RouterConfig")
    else:
        try:
            checked_config = RouterConfig.model_validate(
                config.model_dump(mode="python")
            )
        except Exception as exc:
            raise RouterValidationError(
                f"路由配置重新校验失败: {safe_error_summary(exc)}"
            ) from exc
    if expectation is None:
        checked_expectation = None
    elif not isinstance(expectation, RegionExpectation):
        raise RouterValidationError("expectation 必须是 RegionExpectation")
    else:
        try:
            checked_expectation = RegionExpectation.model_validate(
                expectation.model_dump(mode="python")
            )
        except Exception as exc:
            raise RouterValidationError(
                f"区域期望重新校验失败: {safe_error_summary(exc)}"
            ) from exc
    _validate_crop(checked_crop)
    _check_expectation_binding(checked_crop, checked_expectation)
    return checked_crop, checked_config, checked_expectation


def _numbers_equal(expected: str, actual: str) -> bool:
    left = expected.strip()
    right = actual.strip()
    if left == right:
        return True
    if not left or not right:
        return False
    try:
        left_number = Decimal(left)
        right_number = Decimal(right)
    except InvalidOperation:
        return False
    if not left_number.is_finite() or not right_number.is_finite():
        return False
    return left_number == right_number


def _span_contains(cell: TableCell, row_index: int, col_index: int) -> bool:
    return (
        cell.origin_row <= row_index < cell.origin_row + max(1, cell.rowspan)
        and cell.origin_col <= col_index < cell.origin_col + max(1, cell.colspan)
    )


def _origin_cell(
    table: TableStructure, row_index: int, col_index: int
) -> TableCell | None:
    for row in table.rows:
        for cell in row.cells:
            if (
                cell.row == row_index
                and cell.col == col_index
                and not cell.covered
            ):
                return cell
    return None


def _slot_cell(
    table: TableStructure, row_index: int, col_index: int
) -> TableCell | None:
    covered: list[TableCell] = []
    for row in table.rows:
        for cell in row.cells:
            if not _span_contains(cell, row_index, col_index):
                continue
            if not cell.covered:
                return cell
            covered.append(cell)
    for cell in covered:
        origin = _origin_cell(table, cell.origin_row, cell.origin_col)
        if origin is not None:
            return origin
    return None


def _normalize_row_key(text: str) -> str:
    return _WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", text))


def _expectation_issues(
    markdown: str | None,
    tables: list[TableStructure],
    expectation: RegionExpectation | None,
) -> list[str]:
    if expectation is None:
        return []
    issues: list[str] = []
    shape_tables = tables
    if expectation.expected_table_column_count is not None:
        shape_tables = [
            table
            for table in shape_tables
            if table.column_count == expectation.expected_table_column_count
        ]
        if not shape_tables:
            issues.append("expectation_table_columns")
    if expectation.min_table_rows is not None and not any(
        table.row_count >= expectation.min_table_rows for table in shape_tables
    ):
        issues.append("expectation_table_min_rows")
    for check in expectation.numeric_checks:
        expected_key = _normalize_row_key(check.row_key)
        matches: list[tuple[TableStructure, TableRow]] = []
        for table in tables:
            for row in table.rows:
                key_cell = _slot_cell(table, row.row_index, check.key_col)
                if (
                    key_cell is not None
                    and _normalize_row_key(key_cell.text) == expected_key
                ):
                    matches.append((table, row))
        if len(matches) != 1:
            issues.append("expectation_row_key")
            continue
        table, row = matches[0]
        value_cell = _slot_cell(table, row.row_index, check.col)
        if value_cell is None or not _numbers_equal(
            check.value, value_cell.text
        ):
            issues.append("expectation_value")
    if expectation.expected_text_anchors:
        texts = [markdown or ""]
        texts.extend(
            row.text for table in tables for row in table.rows if row.text
        )
        haystack = _WHITESPACE_RE.sub("", "\n".join(texts))
        for anchor in expectation.expected_text_anchors:
            if _WHITESPACE_RE.sub("", anchor) not in haystack:
                issues.append("expectation_text_anchor")
                break
    return _dedupe(issues)


def _candidate_issues(
    *,
    success: bool,
    errors: list[str],
    finish_reason: str | None,
    markdown: str | None,
    tables: list[TableStructure],
    kind: str,
    expectation: RegionExpectation | None,
) -> list[str]:
    issues: list[str] = []
    if not success or errors:
        issues.append("failed")
    if not _finish_ok(finish_reason):
        issues.append("finish_reason")
    if not _has_content(markdown, tables):
        issues.append("blank")
    if kind == "table" and not tables:
        issues.append("table_missing")
    if kind == "formula" and not _math_present(markdown):
        issues.append("formula_not_math")
    issues.extend(_expectation_issues(markdown, tables, expectation))
    return _dedupe(issues)


def _remap_tables(
    tables: list[TableStructure], crop: RegionCrop, kind: str
) -> list[TableStructure]:
    page = crop.page
    region = crop.region
    remapped: list[TableStructure] = []
    for ordinal, candidate in enumerate(tables, start=1):
        table = candidate.model_copy(deep=True)
        table_id = f"{region.region_id}#table{ordinal}"
        table.table_id = table_id
        table.block_id = f"{table_id}#block"
        table.page = page.page
        table.bbox = None
        if table.locator is not None:
            table.locator = table.locator.model_copy(update={"bbox": None})
        for row in table.rows:
            row.row_id = f"{table_id}#row{row.row_index}"
            if row.locator is not None:
                row.locator = row.locator.model_copy(update={"bbox": None})
            for cell in row.cells:
                cell.cell_id = f"{table_id}#r{cell.row}c{cell.col}"
                cell.bbox = None
        table.vision = {
            **(table.vision or {}),
            "bbox_unit": "page_pixels",
            "region_id": region.region_id,
            "region_kind": kind,
            "region_bbox_px": list(region.bbox_px),
            "crop_bbox_px": list(crop.crop_bbox_px),
            "doc_id": page.doc_id,
            "version_id": page.version_id,
            "source_format": page.source_format,
            "source_sha256": page.source_sha256,
            "page_image_path": str(page.image_path),
            "page_image_sha256": page.image_sha256,
            "page": page.page,
            "crop_image_path": str(crop.image_path),
            "crop_sha256": crop.image_sha256,
            "crop_to_page": list(crop.crop_to_page),
            "candidate_table_index": candidate.table_index,
            "table_index_in_region": ordinal - 1,
            "row_count": table.row_count,
            "column_count": table.column_count,
            "row_indices": [row.row_index for row in table.rows],
            "column_indices": list(range(table.column_count)),
            "header_row_indices": list(table.header_row_indices),
        }
        remapped.append(table)
    return remapped


def _usage_for(
    local_result: OcrPageResult | None,
    fallback_result: FallbackResult | None,
    cloud_requests: int,
) -> OcrUsage:
    local_requests = local_result.usage.local_requests if local_result else 0
    provider_count = int(local_requests > 0) + int(cloud_requests > 0)
    source: OcrUsage | None = None
    if provider_count == 1:
        if local_requests > 0 and local_result is not None:
            source = local_result.usage
        elif cloud_requests > 0 and fallback_result is not None:
            source = fallback_result.usage
    return OcrUsage(
        local_requests=local_requests,
        cloud_requests=cloud_requests,
        prompt_tokens=source.prompt_tokens if source else None,
        completion_tokens=source.completion_tokens if source else None,
        total_tokens=source.total_tokens if source else None,
    )


def _run_local(
    crop: RegionCrop,
    *,
    backend: OcrBackend,
    postprocess: Callable[[str], str],
    model: str,
    model_revision: str,
    postprocess_revision: str,
    kind: str,
    max_tokens: int,
) -> OcrPageResult:
    request = OcrPageRequest(
        doc_id=crop.page.doc_id,
        version_id=crop.page.version_id,
        source_format=crop.page.source_format,
        source_sha256=crop.page.source_sha256,
        page=crop.page.page,
        image_path=crop.image_path,
        image_sha256=crop.image_sha256,
        model=model,
        model_revision=model_revision,
        backend=_LOCAL_BACKEND_ID,
        postprocess_revision=postprocess_revision,
        prompt=_KIND_PROMPTS[kind],
        prompt_revision=_local_prompt_revision(kind),
        max_tokens=max_tokens,
    )
    return run_page_ocr(
        request,
        backend=backend,
        postprocess=postprocess,
        cache_dir=None,
    )


def _validate_fallback_candidate(
    crop: RegionCrop,
    *,
    fallback: FallbackResult,
    kind: str,
    postprocess: Callable[[str], str],
    max_tokens: int,
) -> OcrPageResult:
    candidate = fallback.markdown or ""
    request = OcrPageRequest(
        doc_id=crop.page.doc_id,
        version_id=crop.page.version_id,
        source_format=crop.page.source_format,
        source_sha256=crop.page.source_sha256,
        page=crop.page.page,
        image_path=crop.image_path,
        image_sha256=crop.image_sha256,
        model=fallback.model,
        model_revision=fallback.model_revision,
        backend=_OFFLINE_BACKEND_ID,
        postprocess_revision=_OFFLINE_REVISION,
        prompt=_KIND_PROMPTS[kind],
        prompt_revision=_OFFLINE_REVISION,
        max_tokens=max_tokens,
    )
    processor = postprocess if _OTSL_MARKER_RE.search(candidate) else _identity
    return run_page_ocr(
        request,
        backend=_OfflineCandidateBackend(candidate),
        postprocess=processor,
        cache_dir=None,
    )


def _result_needs_review(result: OcrPageResult) -> bool:
    if result.warnings:
        return True
    return any(
        table.warnings
        or any(cell.needs_review for row in table.rows for cell in row.cells)
        for table in result.tables
    )


def _local_prompt_revision(kind: str) -> str:
    return f"xiaomi-region-{kind}/1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _backend_identity(backend: object) -> str:
    stable = getattr(backend, "backend_identity", None)
    if isinstance(stable, str) and stable.strip():
        return stable.strip()
    module = getattr(backend, "__module__", None)
    if not isinstance(module, str) or not module.strip():
        module = type(backend).__module__
    qualname = getattr(backend, "__qualname__", None)
    if not isinstance(qualname, str) or not qualname.strip():
        qualname = type(backend).__qualname__
    return f"{module}.{qualname}"


def _fallback_binding(
    backend: FallbackBackend | None, config: RouterConfig
) -> dict[str, object]:
    if not config.fallback_enabled or not callable(backend):
        return {
            "enabled": False,
            "backend_identity": None,
            "model": None,
            "version": None,
            "prompt_revision": None,
        }
    return {
        "enabled": True,
        "backend_identity": _backend_identity(backend),
        "model": _non_empty_text(backend.model, "gemini_backend.model"),
        "version": _non_empty_text(backend.version, "gemini_backend.version"),
        "prompt_revision": _non_empty_text(
            backend.prompt_revision, "gemini_backend.prompt_revision"
        ),
    }


def _cache_binding(
    crop: RegionCrop,
    *,
    kind: str,
    local_backend: OcrBackend,
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    config: RouterConfig,
    expectation: RegionExpectation | None,
    gemini_backend: FallbackBackend | None,
    max_tokens: int,
) -> dict[str, object]:
    page = crop.page
    region = crop.region
    return {
        "record_version": _CACHE_RECORD_VERSION,
        "router_schema_version": ROUTER_SCHEMA_VERSION,
        "kind": kind,
        "local": {
            "backend_id": _LOCAL_BACKEND_ID,
            "backend_identity": _backend_identity(local_backend),
            "model": _non_empty_text(local_model, "local_model"),
            "model_revision": _non_empty_text(
                local_model_revision, "local_model_revision"
            ),
            "postprocess_revision": _non_empty_text(
                postprocess_revision, "postprocess_revision"
            ),
            "prompt_revision": _local_prompt_revision(kind),
            "prompt_sha256": hashlib.sha256(
                _KIND_PROMPTS[kind].encode("utf-8")
            ).hexdigest(),
            "max_tokens": max_tokens,
            "token_limits": {
                "max_text_tokens": config.max_text_tokens,
                "max_table_tokens": config.max_table_tokens,
                "max_formula_tokens": config.max_formula_tokens,
            },
            "offline_validation_revision": _OFFLINE_REVISION,
        },
        "page": {
            "doc_id": page.doc_id,
            "version_id": page.version_id,
            "source_format": page.source_format,
            "source_sha256": page.source_sha256,
            "page": page.page,
            "image_path": str(page.image_path),
            "image_sha256": page.image_sha256,
            "width": page.width,
            "height": page.height,
        },
        "region": {
            "region_id": region.region_id,
            "label": region.label,
            "kind": region.kind,
            "detector_id": region.detector_id,
            "detector_revision": region.detector_revision,
            "bbox_px": list(region.bbox_px),
        },
        "crop": {
            "crop_bbox_px": list(crop.crop_bbox_px),
            "image_path": str(crop.image_path),
            "image_sha256": crop.image_sha256,
            "width": crop.width,
            "height": crop.height,
            "crop_to_page": list(crop.crop_to_page),
        },
        "config": config.model_dump(mode="json"),
        "expectation": (
            expectation.model_dump(mode="json")
            if expectation is not None
            else None
        ),
        "fallback": _fallback_binding(gemini_backend, config),
    }


def _cache_key(binding: dict[str, object]) -> str:
    return _canonical_sha256(
        {"record_version": _CACHE_RECORD_VERSION, "binding": binding}
    )


def _cache_path(cache_dir: Path | str, key: str) -> Path:
    if isinstance(cache_dir, str):
        if not cache_dir.strip():
            raise RouterValidationError("cache_dir 不能是空字符串")
        base = Path(cache_dir.strip())
    elif isinstance(cache_dir, Path):
        base = cache_dir
    else:
        raise RouterValidationError("cache_dir 必须是 Path、非空字符串或 None")
    return base.expanduser() / f"{key}.json"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".part", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_cached_result(
    path: Path,
    *,
    binding: dict[str, object],
    key: str,
    result: RoutedRegionResult,
) -> None:
    if not result.success or result.selected_source not in {"local", "gemini"}:
        raise ValueError("只有结构性成功的路由结果可以写入缓存")
    result_payload = result.model_dump(mode="json")
    result_sha256 = _canonical_sha256(result_payload)
    payload = {
        "record_version": _CACHE_RECORD_VERSION,
        "key": key,
        "binding": binding,
        "result": result_payload,
        "result_sha256": result_sha256,
        "seal": _canonical_sha256(
            {"key": key, "result_sha256": result_sha256}
        ),
    }
    _atomic_write_json(path, payload)


def _validate_local_result_contract(
    result: OcrPageResult,
    *,
    crop: RegionCrop,
    kind: str,
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    max_tokens: int,
) -> None:
    page = crop.page
    if (
        result.doc_id != page.doc_id
        or result.version_id != page.version_id
        or result.source_format != page.source_format
        or result.source_sha256 != page.source_sha256
        or result.page != page.page
    ):
        raise ValueError("缓存本地结果与页面绑定不一致")
    if result.image_sha256 != crop.image_sha256:
        raise ValueError("缓存本地结果裁剪哈希不一致")
    if result.image_width != crop.width or result.image_height != crop.height:
        raise ValueError("缓存本地结果裁剪尺寸不一致")
    if Path(result.image_path) != crop.image_path:
        raise ValueError("缓存本地结果裁剪路径不一致")
    evidence = result.evidence
    if (
        evidence.source_format != page.source_format
        or evidence.source_sha256 != page.source_sha256
        or evidence.page != page.page
        or evidence.image_path != str(crop.image_path)
        or evidence.image_sha256 != crop.image_sha256
        or evidence.image_width != crop.width
        or evidence.image_height != crop.height
    ):
        raise ValueError("缓存本地结果证据不一致")
    if (
        result.model != local_model
        or result.model_revision != local_model_revision
        or result.backend != _LOCAL_BACKEND_ID
        or result.prompt_revision != _local_prompt_revision(kind)
        or result.postprocess_revision != postprocess_revision
        or result.max_tokens != max_tokens
    ):
        raise ValueError("缓存本地结果模型/提示绑定不一致")
    if result.cache != "disabled":
        raise ValueError("缓存本地候选自身不能是缓存命中")
    if result.publishable is not False or result.visually_verified is not False:
        raise ValueError("缓存本地候选不能声明可发布或已视觉核验")
    if result.usage.local_requests != 1 or result.usage.cloud_requests != 0:
        raise ValueError("缓存本地候选请求计数不一致")


def _validate_fallback_contract(
    result: FallbackResult, backend: FallbackBackend | None
) -> None:
    if not callable(backend):
        raise ValueError("缓存回退结果缺少可验证的后端身份")
    if (
        result.model != _non_empty_text(backend.model, "gemini_backend.model")
        or result.model_revision
        != _non_empty_text(backend.version, "gemini_backend.version")
        or result.prompt_revision
        != _non_empty_text(
            backend.prompt_revision, "gemini_backend.prompt_revision"
        )
    ):
        raise ValueError("缓存回退结果模型绑定不一致")


def _tables_payload(tables: list[TableStructure]) -> list[dict[str, Any]]:
    return [table.model_dump(mode="json") for table in tables]


def _rebuild_local_candidate(
    local: OcrPageResult,
    *,
    crop: RegionCrop,
    kind: str,
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    postprocess: Callable[[str], str],
    max_tokens: int,
) -> OcrPageResult:
    """用缓存中的原始响应离线重建本地候选；不调用任何真实后端。"""
    if local.raw_response is None:
        raise ValueError("缓存本地候选缺少原始响应，无法离线重建")
    request = OcrPageRequest(
        doc_id=crop.page.doc_id,
        version_id=crop.page.version_id,
        source_format=crop.page.source_format,
        source_sha256=crop.page.source_sha256,
        page=crop.page.page,
        image_path=crop.image_path,
        image_sha256=crop.image_sha256,
        model=local_model,
        model_revision=local_model_revision,
        backend=_LOCAL_BACKEND_ID,
        postprocess_revision=postprocess_revision,
        prompt=_KIND_PROMPTS[kind],
        prompt_revision=_local_prompt_revision(kind),
        max_tokens=max_tokens,
    )
    return run_page_ocr(
        request,
        backend=_OfflineCandidateBackend(
            local.raw_response, finish_reason=local.finish_reason
        ),
        postprocess=postprocess,
        cache_dir=None,
    )


def _validate_local_candidate_offline(
    local: OcrPageResult,
    *,
    crop: RegionCrop,
    kind: str,
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    postprocess: Callable[[str], str],
    max_tokens: int,
) -> OcrPageResult | None:
    if local.raw_response is None:
        if (
            local.success
            or local.markdown is not None
            or local.tables
            or local.finish_reason is not None
            or not local.errors
        ):
            raise ValueError("缓存本地候选缺少原始响应但声明了内容")
        return None
    rebuilt = _rebuild_local_candidate(
        local,
        crop=crop,
        kind=kind,
        local_model=local_model,
        local_model_revision=local_model_revision,
        postprocess_revision=postprocess_revision,
        postprocess=postprocess,
        max_tokens=max_tokens,
    )
    if _canonical_json(_tables_payload(local.tables)) != _canonical_json(
        _tables_payload(rebuilt.tables)
    ):
        raise ValueError("缓存本地表格与原始响应重建不一致")
    if local.markdown != rebuilt.markdown:
        raise ValueError("缓存本地 Markdown 与原始响应重建不一致")
    if list(local.errors) != list(rebuilt.errors):
        raise ValueError("缓存本地错误与原始响应重建不一致")
    if bool(local.success) != bool(rebuilt.success):
        raise ValueError("缓存本地成功标记与原始响应重建不一致")
    if local.finish_reason != rebuilt.finish_reason:
        raise ValueError("缓存本地终止状态与原始响应重建不一致")
    return rebuilt


def _validate_cached_result(
    result: RoutedRegionResult,
    *,
    crop: RegionCrop,
    kind: str,
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    postprocess: Callable[[str], str],
    expectation: RegionExpectation | None,
    gemini_backend: FallbackBackend | None,
    max_tokens: int,
) -> None:
    if result.schema_version != ROUTER_SCHEMA_VERSION:
        raise ValueError("缓存结果 schema 不匹配")
    if result.cache != "disabled":
        raise ValueError("缓存结果自身不能是缓存命中")
    if result.historical_usage is not None:
        raise ValueError("缓存结果不得携带历史用量")
    if result.historical_elapsed_seconds is not None:
        raise ValueError("缓存结果不得携带历史耗时")
    if result.publishable is not False or result.visually_verified is not False:
        raise ValueError("缓存结果不能声明可发布或已视觉核验")
    if result.success is not True:
        raise ValueError("只有结构性成功结果可以命中缓存")
    if result.selected_source not in {"local", "gemini"}:
        raise ValueError("缓存结果没有选中的来源")
    if _canonical_json(result.crop.model_dump(mode="json")) != _canonical_json(
        crop.model_dump(mode="json")
    ):
        raise ValueError("缓存结果裁剪绑定不一致")

    local = result.local_result
    if local is None:
        raise ValueError("缓存结果缺少本地候选")
    _validate_local_result_contract(
        local,
        crop=crop,
        kind=kind,
        local_model=local_model,
        local_model_revision=local_model_revision,
        postprocess_revision=postprocess_revision,
        max_tokens=max_tokens,
    )
    rebuilt_local = _validate_local_candidate_offline(
        local,
        crop=crop,
        kind=kind,
        local_model=local_model,
        local_model_revision=local_model_revision,
        postprocess_revision=postprocess_revision,
        postprocess=postprocess,
        max_tokens=max_tokens,
    )
    local_issues = _candidate_issues(
        success=local.success,
        errors=local.errors,
        finish_reason=local.finish_reason,
        markdown=local.markdown,
        tables=local.tables,
        kind=kind,
        expectation=expectation,
    )
    if result.selected_source == "local":
        if result.fallback_result is not None:
            raise ValueError("本地成功结果不得携带回退候选")
        if local_issues:
            raise ValueError("缓存本地候选不再通过结构校验")
        if rebuilt_local is None:
            raise ValueError("缓存本地成功候选缺少可重放的原始响应")
        expected_tables = _remap_tables(rebuilt_local.tables, crop, kind)
        expected_markdown = local.markdown
        expected_errors = _dedupe(local.errors)
        expected_needs_review = _result_needs_review(local)
        expected_usage = _usage_for(local, None, 0)
    else:
        fallback = result.fallback_result
        if fallback is None:
            raise ValueError("缓存回退结果缺失")
        if not local_issues:
            raise ValueError("缓存回退结果存在但本地候选已通过")
        _validate_fallback_contract(fallback, gemini_backend)
        if fallback.uncertain:
            raise ValueError("缓存回退结果不确定")
        if fallback.errors:
            raise ValueError("缓存回退结果带错误")
        if not _finish_ok(fallback.finish_reason):
            raise ValueError("缓存回退结果终止状态无效")
        if not (fallback.markdown or "").strip():
            raise ValueError("缓存回退结果为空")
        if result.usage.cloud_requests <= 0:
            raise ValueError("缓存回退结果没有云端请求计数")
        validated = _validate_fallback_candidate(
            crop,
            fallback=fallback,
            kind=kind,
            postprocess=postprocess,
            max_tokens=max_tokens,
        )
        fallback_issues = _candidate_issues(
            success=validated.success,
            errors=validated.errors,
            finish_reason=validated.finish_reason,
            markdown=validated.markdown,
            tables=validated.tables,
            kind=kind,
            expectation=expectation,
        )
        if fallback_issues:
            raise ValueError("缓存回退候选不再通过结构校验")
        expected_tables = _remap_tables(validated.tables, crop, kind)
        expected_markdown = validated.markdown or fallback.markdown
        expected_errors = _dedupe(validated.errors)
        expected_needs_review = _result_needs_review(validated)
        expected_usage = _usage_for(
            local, fallback, result.usage.cloud_requests
        )
    if result.usage != expected_usage:
        raise ValueError("缓存结果用量与候选不一致")
    if _canonical_json(_tables_payload(result.tables)) != _canonical_json(
        _tables_payload(expected_tables)
    ):
        raise ValueError("缓存表格 ID 或证据与候选重建不一致")
    if result.markdown != expected_markdown:
        raise ValueError("缓存 Markdown 与候选不一致")
    if list(result.errors) != expected_errors:
        raise ValueError("缓存错误列表与候选不一致")
    if bool(result.needs_review) != bool(expected_needs_review):
        raise ValueError("缓存复核标记与候选不一致")


def _read_cached_result(
    path: Path,
    *,
    binding: dict[str, object],
    key: str,
    crop: RegionCrop,
    kind: str,
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    postprocess: Callable[[str], str],
    expectation: RegionExpectation | None,
    gemini_backend: FallbackBackend | None,
    max_tokens: int,
) -> tuple[RoutedRegionResult | None, bool]:
    try:
        if not path.exists():
            return None, False
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("缓存记录不是 JSON 对象")
        if payload.get("record_version") != _CACHE_RECORD_VERSION:
            raise ValueError("缓存记录版本不匹配")
        if payload.get("key") != key:
            raise ValueError("缓存键不匹配")
        if payload.get("binding") != binding:
            raise ValueError("缓存绑定不匹配")
        result_payload = payload.get("result")
        if not isinstance(result_payload, dict):
            raise ValueError("缓存记录缺少结果")
        result_sha256 = _canonical_sha256(result_payload)
        if payload.get("result_sha256") != result_sha256:
            raise ValueError("缓存结果摘要不匹配")
        expected_seal = _canonical_sha256(
            {"key": key, "result_sha256": result_sha256}
        )
        if payload.get("seal") != expected_seal:
            raise ValueError("缓存封条不匹配")
        result = RoutedRegionResult.model_validate(result_payload)
        _validate_cached_result(
            result,
            crop=crop,
            kind=kind,
            local_model=local_model,
            local_model_revision=local_model_revision,
            postprocess_revision=postprocess_revision,
            postprocess=postprocess,
            expectation=expectation,
            gemini_backend=gemini_backend,
            max_tokens=max_tokens,
        )
        return result, False
    except Exception:
        return None, True


def _cached_hit_result(
    cached: RoutedRegionResult, *, started: float
) -> RoutedRegionResult:
    return cached.model_copy(
        update={
            "usage": OcrUsage(),
            "historical_usage": cached.usage,
            "cache": "hit",
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "historical_elapsed_seconds": cached.elapsed_seconds,
        }
    )


def _route_region_uncached(
    crop: RegionCrop,
    *,
    local_backend: OcrBackend,
    postprocess: Callable[[str], str],
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    config: RouterConfig | None = None,
    gemini_backend: FallbackBackend | None = None,
    budget: VisionRequestBudget | None = None,
    expectation: RegionExpectation | None = None,
) -> RoutedRegionResult:
    """路由单个区域；本地通过即不触云，回退仅在显式配置时发生。"""
    started = time.monotonic()
    if config is None:
        config = RouterConfig()
    _validate_crop(crop)
    _check_expectation_binding(crop, expectation)
    kind = crop.region.kind
    if not config.enabled:
        return _assemble_result(
            crop, started=started, reasons=["router_disabled"]
        )
    if kind not in _KIND_PROMPTS:
        return _assemble_result(
            crop, started=started, reasons=["kind_requires_review"]
        )
    if not callable(local_backend):
        raise RouterValidationError("local_backend 必须可调用")
    if not callable(postprocess):
        raise RouterValidationError("postprocess 必须可调用")
    max_tokens = getattr(config, _TOKEN_FIELDS[kind])
    try:
        local_result = _run_local(
            crop,
            backend=local_backend,
            postprocess=postprocess,
            model=local_model,
            model_revision=local_model_revision,
            postprocess_revision=postprocess_revision,
            kind=kind,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise RouterValidationError(
            f"本地 OCR 请求无效: {safe_error_summary(exc)}"
        ) from exc

    local_issues = _candidate_issues(
        success=local_result.success,
        errors=local_result.errors,
        finish_reason=local_result.finish_reason,
        markdown=local_result.markdown,
        tables=local_result.tables,
        kind=kind,
        expectation=expectation,
    )
    local_reasons = [f"local_{issue}" for issue in local_issues]
    if not local_issues:
        return _assemble_result(
            crop,
            started=started,
            reasons=["local_accepted"],
            selected_source="local",
            local_result=local_result,
            markdown=local_result.markdown,
            tables=_remap_tables(local_result.tables, crop, kind),
            errors=list(local_result.errors),
            needs_review=_result_needs_review(local_result),
            success=True,
            usage=_usage_for(local_result, None, 0),
        )

    errors = list(local_result.errors)
    fallback_reasons: list[str] = []
    fallback_result: FallbackResult | None = None
    validated: OcrPageResult | None = None
    cloud_requests = 0

    if not config.fallback_enabled:
        fallback_reasons.append("fallback_disabled")
    elif not callable(gemini_backend):
        fallback_reasons.append("fallback_unavailable")
    else:
        if budget is not None and not isinstance(budget, VisionRequestBudget):
            raise RouterValidationError("budget 必须是 VisionRequestBudget")
        parent_budget = (
            budget
            if budget is not None
            else VisionRequestBudget(limit=config.max_cloud_requests_per_region)
        )
        if parent_budget.remaining <= 0:
            fallback_reasons.append("fallback_budget_exhausted")
        else:
            child_budget = _RegionCloudBudget(
                parent_budget, config.max_cloud_requests_per_region
            )
            before_cloud = parent_budget.images_sent
            try:
                raw = gemini_backend(crop, kind=kind, budget=child_budget)
            except VisionBudgetExceeded:
                fallback_reasons.append("fallback_budget_exhausted")
            except Exception as exc:
                fallback_reasons.append("fallback_exception")
                errors.append(
                    f"fallback_backend_error: {safe_error_summary(exc)}"
                )
            else:
                if isinstance(raw, FallbackResult):
                    fallback_result = raw
                else:
                    fallback_reasons.append("fallback_invalid_result")
                    errors.append(
                        "fallback_backend_error: 返回值不是 FallbackResult"
                    )
            cloud_requests = max(
                0, parent_budget.images_sent - before_cloud
            )

            if fallback_result is not None:
                if fallback_result.uncertain:
                    fallback_reasons.append("fallback_uncertain")
                if fallback_result.errors:
                    fallback_reasons.append("fallback_errors")
                    errors.extend(fallback_result.errors)
                if not _finish_ok(fallback_result.finish_reason):
                    fallback_reasons.append("fallback_finish_reason")
                if not (fallback_result.markdown or "").strip():
                    fallback_reasons.append("fallback_blank_output")
                if cloud_requests == 0:
                    fallback_reasons.append("unmetered_fallback")
                if (
                    cloud_requests > 0
                    and not fallback_result.uncertain
                    and not fallback_result.errors
                    and _finish_ok(fallback_result.finish_reason)
                    and (fallback_result.markdown or "").strip()
                ):
                    try:
                        validated = _validate_fallback_candidate(
                            crop,
                            fallback=fallback_result,
                            kind=kind,
                            postprocess=postprocess,
                            max_tokens=max_tokens,
                        )
                    except Exception as exc:
                        fallback_reasons.append(
                            "fallback_validation_error"
                        )
                        errors.append(
                            "fallback_validation_error: "
                            + safe_error_summary(exc)
                        )
                    else:
                        errors.extend(validated.errors)
                        fallback_reasons.extend(
                            f"fallback_{issue}"
                            for issue in _candidate_issues(
                                success=validated.success,
                                errors=validated.errors,
                                finish_reason=validated.finish_reason,
                                markdown=validated.markdown,
                                tables=validated.tables,
                                kind=kind,
                                expectation=expectation,
                            )
                        )

    if fallback_result is None and not fallback_reasons:
        fallback_reasons.append("fallback_unavailable")
    if fallback_reasons or fallback_result is None or validated is None:
        return _assemble_result(
            crop,
            started=started,
            reasons=[*local_reasons, *fallback_reasons],
            selected_source="none",
            local_result=local_result,
            fallback_result=fallback_result,
            errors=errors,
            needs_review=True,
            success=False,
            usage=_usage_for(local_result, fallback_result, cloud_requests),
        )

    return _assemble_result(
        crop,
        started=started,
        reasons=[*local_reasons, "fallback_accepted"],
        selected_source="gemini",
        local_result=local_result,
        fallback_result=fallback_result,
        markdown=validated.markdown or fallback_result.markdown,
        tables=_remap_tables(validated.tables, crop, kind),
        errors=list(validated.errors),
        needs_review=_result_needs_review(validated),
        success=True,
        usage=_usage_for(local_result, fallback_result, cloud_requests),
    )


def route_region(
    crop: RegionCrop,
    *,
    local_backend: OcrBackend,
    postprocess: Callable[[str], str],
    local_model: str,
    local_model_revision: str,
    postprocess_revision: str,
    config: RouterConfig | None = None,
    gemini_backend: FallbackBackend | None = None,
    budget: VisionRequestBudget | None = None,
    cache_dir: Path | str | None = None,
    expectation: RegionExpectation | None = None,
) -> RoutedRegionResult:
    """缓存包装；所有公共输入在进入任一分支前统一重新校验。"""
    started = time.monotonic()
    checked_crop, checked_config, checked_expectation = _validate_route_inputs(
        crop, config=config, expectation=expectation
    )
    if cache_dir is None:
        return _route_region_uncached(
            checked_crop,
            local_backend=local_backend,
            postprocess=postprocess,
            local_model=local_model,
            local_model_revision=local_model_revision,
            postprocess_revision=postprocess_revision,
            config=checked_config,
            gemini_backend=gemini_backend,
            budget=budget,
            expectation=checked_expectation,
        )

    kind = checked_crop.region.kind
    if not checked_config.enabled or kind not in _KIND_PROMPTS:
        result = _route_region_uncached(
            checked_crop,
            local_backend=local_backend,
            postprocess=postprocess,
            local_model=local_model,
            local_model_revision=local_model_revision,
            postprocess_revision=postprocess_revision,
            config=checked_config,
            gemini_backend=gemini_backend,
            budget=budget,
            expectation=checked_expectation,
        )
        return result.model_copy(update={"cache": "miss"})
    if not callable(local_backend):
        raise RouterValidationError("local_backend 必须可调用")
    if not callable(postprocess):
        raise RouterValidationError("postprocess 必须可调用")
    max_tokens = getattr(checked_config, _TOKEN_FIELDS[kind])
    try:
        binding = _cache_binding(
            checked_crop,
            kind=kind,
            local_backend=local_backend,
            local_model=local_model,
            local_model_revision=local_model_revision,
            postprocess_revision=postprocess_revision,
            config=checked_config,
            expectation=checked_expectation,
            gemini_backend=gemini_backend,
            max_tokens=max_tokens,
        )
        key = _cache_key(binding)
        path = _cache_path(cache_dir, key)
    except RouterValidationError:
        raise
    except Exception as exc:
        raise RouterValidationError(
            f"无法构建缓存绑定: {safe_error_summary(exc)}"
        ) from exc

    cached, cache_corrupt = _read_cached_result(
        path,
        binding=binding,
        key=key,
        crop=checked_crop,
        kind=kind,
        local_model=local_model,
        local_model_revision=local_model_revision,
        postprocess_revision=postprocess_revision,
        postprocess=postprocess,
        expectation=checked_expectation,
        gemini_backend=gemini_backend,
        max_tokens=max_tokens,
    )
    if cached is not None:
        return _cached_hit_result(cached, started=started)

    result = _route_region_uncached(
        checked_crop,
        local_backend=local_backend,
        postprocess=postprocess,
        local_model=local_model,
        local_model_revision=local_model_revision,
        postprocess_revision=postprocess_revision,
        config=checked_config,
        gemini_backend=gemini_backend,
        budget=budget,
        expectation=checked_expectation,
    )
    if result.success:
        try:
            _write_cached_result(
                path, binding=binding, key=key, result=result
            )
        except Exception:
            result = result.model_copy(
                update={
                    "reason_codes": _dedupe(
                        [*result.reason_codes, "cache_write_failed"]
                    )
                }
            )
    if cache_corrupt:
        result = result.model_copy(
            update={
                "reason_codes": _dedupe(
                    [*result.reason_codes, "cache_corrupt"]
                )
            }
        )
    return result.model_copy(update={"cache": "miss"})


__all__ = [
    "FallbackBackend",
    "FallbackResult",
    "NumericExpectation",
    "RegionExpectation",
    "RoutedRegionResult",
    "RouterConfig",
    "RouterValidationError",
    "route_region",
]
