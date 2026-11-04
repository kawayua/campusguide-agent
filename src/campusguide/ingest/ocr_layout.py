"""页面布局检测、区域审计与区域裁剪的离线边界层。

本模块只处理调用方提供的真实页面 PNG 和本地布局模型输出。它不下载模型、
不读取凭据、不接入生产解析入口，也不从文本或规则生成“模型框”。布局框是
输入页面的像素坐标；裁剪只做轴对齐像素裁剪，不缩放、不补造单元格框。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import zlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

PYDANTIC_CONFIG = ConfigDict(extra="forbid")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_REGION_ID_DIGEST_LENGTH = 32
_PP_DOCLAYOUT_V3_DETECTOR_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
# 后处理阈值低于 build_layout 的 min_score 时，低于阈值的候选仍进入审计，
# 而不是在 Transformers 后处理内部无声消失；这些候选不会被当成有效区域。
_POSTPROCESS_AUDIT_FLOOR = 0.01

RegionKind = Literal[
    "text",
    "title",
    "table",
    "formula",
    "figure",
    "header",
    "footer",
    "unknown",
]
_PIXEL_UNIT = "pixels"
_NORMALIZED_UNIT = "normalized"


class LayoutValidationError(ValueError):
    """页面证据、区域绑定或裁剪参数不满足本模块的严格约束。"""


class LayoutInferenceError(RuntimeError):
    """本地布局模型、处理器或图像解码失败；调用方应保守复核。"""


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("必须是正整数（bool 不接受）")
    return value


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("必须是非负整数（bool 不接受）")
    return value


def _finite_float(value: object, *, name: str = "数值") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限浮点数")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限浮点数")
    return number


def _score_float(value: object) -> float:
    number = _finite_float(value, name="score")
    if not 0.0 <= number <= 1.0:
        raise ValueError("score 必须在闭区间 [0, 1] 内")
    return number


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("必须是布尔值")
    return value


def _clean_text(value: object, *, name: str, max_length: int = 300) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    text = value.strip()
    if not text or len(text) > max_length:
        raise ValueError(f"{name} 不能为空且长度不超过 {max_length}")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"{name} 不能包含控制字符")
    return text


def _clean_sha256(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise ValueError(f"{name} 必须是 64 位十六进制 SHA-256")
    return value.strip().lower()


def _path_value(value: object) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value.strip():
        path = Path(value.strip())
    else:
        raise ValueError("路径必须是非空 Path 或字符串")
    return path.expanduser().resolve()


def _path_input(value: object, *, mode: str) -> Path | str:
    # 严格 JSON 模式只接受 str，before validator 返回 Path 会被 path_type 拒绝。
    path = _path_value(value)
    return path if mode == "python" else str(path)


def _bbox_values(value: object, *, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} 必须是 [x0, y0, x1, y1]")
    return [_finite_float(item, name=name) for item in value]


def _crop_bbox_values(value: object) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("crop_bbox_px 必须是 4 个整数")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("crop_bbox_px 必须是整数像素")
        result.append(item)
    return result


def _affine_values(value: object) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise ValueError("crop_to_page 必须是 6 项仿射参数")
    return [_finite_float(item, name="crop_to_page") for item in value]


def _polygon_values(value: object) -> list[list[float]] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise ValueError("polygon 至少需要 3 个点")
    points: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("polygon 每个点必须是 [x, y]")
        points.append(
            [
                _finite_float(point[0], name="polygon.x"),
                _finite_float(point[1], name="polygon.y"),
            ]
        )
    return points


def _warning_texts(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("warnings 必须是字符串列表")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("warnings 必须是字符串列表")
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", item).strip()
        if text:
            result.append(text[:500])
    return result


def _safe_error(exc: BaseException) -> str:
    text = re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()
    return text[:300]


def _unique_warnings(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _revalidated(model_type: type[_ModelT], value: _ModelT | dict[str, Any]) -> _ModelT:
    """模型实例也重新校验，避免 model_construct/model_copy 绕过严格字段约束。"""

    if isinstance(value, model_type):
        value = value.model_dump()
    return model_type.model_validate(value)


def _inspect_png_bytes(data: bytes, *, path: Path | None = None) -> tuple[int, int, str]:
    where = f": {path}" if path is not None else ""
    if len(data) < 33 or not data.startswith(_PNG_SIGNATURE):
        raise LayoutValidationError(f"页面图像不是 PNG{where}")
    if int.from_bytes(data[8:12], "big") != 13 or data[12:16] != b"IHDR":
        raise LayoutValidationError(f"PNG 缺少合法 IHDR{where}")
    expected_crc = int.from_bytes(data[29:33], "big")
    actual_crc = zlib.crc32(data[12:29]) & 0xFFFFFFFF
    if expected_crc != actual_crc:
        raise LayoutValidationError(f"PNG IHDR CRC 不匹配{where}")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise LayoutValidationError(f"PNG 宽高必须为正{where}")
    return width, height, hashlib.sha256(data).hexdigest()


def _read_png_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LayoutValidationError(
            f"无法读取页面 PNG: {_safe_error(exc)}"
        ) from exc


PositiveInt = Annotated[int, BeforeValidator(_positive_int)]
NonNegativeInt = Annotated[int, BeforeValidator(_non_negative_int)]
ScoreFloat = Annotated[float, BeforeValidator(_score_float)]
StrictBool = Annotated[bool, BeforeValidator(_strict_bool)]


class PageImageEvidence(BaseModel):
    """真实页面 PNG 的身份、尺寸和实际字节哈希。"""

    model_config = PYDANTIC_CONFIG

    doc_id: str
    version_id: str
    source_format: Literal["pdf", "html"]
    source_sha256: str
    page: PositiveInt
    image_path: Path
    image_sha256: str
    width: PositiveInt
    height: PositiveInt

    @field_validator("doc_id", "version_id", mode="before")
    @classmethod
    def _identities(cls, value: object, info: Any) -> str:
        return _clean_text(value, name=info.field_name)

    @field_validator("source_sha256", "image_sha256", mode="before")
    @classmethod
    def _hashes(cls, value: object, info: Any) -> str:
        return _clean_sha256(value, name=info.field_name)

    @field_validator("image_path", mode="before")
    @classmethod
    def _path(cls, value: object, info: ValidationInfo) -> Path | str:
        return _path_input(value, mode=info.mode)

    @classmethod
    def from_png(
        cls,
        *,
        doc_id: str,
        version_id: str,
        source_format: Literal["pdf", "html"],
        source_sha256: str,
        page: int,
        image_path: Path,
    ) -> PageImageEvidence:
        path = _path_value(image_path)
        width, height, image_sha256 = _inspect_png_bytes(
            _read_png_bytes(path), path=path
        )
        return cls(
            doc_id=doc_id,
            version_id=version_id,
            source_format=source_format,
            source_sha256=source_sha256,
            page=page,
            image_path=path,
            image_sha256=image_sha256,
            width=width,
            height=height,
        )

    def verify_current(self) -> None:
        """确认磁盘上的页面仍是构建证据时的同一份 PNG。"""

        width, height, image_sha256 = _inspect_png_bytes(
            _read_png_bytes(self.image_path), path=self.image_path
        )
        if (
            image_sha256 != self.image_sha256
            or width != self.width
            or height != self.height
        ):
            raise LayoutValidationError("页面 PNG 已变化，与记录的证据不一致")


class LayoutDetection(BaseModel):
    """布局模型的一条原始检测；坐标为输入页面像素 xyxy。"""

    model_config = PYDANTIC_CONFIG

    label: str
    score: ScoreFloat
    bbox_px: list[float]
    order: NonNegativeInt
    polygon: list[list[float]] | None = None

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: object) -> str:
        return _clean_text(value, name="label")

    @field_validator("bbox_px", mode="before")
    @classmethod
    def _bbox(cls, value: object) -> list[float]:
        return _bbox_values(value, name="bbox_px")

    @field_validator("polygon", mode="before")
    @classmethod
    def _polygon(cls, value: object) -> list[list[float]] | None:
        return _polygon_values(value)


class LayoutRegion(BaseModel):
    """经过审计的区域；region_id 由文档、检测器与几何确定。"""

    model_config = PYDANTIC_CONFIG

    region_id: str
    page: PositiveInt
    label: str
    kind: RegionKind
    score: ScoreFloat
    bbox_px: list[float]
    order: NonNegativeInt
    detector_id: str
    detector_revision: str
    warnings: list[str] = Field(default_factory=list)

    @field_validator("region_id", mode="before")
    @classmethod
    def _region_id(cls, value: object) -> str:
        return _clean_text(value, name="region_id", max_length=1000)

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: object) -> str:
        return _clean_text(value, name="label")

    @field_validator("bbox_px", mode="before")
    @classmethod
    def _bbox(cls, value: object) -> list[float]:
        return _bbox_values(value, name="bbox_px")

    @field_validator("detector_id", "detector_revision", mode="before")
    @classmethod
    def _detector_fields(cls, value: object, info: Any) -> str:
        return _clean_text(value, name=info.field_name)

    @field_validator("warnings", mode="before")
    @classmethod
    def _warnings(cls, value: object) -> list[str]:
        return _warning_texts(value)


class LayoutPageResult(BaseModel):
    """单页布局结果；没有有效区域时必须进入人工复核。"""

    model_config = PYDANTIC_CONFIG

    page: PageImageEvidence
    regions: list[LayoutRegion] = Field(default_factory=list)
    rejected_detections: list[dict[str, Any]] = Field(default_factory=list)
    detector_id: str
    detector_revision: str
    warnings: list[str] = Field(default_factory=list)
    needs_review: StrictBool

    @field_validator("detector_id", "detector_revision", mode="before")
    @classmethod
    def _detector_fields(cls, value: object, info: Any) -> str:
        return _clean_text(value, name=info.field_name)

    @field_validator("warnings", mode="before")
    @classmethod
    def _warnings(cls, value: object) -> list[str]:
        return _warning_texts(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.regions and not self.needs_review:
            raise ValueError("没有布局区域时 needs_review 必须为 true")
        seen: set[str] = set()
        for region in self.regions:
            if region.region_id in seen:
                raise ValueError(f"重复的 region_id: {region.region_id}")
            seen.add(region.region_id)
            if region.page != self.page.page:
                raise ValueError("region 页码与页面证据不一致")
            if (
                region.detector_id != self.detector_id
                or region.detector_revision != self.detector_revision
            ):
                raise ValueError("region 检测器身份与结果不一致")
        return self


class RegionCrop(BaseModel):
    """实际轴对齐 PNG 裁剪及其到原页面的像素仿射映射。"""

    model_config = PYDANTIC_CONFIG

    page: PageImageEvidence
    region: LayoutRegion
    crop_bbox_px: list[int]
    image_path: Path
    image_sha256: str
    width: PositiveInt
    height: PositiveInt
    crop_to_page: list[float]
    warnings: list[str] = Field(default_factory=list)

    @field_validator("crop_bbox_px", mode="before")
    @classmethod
    def _crop_bbox(cls, value: object) -> list[int]:
        return _crop_bbox_values(value)

    @field_validator("crop_to_page", mode="before")
    @classmethod
    def _crop_to_page(cls, value: object) -> list[float]:
        return _affine_values(value)

    @field_validator("image_path", mode="before")
    @classmethod
    def _path(cls, value: object, info: ValidationInfo) -> Path | str:
        return _path_input(value, mode=info.mode)

    @field_validator("image_sha256", mode="before")
    @classmethod
    def _hash(cls, value: object) -> str:
        return _clean_sha256(value, name="image_sha256")

    @field_validator("warnings", mode="before")
    @classmethod
    def _warnings(cls, value: object) -> list[str]:
        return _warning_texts(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        left, top, right, bottom = self.crop_bbox_px
        if not (
            0 <= left < right <= self.page.width
            and 0 <= top < bottom <= self.page.height
        ):
            raise ValueError("crop_bbox_px 必须在页面内且非零")
        if self.width != right - left or self.height != bottom - top:
            raise ValueError("裁剪宽高与 crop_bbox_px 不一致")
        if self.region.page != self.page.page:
            raise ValueError("裁剪区域页码与页面证据不一致")
        expected_region_id = _region_id(
            doc_id=self.page.doc_id,
            version_id=self.page.version_id,
            page=self.page.page,
            detector_id=self.region.detector_id,
            detector_revision=self.region.detector_revision,
            label=self.region.label,
            kind=self.region.kind,
            bbox_px=self.region.bbox_px,
        )
        if self.region.region_id != expected_region_id:
            raise ValueError("region_id 与页面/检测器/几何绑定不一致")
        a, b, c, d, e, f = self.crop_to_page
        if (a, b, d, e) != (1.0, 0.0, 0.0, 1.0):
            raise ValueError("crop_to_page 必须是轴对齐平移")
        if c != float(left) or f != float(top):
            raise ValueError("crop_to_page 平移与裁剪框不一致")
        return self


_KIND_BY_LABEL: dict[str, RegionKind] = {
    "abstract": "text",
    "aside_text": "text",
    "content": "text",
    "footnote": "text",
    "number": "text",
    "reference": "text",
    "reference_content": "text",
    "text": "text",
    "vertical_text": "text",
    "vision_footnote": "text",
    "doc_title": "title",
    "figure_title": "title",
    "paragraph_title": "title",
    "title": "title",
    "table": "table",
    "display_formula": "formula",
    "formula": "formula",
    "formula_number": "formula",
    "inline_formula": "formula",
    "chart": "figure",
    "figure": "figure",
    "image": "figure",
    "seal": "figure",
    "header": "header",
    "header_image": "header",
    "footer": "footer",
    "footer_image": "footer",
    "page_number": "footer",
    "page_footer": "footer",
}


def _kind_for_label(label: str) -> RegionKind:
    key = re.sub(r"[\s\-]+", "_", label.strip().lower())
    return _KIND_BY_LABEL.get(key, "unknown")


def _region_id(
    *,
    doc_id: str,
    version_id: str,
    page: int,
    detector_id: str,
    detector_revision: str,
    label: str,
    kind: str,
    bbox_px: Sequence[float],
) -> str:
    payload = {
        "doc_id": doc_id,
        "version_id": version_id,
        "page": page,
        "detector_id": detector_id,
        "detector_revision": detector_revision,
        "label": label,
        "kind": kind,
        "bbox_px": [float(value) for value in bbox_px],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (
        f"{doc_id}@{version_id}#ocr-page{page}"
        f"#region-{digest[:_REGION_ID_DIGEST_LENGTH]}"
    )


def _detection_payload(detection: LayoutDetection) -> dict[str, Any]:
    return detection.model_dump(mode="json")


def build_layout(
    page: PageImageEvidence,
    detections: Iterable[LayoutDetection | dict[str, Any]],
    *,
    detector_id: str,
    detector_revision: str,
    min_score: float = 0.3,
) -> LayoutPageResult:
    """把原始检测审计为确定性区域；不生成、不补框、不静默丢弃未知标签。"""

    page = _revalidated(PageImageEvidence, page)
    detector_id = _clean_text(detector_id, name="detector_id")
    detector_revision = _clean_text(detector_revision, name="detector_revision")
    threshold = _score_float(min_score)
    warnings: list[str] = []
    rejected: list[dict[str, Any]] = []
    candidates: list[tuple[str, int, LayoutDetection, RegionKind]] = []
    review = False

    try:
        iterator = iter(detections)
    except TypeError as exc:
        raise ValueError("detections 必须是可迭代对象") from exc

    for index, raw in enumerate(iterator):
        try:
            detection = _revalidated(LayoutDetection, raw)
        except ValidationError as exc:
            rejected.append(
                {
                    "index": index,
                    "reason": "invalid_detection",
                    "error": _safe_error(exc),
                }
            )
            warnings.append(f"layout_invalid_detection: index {index}")
            review = True
            continue
        if detection.score < threshold:
            rejected.append(
                {
                    "index": index,
                    "reason": "below_min_score",
                    "detection": _detection_payload(detection),
                }
            )
            warnings.append(
                f"layout_low_score_detection: index {index} "
                f"score {detection.score:.6f} < {threshold:.6f}"
            )
            continue
        x0, y0, x1, y1 = detection.bbox_px
        if x1 <= x0 or y1 <= y0:
            rejected.append(
                {
                    "index": index,
                    "reason": "reversed_or_zero_bbox",
                    "detection": _detection_payload(detection),
                }
            )
            warnings.append(f"layout_reversed_or_zero_bbox: index {index}")
            review = True
            continue
        if x1 <= 0 or y1 <= 0 or x0 >= page.width or y0 >= page.height:
            rejected.append(
                {
                    "index": index,
                    "reason": "outside_page_bbox",
                    "detection": _detection_payload(detection),
                }
            )
            warnings.append(f"layout_outside_page_bbox: index {index}")
            review = True
            continue
        kind = _kind_for_label(detection.label)
        if kind == "unknown":
            warnings.append(
                f"layout_unknown_label: index {index} label {detection.label!r}"
            )
            review = True
        if kind in {"header", "footer"}:
            warnings.append(
                f"layout_header_footer_detection: index {index} kind {kind}"
            )
        if (
            x0 < 0.0
            or y0 < 0.0
            or x1 > float(page.width)
            or y1 > float(page.height)
        ):
            warnings.append(f"layout_partial_outside_bbox: index {index}")
            review = True
        region_id = _region_id(
            doc_id=page.doc_id,
            version_id=page.version_id,
            page=page.page,
            detector_id=detector_id,
            detector_revision=detector_revision,
            label=detection.label,
            kind=kind,
            bbox_px=detection.bbox_px,
        )
        candidates.append((region_id, index, detection, kind))

    kept: dict[str, tuple[str, int, LayoutDetection, RegionKind]] = {}
    dropped: list[tuple[int, LayoutDetection, str, str]] = []
    for region_id, index, detection, kind in candidates:
        existing = kept.get(region_id)
        if existing is None:
            kept[region_id] = (region_id, index, detection, kind)
            continue
        existing_score = existing[2].score
        if detection.score > existing_score:
            dropped.append(
                (existing[1], existing[2], existing[0], "replaced_lower_score")
            )
            kept[region_id] = (region_id, index, detection, kind)
        else:
            dropped.append((index, detection, region_id, "duplicate_region_id"))
    for index, detection, region_id, reason in dropped:
        rejected.append(
            {
                "index": index,
                "reason": reason,
                "region_id": region_id,
                "detection": _detection_payload(detection),
            }
        )
        warnings.append(f"layout_duplicate_detection: index {index} {reason}")

    regions: list[LayoutRegion] = []
    for region_id, _index, detection, kind in kept.values():
        region_warnings: list[str] = []
        if kind == "unknown":
            region_warnings.append(
                f"unknown_label_requires_review: {detection.label!r}"
            )
        if kind in {"header", "footer"}:
            region_warnings.append(
                "nonfactual_header_footer_detection: 仅作审计，不视为正文事实"
            )
        x0, y0, x1, y1 = detection.bbox_px
        if (
            x0 < 0.0
            or y0 < 0.0
            or x1 > float(page.width)
            or y1 > float(page.height)
        ):
            region_warnings.append("bbox_partially_outside_page: 裁剪时需显式审计")
        regions.append(
            LayoutRegion(
                region_id=region_id,
                page=page.page,
                label=detection.label,
                kind=kind,
                score=detection.score,
                bbox_px=list(detection.bbox_px),
                order=detection.order,
                detector_id=detector_id,
                detector_revision=detector_revision,
                warnings=region_warnings,
            )
        )

    regions.sort(key=lambda region: (region.order, region.label, region.bbox_px))
    if not regions:
        warnings.append("layout_no_detections: 没有满足阈值的区域，必须复核")
        review = True
    elif all(region.kind in {"header", "footer"} for region in regions):
        warnings.append("layout_only_header_footer_regions: 没有正文内容区域")
        review = True
    if len({region.order for region in regions}) != len(regions):
        warnings.append("layout_duplicate_order: 检测 order 不唯一，已保留原值")

    rejected.sort(key=lambda item: int(item.get("index", -1)))
    return LayoutPageResult(
        page=page,
        regions=regions,
        rejected_detections=rejected,
        detector_id=detector_id,
        detector_revision=detector_revision,
        warnings=_unique_warnings(warnings),
        needs_review=review,
    )


def _validate_region_binding(page: PageImageEvidence, region: LayoutRegion) -> None:
    if region.page != page.page:
        raise LayoutValidationError("region 页码与页面证据不一致")
    expected = _region_id(
        doc_id=page.doc_id,
        version_id=page.version_id,
        page=page.page,
        detector_id=region.detector_id,
        detector_revision=region.detector_revision,
        label=region.label,
        kind=region.kind,
        bbox_px=region.bbox_px,
    )
    if region.region_id != expected:
        raise LayoutValidationError("region_id 与页面/检测器/几何绑定不一致")


def _crop_png_bytes(page: PageImageEvidence, box: tuple[int, int, int, int]) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise LayoutInferenceError("裁剪实际 PNG 需要本地 Pillow") from exc
    try:
        with Image.open(page.image_path) as image:
            if image.format != "PNG":
                raise LayoutValidationError("页面证据不是可解码 PNG")
            if image.size != (page.width, page.height):
                raise LayoutValidationError("页面 PNG 尺寸与证据不一致")
            image.load()
            cropped = image.crop(box)
            if cropped.size != (box[2] - box[0], box[3] - box[1]):
                raise LayoutValidationError("实际裁剪尺寸与请求不一致")
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG")
    except LayoutValidationError:
        raise
    except Exception as exc:
        raise LayoutValidationError(
            f"无法裁剪页面 PNG: {_safe_error(exc)}"
        ) from exc
    return buffer.getvalue()


def _write_atomic_crop(output_dir: Path, image_sha256: str, data: bytes) -> Path:
    directory = _path_value(output_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LayoutValidationError(
            f"无法创建裁剪输出目录: {_safe_error(exc)}"
        ) from exc
    if not directory.is_dir():
        raise LayoutValidationError("裁剪输出路径不是目录")
    target = directory / f"crop-{image_sha256}.png"
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise LayoutValidationError(
                f"无法读取已有裁剪文件: {_safe_error(exc)}"
            ) from exc
        if hashlib.sha256(existing).hexdigest() != image_sha256:
            raise LayoutValidationError("内容寻址裁剪路径已存在但内容不一致")
        return target
    descriptor, temporary = tempfile.mkstemp(
        prefix=".crop-", suffix=".part", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise LayoutValidationError(
            f"无法原子写入裁剪文件: {_safe_error(exc)}"
        ) from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def crop_layout_region(
    page: PageImageEvidence,
    region: LayoutRegion,
    *,
    output_dir: Path,
    padding_px: int = 4,
) -> RegionCrop:
    """按整数像素向外扩展并裁剪真实 PNG；不缩放、不补造单元格坐标。"""

    page = _revalidated(PageImageEvidence, page)
    region = _revalidated(LayoutRegion, region)
    if isinstance(padding_px, bool) or not isinstance(padding_px, int) or padding_px < 0:
        raise ValueError("padding_px 必须是非负整数（bool 不接受）")
    _validate_region_binding(page, region)

    x0, y0, x1, y1 = region.bbox_px
    if x1 <= x0 or y1 <= y0:
        raise LayoutValidationError("裁剪区域 bbox 为零面积或方向反转")
    if x1 <= 0.0 or y1 <= 0.0 or x0 >= page.width or y0 >= page.height:
        raise LayoutValidationError("裁剪区域完全位于页面之外")

    requested = [
        math.floor(x0) - padding_px,
        math.floor(y0) - padding_px,
        math.ceil(x1) + padding_px,
        math.ceil(y1) + padding_px,
    ]
    left = max(0, requested[0])
    top = max(0, requested[1])
    right = min(page.width, requested[2])
    bottom = min(page.height, requested[3])
    if right <= left or bottom <= top:
        raise LayoutValidationError("裁剪区域在页面边界内没有有效像素")
    warnings: list[str] = []
    if [left, top, right, bottom] != requested:
        warnings.append(
            "crop_clipped_to_page_boundary: "
            f"requested {requested} clipped {[left, top, right, bottom]}"
        )

    page.verify_current()
    crop_bytes = _crop_png_bytes(page, (left, top, right, bottom))
    page.verify_current()
    crop_sha256 = hashlib.sha256(crop_bytes).hexdigest()
    image_path = _write_atomic_crop(output_dir, crop_sha256, crop_bytes)
    return RegionCrop(
        page=page,
        region=region,
        crop_bbox_px=[left, top, right, bottom],
        image_path=image_path,
        image_sha256=crop_sha256,
        width=right - left,
        height=bottom - top,
        crop_to_page=[1.0, 0.0, float(left), 0.0, 1.0, float(top)],
        warnings=warnings,
    )


def map_crop_bbox_to_page(
    crop: RegionCrop,
    bbox: Sequence[float],
    *,
    unit: Literal["pixels", "normalized"] = "pixels",
) -> list[float]:
    """把裁剪内 bbox 映射回原页面像素；归一化输入按裁剪宽高展开。"""

    crop = _revalidated(RegionCrop, crop)
    if unit not in {_PIXEL_UNIT, _NORMALIZED_UNIT}:
        raise ValueError("unit 必须是 'pixels' 或 'normalized'")
    x0, y0, x1, y1 = _bbox_values(bbox, name="bbox")
    if x0 > x1 or y0 > y1:
        raise LayoutValidationError("bbox 方向反转")
    if unit == _PIXEL_UNIT:
        if (
            x0 < 0.0
            or y0 < 0.0
            or x1 > float(crop.width)
            or y1 > float(crop.height)
        ):
            raise LayoutValidationError("bbox 超出裁剪图像范围")
        pixels = [x0, y0, x1, y1]
    else:
        if any(value < 0.0 or value > 1.0 for value in (x0, y0, x1, y1)):
            raise LayoutValidationError("归一化 bbox 必须在 [0, 1] 内")
        pixels = [
            x0 * crop.width,
            y0 * crop.height,
            x1 * crop.width,
            y1 * crop.height,
        ]
    a, b, c, d, e, f = crop.crop_to_page
    mapped = [
        a * pixels[0] + b * pixels[1] + c,
        d * pixels[0] + e * pixels[1] + f,
        a * pixels[2] + b * pixels[3] + c,
        d * pixels[2] + e * pixels[3] + f,
    ]
    if not all(math.isfinite(value) for value in mapped):
        raise LayoutValidationError("bbox 映射结果必须是有限数")
    return [float(value) for value in mapped]


def _cpu_value(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    return value


def _cpu_sequence(value: Any) -> list[Any]:
    value = _cpu_value(value)
    if hasattr(value, "tolist"):
        value = value.tolist()
    try:
        return list(value)
    except TypeError as exc:
        raise LayoutInferenceError("模型后处理输出不是序列") from exc


def _cpu_scalar(value: Any) -> Any:
    value = _cpu_value(value)
    if hasattr(value, "item"):
        return value.item()
    return value


def _cpu_float(value: Any, *, name: str) -> float:
    scalar = _cpu_scalar(value)
    if isinstance(scalar, bool) or not isinstance(scalar, (int, float)):
        raise LayoutInferenceError(f"{name} 不是数值")
    number = float(scalar)
    if not math.isfinite(number):
        raise LayoutInferenceError(f"{name} 不是有限数")
    return number


def _cpu_int(value: Any, *, name: str) -> int:
    scalar = _cpu_scalar(value)
    if isinstance(scalar, bool):
        raise LayoutInferenceError(f"{name} 不是整数")
    if isinstance(scalar, int):
        return scalar
    if isinstance(scalar, float) and scalar.is_integer():
        return int(scalar)
    raise LayoutInferenceError(f"{name} 不是整数")


def _model_polygon(value: Any) -> list[list[float]] | None:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        return None
    points: list[list[float]] = []
    for point in value:
        if hasattr(point, "tolist"):
            point = point.tolist()
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        try:
            x = float(point[0])
            y = float(point[1])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        points.append([x, y])
    return points if len(points) >= 3 else None


class PPDocLayoutV3Detector:
    """本地 PP-DocLayoutV3 Transformers 检测器；模型按需懒加载且仅离线读取。"""

    detector_id = _PP_DOCLAYOUT_V3_DETECTOR_ID

    def __init__(
        self,
        model_path: Path,
        model_revision: str,
        device: str = "cpu",
        min_score: float = 0.3,
    ) -> None:
        self.model_path = _path_value(model_path)
        self.model_revision = _clean_text(
            model_revision, name="model_revision"
        )
        self.device = _clean_text(device, name="device", max_length=64)
        self.min_score = _score_float(min_score)
        self._processor: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: Any = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.is_dir():
            raise LayoutInferenceError(
                f"本地布局模型目录不存在: {self.model_path}"
            )
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModelForObjectDetection
        except ImportError as exc:
            raise LayoutInferenceError(
                "布局检测需要本地 torch 与 transformers"
            ) from exc
        try:
            processor = AutoImageProcessor.from_pretrained(
                str(self.model_path),
                revision=self.model_revision,
                local_files_only=True,
                trust_remote_code=False,
            )
            model = AutoModelForObjectDetection.from_pretrained(
                str(self.model_path),
                revision=self.model_revision,
                local_files_only=True,
                trust_remote_code=False,
            )
            if not callable(
                getattr(processor, "post_process_object_detection", None)
            ):
                raise LayoutInferenceError(
                    "本地图像处理器缺少 post_process_object_detection"
                )
            device = torch.device(self.device)
            model.to(device)
            model.eval()
        except LayoutInferenceError:
            raise
        except Exception as exc:
            raise LayoutInferenceError(
                f"加载本地布局模型失败: {_safe_error(exc)}"
            ) from exc
        self._processor = processor
        self._model = model
        self._torch = torch
        self._device = device

    def _detections_from_result(
        self, result: Any
    ) -> tuple[list[LayoutDetection], list[str]]:
        if not isinstance(result, dict):
            raise LayoutInferenceError("布局后处理结果不是对象")
        for key in ("scores", "labels", "boxes", "order_seq"):
            if key not in result:
                raise LayoutInferenceError(f"布局后处理结果缺少 {key}")
        scores = _cpu_sequence(result["scores"])
        labels = _cpu_sequence(result["labels"])
        boxes = _cpu_sequence(result["boxes"])
        orders = _cpu_sequence(result["order_seq"])
        count = len(scores)
        if len(labels) != count or len(boxes) != count or len(orders) != count:
            raise LayoutInferenceError("布局后处理字段长度不一致")
        polygons_raw = result.get("polygon_points")
        warnings: list[str] = []
        if polygons_raw is None:
            warnings.append("layout_polygon_metadata_missing: 后处理未提供 polygon")
        else:
            try:
                polygon_count = len(polygons_raw)
            except TypeError:
                polygon_count = -1
            if polygon_count != count:
                warnings.append(
                    "layout_polygon_metadata_mismatch: polygon 数量与检测不一致"
                )
                polygons_raw = None
        id2label = getattr(getattr(self._model, "config", None), "id2label", None)
        if not isinstance(id2label, dict):
            id2label = {}
        detections: list[LayoutDetection] = []
        for index in range(count):
            try:
                score = _cpu_float(scores[index], name=f"布局分数 {index}")
                label_id = _cpu_int(labels[index], name=f"布局标签 {index}")
                order = _cpu_int(orders[index], name=f"布局顺序 {index}")
                box = [
                    _cpu_float(item, name=f"布局框 {index}")
                    for item in _cpu_sequence(boxes[index])
                ]
            except (TypeError, ValueError) as exc:
                raise LayoutInferenceError(
                    f"布局后处理字段 {index} 无法转换为数值: "
                    f"{_safe_error(exc)}"
                ) from exc
            if len(box) != 4:
                raise LayoutInferenceError(f"布局框 {index} 不是有限 xyxy")
            if order < 0:
                raise LayoutInferenceError(f"布局顺序 {index} 为负数")
            label_value = id2label.get(label_id, id2label.get(str(label_id)))
            if label_value is None:
                label = f"label_{label_id}"
                warnings.append(
                    f"layout_label_metadata_missing: label id {label_id}"
                )
            else:
                label = str(label_value)
            polygon = None
            if polygons_raw is not None:
                polygon = _model_polygon(polygons_raw[index])
                if polygon is None:
                    warnings.append(
                        f"layout_polygon_metadata_unavailable: index {index}"
                    )
            try:
                detections.append(
                    LayoutDetection(
                        label=label,
                        score=score,
                        bbox_px=box,
                        order=order,
                        polygon=polygon,
                    )
                )
            except ValidationError as exc:
                raise LayoutInferenceError(
                    f"布局检测 {index} 不符合严格接口: {_safe_error(exc)}"
                ) from exc
        return detections, warnings

    def detect(self, page: PageImageEvidence) -> LayoutPageResult:
        page = _revalidated(PageImageEvidence, page)
        page.verify_current()
        self._ensure_loaded()
        try:
            from PIL import Image
        except ImportError as exc:
            raise LayoutInferenceError("布局检测需要本地 Pillow") from exc
        try:
            with Image.open(page.image_path) as image:
                if image.format != "PNG":
                    raise LayoutValidationError("页面证据不是可解码 PNG")
                if image.size != (page.width, page.height):
                    raise LayoutValidationError("页面 PNG 尺寸与证据不一致")
                image.load()
                rgb = image.convert("RGB")
        except LayoutValidationError:
            raise
        except Exception as exc:
            raise LayoutValidationError(
                f"无法读取页面 PNG: {_safe_error(exc)}"
            ) from exc
        page.verify_current()

        try:
            inputs = self._processor(images=rgb, return_tensors="pt")
            if hasattr(inputs, "to"):
                inputs = inputs.to(self._device)
            with self._torch.inference_mode():
                outputs = self._model(**inputs)
            results = self._processor.post_process_object_detection(
                outputs,
                threshold=min(self.min_score, _POSTPROCESS_AUDIT_FLOOR),
                target_sizes=[(page.height, page.width)],
            )
        except Exception as exc:
            raise LayoutInferenceError(
                f"本地布局推理失败: {_safe_error(exc)}"
            ) from exc
        if not isinstance(results, (list, tuple)) or len(results) != 1:
            raise LayoutInferenceError("布局后处理未返回单页结果")
        detections, conversion_warnings = self._detections_from_result(results[0])
        result = build_layout(
            page,
            detections,
            detector_id=self.detector_id,
            detector_revision=self.model_revision,
            min_score=self.min_score,
        )
        result.warnings = _unique_warnings(
            [*result.warnings, *conversion_warnings]
        )
        return result


__all__ = [
    "LayoutDetection",
    "LayoutInferenceError",
    "LayoutPageResult",
    "LayoutRegion",
    "LayoutValidationError",
    "PPDocLayoutV3Detector",
    "PageImageEvidence",
    "RegionCrop",
    "build_layout",
    "crop_layout_region",
    "map_crop_bbox_to_page",
]
