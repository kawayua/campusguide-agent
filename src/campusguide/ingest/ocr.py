"""扫描版 PDF 的 OCR 旁路。

原始 PDF 保持不变；逐页渲染后交给 RapidOCR，结果缓存为 JSON。
`parse_corpus` 只在 PDF 文本层为空时读取该缓存，因此普通 PDF 与离线测试
都不依赖 OCR 运行时。

安装可选依赖：

    uv sync --group ocr

运行：

    uv run --group ocr python scripts/ocr_scans.py --only info-grad-cs-plan@v2020
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

OCR_SCHEMA_VERSION = "1.1"
OCR_MODEL = "rapidocr-onnxruntime"
DEFAULT_DPI = 240
OCR_MAX_SIDE_LEN = 4000
OCR_DET_LIMIT_SIDE_LEN = 1600
OCR_MIN_CJK = 50


class OcrPassConfig(NamedTuple):
    dpi: int
    max_side_len: int
    det_limit_side_len: int


# 两份 2020 级扫描件逐页对比后没有任何单一配置全面占优：200dpi 默认
# 保留了数学方案第 1 页的三行公式性表述，240dpi 全分辨率保留了该方案
# 第 4 页的“前沿讲座”整行，但两者各会在别的页面丢行。默认跑两个 pass，
# 逐页取字符覆盖更多的一页，避免把整行要求静默漏掉。
OCR_PASSES: tuple[OcrPassConfig, ...] = (
    OcrPassConfig(dpi=200, max_side_len=2000, det_limit_side_len=736),
    OcrPassConfig(
        dpi=240,
        max_side_len=OCR_MAX_SIDE_LEN,
        det_limit_side_len=OCR_DET_LIMIT_SIDE_LEN,
    ),
)


class OcrUnavailableError(RuntimeError):
    """未安装可选 OCR 依赖。"""


class OcrLine(BaseModel):
    text: str
    confidence: float
    box: list[list[float]] | None = None


class OcrPage(BaseModel):
    page_no: int
    text: str
    lines: list[OcrLine] = Field(default_factory=list)
    mean_confidence: float = 0.0
    dpi: int = DEFAULT_DPI
    max_side_len: int = OCR_MAX_SIDE_LEN
    det_limit_side_len: int = OCR_DET_LIMIT_SIDE_LEN
    width: int = 0
    height: int = 0
    content_chars: int = 0
    grafted_lines: int = 0
    warnings: list[str] = Field(default_factory=list)


class OcrDocument(BaseModel):
    schema_version: str = OCR_SCHEMA_VERSION
    doc_id: str
    version_id: str
    source_sha256: str
    model: str = OCR_MODEL
    model_version: str | None = None
    dpi: int = DEFAULT_DPI
    max_side_len: int = OCR_MAX_SIDE_LEN
    det_limit_side_len: int = OCR_DET_LIMIT_SIDE_LEN
    selection: str = "single-pass"
    generated_at: str
    page_count: int
    mean_confidence: float = 0.0
    pages: list[OcrPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def ocr_output_path(ocr_dir: Path, doc_id: str, version_id: str) -> Path:
    return ocr_dir / doc_id / f"{version_id}.json"


def load_ocr_document(path: Path) -> OcrDocument:
    return OcrDocument.model_validate_json(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _box_key(box: list[list[float]]) -> tuple[float, float, float]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return min(ys), max(ys), min(xs)


def _reading_order(
    raw: list[list[object]] | None,
) -> list[tuple[str, float, list[list[float]]]]:
    """把 OCR 框按“先上后下、同一行从左到右”排序。

    RapidOCR 通常已经按阅读顺序返回；这里再做一次确定性排序，避免
    不同版本或并行检测造成行序漂移。同一视觉行的多个框（例如表格
    单元格）按纵向重叠合并，再从左到右拼接；不能按固定像素间距合并，
    否则字号和行距不同的相邻文本行会被误并成一行。
    """
    entries: list[dict[str, object]] = []
    for item in raw or []:
        if len(item) < 3:
            continue
        box = item[0]
        text = str(item[1]).strip()
        if not text or not isinstance(box, list) or len(box) < 3:
            continue
        try:
            top, bottom, left = _box_key([[float(x), float(y)] for x, y in box])
            confidence = float(item[2])
        except (TypeError, ValueError):
            continue
        entries.append(
            {
                "top": top,
                "bottom": bottom,
                "left": left,
                "text": text,
                "confidence": confidence,
                "box": [[float(x), float(y)] for x, y in box],
            }
        )

    entries.sort(key=lambda entry: (float(entry["top"]), float(entry["left"])))
    groups: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    current_top = 0.0
    current_bottom = 0.0
    for entry in entries:
        top = float(entry["top"])
        bottom = float(entry["bottom"])
        same_row = False
        if current:
            overlap = min(current_bottom, bottom) - max(current_top, top)
            shorter = min(current_bottom - current_top, bottom - top)
            same_row = shorter > 0 and overlap >= shorter * 0.5
        if current and not same_row:
            groups.append(current)
            current = []
        if not current:
            current_top = top
            current_bottom = bottom
        else:
            current_top = min(current_top, top)
            current_bottom = max(current_bottom, bottom)
        current.append(entry)
    if current:
        groups.append(current)

    lines: list[tuple[str, float, list[list[float]]]] = []
    for group in groups:
        group.sort(key=lambda entry: float(entry["left"]))
        text = " ".join(str(entry["text"]) for entry in group)
        confidence = sum(float(entry["confidence"]) for entry in group) / len(group)
        box = group[0]["box"]
        lines.append((text, confidence, box))  # type: ignore[arg-type]
    return lines


def _load_runtime() -> tuple[Any, Any, str | None]:
    try:
        import pymupdf
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - 由可选依赖环境触发
        raise OcrUnavailableError("未安装 OCR 依赖；请先运行 `uv sync --group ocr`") from exc

    try:
        model_version = importlib.metadata.version(OCR_MODEL)
    except importlib.metadata.PackageNotFoundError:
        model_version = None
    return pymupdf, RapidOCR, model_version


def _content_chars(text: str) -> int:
    """统计中英文数字字符数，作为逐页 OCR 覆盖面的比较指标。"""
    return len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", text))


def _line_center(line: OcrLine, page: OcrPage) -> float:
    if not line.box or not page.height:
        return 0.0
    tops = [point[1] for point in line.box]
    return ((min(tops) + max(tops)) / 2) / page.height


def _line_height(line: OcrLine, page: OcrPage) -> float:
    if not line.box or not page.height:
        return 0.0
    tops = [point[1] for point in line.box]
    return (max(tops) - min(tops)) / page.height


def _rescale_line(line: OcrLine, source: OcrPage, target: OcrPage) -> OcrLine:
    if (
        not line.box
        or not source.width
        or not source.height
        or not target.width
        or not target.height
    ):
        return line
    scale_x = target.width / source.width
    scale_y = target.height / source.height
    if abs(scale_x - 1) < 1e-6 and abs(scale_y - 1) < 1e-6:
        return line
    box = [[x * scale_x, y * scale_y] for x, y in line.box]
    return line.model_copy(update={"box": box})


def _merge_ocr_pages(candidates: list[OcrPage]) -> OcrPage:
    """以覆盖最多的一页为底稿，逐行补齐其他 pass 独有的整行。

    只补“底稿在该纵向位置没有文字”的行；同一位置的不同识别变体保留
    底稿。行距用页高中位数归一化，避免不同 DPI 的像素坐标无法比较。
    """
    if not candidates:
        raise ValueError("没有可合并的 OCR 页面")
    base = max(
        candidates,
        key=lambda page: (page.content_chars, page.mean_confidence),
    )
    if len(candidates) == 1 or not base.lines:
        return base

    base_centers = [_line_center(line, base) for line in base.lines]
    heights = [height for line in base.lines if (height := _line_height(line, base)) > 0]
    median_height = median(heights) if heights else 0.0
    tolerance = min(0.0045, max(0.0015, median_height * 0.5))

    inserted: list[tuple[float, OcrLine, OcrPage]] = []
    for candidate in candidates:
        if candidate is base:
            continue
        for line in candidate.lines:
            if not line.box or _content_chars(line.text) == 0:
                continue
            center = _line_center(line, candidate)
            if any(abs(center - base_center) <= tolerance for base_center in base_centers):
                continue
            duplicate = next(
                (
                    (index, item)
                    for index, item in enumerate(inserted)
                    if abs(center - item[0]) <= tolerance
                ),
                None,
            )
            if duplicate is None:
                inserted.append((center, line, candidate))
                continue
            index, (_, existing, _) = duplicate
            if _content_chars(line.text) > _content_chars(existing.text):
                inserted[index] = (center, line, candidate)
    if not inserted:
        return base

    rows = [(_line_center(line, base), line) for line in base.lines]
    rows.extend((center, _rescale_line(line, source, base)) for center, line, source in inserted)
    rows.sort(key=lambda row: row[0])
    lines = [line for _, line in rows]
    text = "\n".join(line.text for line in lines).strip()
    mean = sum(line.confidence for line in lines) / len(lines)
    return base.model_copy(
        update={
            "text": text,
            "lines": lines,
            "mean_confidence": mean,
            "content_chars": _content_chars(text),
            "grafted_lines": len(inserted),
        }
    )


def _ocr_page(
    page: Any,
    engine: Any,
    config: OcrPassConfig,
    page_no: int,
) -> OcrPage:
    pixmap = page.get_pixmap(dpi=config.dpi)
    raw, _ = engine(pixmap.tobytes("png"))
    lines = _reading_order(raw)
    text = "\n".join(line[0] for line in lines).strip()
    page_confidences = [line[1] for line in lines]
    mean = sum(page_confidences) / len(page_confidences) if page_confidences else 0.0
    page_warnings: list[str] = []
    if not lines:
        page_warnings.append("本页未识别到文字")
    elif mean < 0.80:
        page_warnings.append(f"本页 OCR 平均置信度偏低：{mean:.3f}")
    return OcrPage(
        page_no=page_no,
        text=text,
        lines=[OcrLine(text=line[0], confidence=line[1], box=line[2]) for line in lines],
        mean_confidence=mean,
        dpi=config.dpi,
        max_side_len=config.max_side_len,
        det_limit_side_len=config.det_limit_side_len,
        width=pixmap.width,
        height=pixmap.height,
        content_chars=_content_chars(text),
        warnings=page_warnings,
    )


def _build_document(
    path: Path,
    *,
    doc_id: str,
    version_id: str,
    pages: list[OcrPage],
    warnings: list[str],
    model_version: str | None,
    config: OcrPassConfig,
    selection: str,
) -> OcrDocument:
    all_confidences = [line.confidence for page in pages for line in page.lines]
    overall = sum(all_confidences) / len(all_confidences) if all_confidences else 0.0
    warnings = list(warnings)
    if overall < 0.85:
        warnings.append(f"OCR 总体平均置信度偏低：{overall:.3f}，建议人工抽查")
    if pages and sum(1 for page in pages if not page.text) >= 2:
        warnings.append("超过一页未识别到文字，请检查 PDF 图像质量或语言设置")
    return OcrDocument(
        doc_id=doc_id,
        version_id=version_id,
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        model=OCR_MODEL,
        model_version=model_version,
        dpi=config.dpi,
        max_side_len=config.max_side_len,
        det_limit_side_len=config.det_limit_side_len,
        selection=selection,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        page_count=len(pages),
        mean_confidence=overall,
        pages=pages,
        warnings=warnings,
    )


def ocr_pdf(
    path: Path,
    *,
    doc_id: str,
    version_id: str,
    dpi: int = DEFAULT_DPI,
    max_side_len: int = OCR_MAX_SIDE_LEN,
    det_limit_side_len: int = OCR_DET_LIMIT_SIDE_LEN,
    on_page: Callable[[int, int], None] | None = None,
) -> OcrDocument:
    """逐页 OCR 一个 PDF（单一配置）；需要安装 `ocr` 依赖组。"""
    pymupdf, rapid_ocr, model_version = _load_runtime()
    config = OcrPassConfig(
        dpi=dpi,
        max_side_len=max_side_len,
        det_limit_side_len=det_limit_side_len,
    )
    document = pymupdf.open(path)
    engine = rapid_ocr(
        max_side_len=config.max_side_len,
        det_limit_side_len=config.det_limit_side_len,
    )
    pages: list[OcrPage] = []
    warnings: list[str] = []
    try:
        for page_no, page in enumerate(document, start=1):
            result = _ocr_page(page, engine, config, page_no)
            pages.append(result)
            warnings.extend(f"第 {page_no} 页：{warning}" for warning in result.warnings)
            if on_page is not None:
                on_page(page_no, len(document))
    finally:
        document.close()
    return _build_document(
        path,
        doc_id=doc_id,
        version_id=version_id,
        pages=pages,
        warnings=warnings,
        model_version=model_version,
        config=config,
        selection="single-pass",
    )


def ocr_pdf_multiscale(
    path: Path,
    *,
    doc_id: str,
    version_id: str,
    passes: tuple[OcrPassConfig, ...] = OCR_PASSES,
    on_page: Callable[[int, int], None] | None = None,
) -> OcrDocument:
    """多尺度逐页 OCR：以覆盖最多的 pass 为底稿，补齐另一 pass 的漏行。

    扫描件的字号、倾斜和压缩噪声会让某个 DPI 偶发漏掉整行；单一配置
    无法同时照顾所有页面。这里对每页跑多个 pass，先按识别到的中英文
    字符数选出底稿，再按归一化纵向位置补齐另一 pass 独有的整行，并把
    底稿配置记录在页级字段中。
    """
    if not passes:
        raise ValueError("至少需要一个 OCR pass")
    pymupdf, rapid_ocr, model_version = _load_runtime()
    document = pymupdf.open(path)
    engines = [
        (
            config,
            rapid_ocr(
                max_side_len=config.max_side_len,
                det_limit_side_len=config.det_limit_side_len,
            ),
        )
        for config in passes
    ]
    pages: list[OcrPage] = []
    warnings: list[str] = []
    try:
        for page_no, page in enumerate(document, start=1):
            candidates = [_ocr_page(page, engine, config, page_no) for config, engine in engines]
            merged = _merge_ocr_pages(candidates)
            pages.append(merged)
            warnings.extend(f"第 {page_no} 页：{warning}" for warning in merged.warnings)
            if merged.grafted_lines:
                warnings.append(f"第 {page_no} 页：从其他 pass 补齐 {merged.grafted_lines} 行")
            if on_page is not None:
                on_page(page_no, len(document))
    finally:
        document.close()

    pass_text = " / ".join(f"{config.dpi}dpi" for config in passes)
    chosen: dict[int, int] = {}
    for page in pages:
        chosen[page.dpi] = chosen.get(page.dpi, 0) + 1
    chosen_text = "、".join(f"{dpi}dpi×{count}" for dpi, count in sorted(chosen.items()))
    grafted = sum(page.grafted_lines for page in pages)
    warnings.append(
        f"多尺度 OCR：逐页比较 {pass_text} 渲染结果，以覆盖最多的 pass 为底稿，"
        f"逐行补齐另一 pass 独有的整行；底稿 {chosen_text}，共补齐 {grafted} 行。"
        "表格仍按 OCR 行序保留"
    )
    return _build_document(
        path,
        doc_id=doc_id,
        version_id=version_id,
        pages=pages,
        warnings=warnings,
        model_version=model_version,
        config=passes[-1],
        selection="multi-pass-line-merge",
    )


def write_ocr_document(ocr_dir: Path, document: OcrDocument) -> Path:
    path = ocr_output_path(ocr_dir, document.doc_id, document.version_id)
    _atomic_write_json(path, document.model_dump(mode="json"))
    return path
