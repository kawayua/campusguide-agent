from __future__ import annotations

import hashlib
import json
import math
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from campusguide.ingest.ocr_layout import (
    LayoutDetection,
    LayoutInferenceError,
    LayoutPageResult,
    LayoutRegion,
    LayoutValidationError,
    PageImageEvidence,
    PPDocLayoutV3Detector,
    RegionCrop,
    _region_id,
    build_layout,
    crop_layout_region,
    map_crop_bbox_to_page,
)
from campusguide.ingest.ocr_router import RoutedRegionResult
from campusguide.ingest.xiaomi_ocr import OcrUsage

SOURCE_SHA = "a" * 64
DETECTOR_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
DETECTOR_REVISION = "b" * 40


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, *, offset: int = 0) -> bytes:
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        for x in range(width):
            scanlines.extend(
                (
                    (x * 7 + offset) % 256,
                    (y * 11 + offset) % 256,
                    ((x + y) * 5 + offset) % 256,
                )
            )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + _png_chunk(b"IEND", b"")
    )


def _write_png(
    path: Path,
    *,
    width: int = 10,
    height: int = 10,
    offset: int = 0,
) -> str:
    data = _png(width, height, offset=offset)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _evidence(
    path: Path,
    *,
    page: int = 1,
    doc_id: str = "doc-1",
    version_id: str = "v1",
    source_format: str = "pdf",
) -> PageImageEvidence:
    return PageImageEvidence.from_png(
        doc_id=doc_id,
        version_id=version_id,
        source_format=source_format,
        source_sha256=SOURCE_SHA,
        page=page,
        image_path=path,
    )


def _page(
    tmp_path: Path,
    *,
    width: int = 10,
    height: int = 10,
    offset: int = 0,
    page: int = 1,
    doc_id: str = "doc-1",
    name: str = "page.png",
) -> PageImageEvidence:
    path = tmp_path / name
    _write_png(path, width=width, height=height, offset=offset)
    return _evidence(path, page=page, doc_id=doc_id)


def _detection(
    *,
    label: str = "text",
    score: float = 0.9,
    bbox: tuple[float, float, float, float] = (1.0, 1.0, 5.0, 5.0),
    order: int = 0,
    polygon: list[list[float]] | None = None,
) -> LayoutDetection:
    return LayoutDetection(
        label=label,
        score=score,
        bbox_px=list(bbox),
        order=order,
        polygon=polygon,
    )


def _build(
    page: PageImageEvidence,
    detections: list[LayoutDetection],
    *,
    min_score: float = 0.3,
) -> LayoutPageResult:
    return build_layout(
        page,
        detections,
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
        min_score=min_score,
    )


def _region_for(
    page: PageImageEvidence,
    *,
    bbox: tuple[float, float, float, float],
    label: str = "text",
    kind: str = "text",
    score: float = 0.9,
    order: int = 0,
) -> LayoutRegion:
    region_id = _region_id(
        doc_id=page.doc_id,
        version_id=page.version_id,
        page=page.page,
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
        label=label,
        kind=kind,
        bbox_px=bbox,
    )
    return LayoutRegion(
        region_id=region_id,
        page=page.page,
        label=label,
        kind=kind,
        score=score,
        bbox_px=list(bbox),
        order=order,
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
    )


def _crop_fixture(tmp_path: Path) -> tuple[PageImageEvidence, RegionCrop]:
    page = _page(tmp_path, width=20, height=15)
    result = _build(page, [_detection(bbox=(5.0, 4.0, 10.0, 9.0))])
    crop = crop_layout_region(
        page,
        result.regions[0],
        output_dir=tmp_path / "crops",
        padding_px=0,
    )
    return page, crop


def test_evidence_records_png_bytes_and_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "page.png"
    image_sha256 = _write_png(path, width=6, height=4)
    page = _evidence(path)

    assert page.width == 6
    assert page.height == 4
    assert page.image_sha256 == image_sha256
    assert page.image_path == path.resolve()
    assert page.page == 1
    assert page.source_format == "pdf"

    page.verify_current()
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(LayoutValidationError, match="证据"):
        page.verify_current()


def test_evidence_rejects_non_png_bad_crc_and_bad_fields(tmp_path: Path) -> None:
    text_path = tmp_path / "not.png"
    text_path.write_bytes(b"not a png")
    with pytest.raises(LayoutValidationError, match="不是 PNG"):
        _evidence(text_path)

    bad_crc = bytearray(_png(4, 3))
    bad_crc[30] ^= 0xFF
    crc_path = tmp_path / "bad-crc.png"
    crc_path.write_bytes(bytes(bad_crc))
    with pytest.raises(LayoutValidationError, match="CRC"):
        _evidence(crc_path)

    good_path = tmp_path / "good.png"
    _write_png(good_path, width=4, height=3)
    with pytest.raises(ValidationError):
        PageImageEvidence.from_png(
            doc_id="doc-1",
            version_id="v1",
            source_format="pdf",
            source_sha256=SOURCE_SHA,
            page=True,
            image_path=good_path,
        )
    with pytest.raises(ValidationError):
        PageImageEvidence.from_png(
            doc_id="doc-1",
            version_id="v1",
            source_format="docx",
            source_sha256=SOURCE_SHA,
            page=1,
            image_path=good_path,
        )
    page = _evidence(good_path)
    with pytest.raises(ValidationError):
        PageImageEvidence.model_validate(
            page.model_dump() | {"source_sha256": "not-a-hash"}
        )
    with pytest.raises(ValidationError):
        PageImageEvidence.model_validate(page.model_dump() | {"unexpected": True})


def test_detection_is_strict_about_numbers_and_shapes() -> None:
    detection = _detection(bbox=(0, 0, 2, 2), polygon=[[0, 0], [2, 0], [2, 2]])
    assert detection.bbox_px == [0.0, 0.0, 2.0, 2.0]
    assert detection.polygon == [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]]

    invalid_values = (
        {"score": True},
        {"score": 1.2},
        {"score": math.nan},
        {"score": math.inf},
        {"bbox_px": [0.0, 0.0, 1.0]},
        {"bbox_px": [0.0, 0.0, math.inf, 1.0]},
        {"bbox_px": [False, 0.0, 1.0, 1.0]},
        {"order": True},
        {"order": -1},
        {"polygon": [[0.0, 0.0], [1.0, 1.0]]},
        {"unexpected": 1},
    )
    for overrides in invalid_values:
        values = {
            "label": "text",
            "score": 0.5,
            "bbox_px": [0.0, 0.0, 1.0, 1.0],
            "order": 0,
        }
        values.update(overrides)
        with pytest.raises(ValidationError):
            LayoutDetection.model_validate(values)


def test_region_ids_are_stable_page_scoped_and_not_positional(
    tmp_path: Path,
) -> None:
    page1 = _page(tmp_path, name="page1.png")
    page2 = _page(tmp_path, name="page2.png", page=2, offset=3)
    page1_other_doc = _evidence(
        tmp_path / "page1.png", doc_id="doc-2"
    )
    detections = [
        _detection(label="text", bbox=(1.0, 1.0, 5.0, 4.0), order=0),
        _detection(label="table", bbox=(5.0, 5.0, 9.0, 9.0), order=1),
    ]

    first = _build(page1, detections)
    reordered = _build(page1, list(reversed(detections)))
    renumbered = _build(
        page1,
        [
            _detection(label="text", bbox=(1.0, 1.0, 5.0, 4.0), order=7),
            _detection(label="table", bbox=(5.0, 5.0, 9.0, 9.0), order=8),
        ],
    )
    second_page = _build(
        page2,
        [_detection(label="text", bbox=(1.0, 1.0, 5.0, 4.0), order=0)],
    )
    other_doc = _build(
        page1_other_doc,
        [_detection(label="text", bbox=(1.0, 1.0, 5.0, 4.0), order=0)],
    )

    def by_label(result: LayoutPageResult) -> dict[str, str]:
        return {region.label: region.region_id for region in result.regions}

    ids = by_label(first)
    assert ids == by_label(reordered) == by_label(renumbered)
    assert len(set(ids.values())) == 2
    assert all("#ocr-page1#region-" in region_id for region_id in ids.values())
    assert second_page.regions[0].region_id not in ids.values()
    assert other_doc.regions[0].region_id not in ids.values()
    assert first.page.page == 1


def test_unknown_label_is_kept_but_requires_review(tmp_path: Path) -> None:
    page = _page(tmp_path)
    result = _build(
        page,
        [
            _detection(label="dean_stamp", score=0.8, bbox=(1.0, 1.0, 4.0, 4.0)),
            _detection(label="text", score=0.9, bbox=(1.0, 5.0, 8.0, 9.0), order=1),
        ],
    )

    assert result.needs_review is True
    assert [region.kind for region in result.regions] == ["unknown", "text"]
    unknown = result.regions[0]
    assert unknown.label == "dean_stamp"
    assert any("unknown_label_requires_review" in item for item in unknown.warnings)
    assert any("layout_unknown_label" in item for item in result.warnings)
    assert result.rejected_detections == []


def test_controls_without_unknown_or_anomaly_need_no_review(tmp_path: Path) -> None:
    page = _page(tmp_path)
    result = _build(
        page,
        [
            _detection(label="paragraph_title", bbox=(1.0, 1.0, 8.0, 2.0)),
            _detection(label="text", bbox=(1.0, 3.0, 8.0, 8.0), order=1),
        ],
    )

    assert result.needs_review is False
    assert [region.kind for region in result.regions] == ["title", "text"]
    assert not any("layout_unknown_label" in item for item in result.warnings)


def test_low_score_detections_are_audited_not_regions(tmp_path: Path) -> None:
    page = _page(tmp_path)
    result = _build(
        page,
        [_detection(label="text", score=0.299999, bbox=(1.0, 1.0, 5.0, 5.0))],
        min_score=0.3,
    )

    assert result.regions == []
    assert result.needs_review is True
    assert result.rejected_detections[0]["reason"] == "below_min_score"
    assert result.rejected_detections[0]["detection"]["score"] == 0.299999
    assert any("layout_low_score_detection" in item for item in result.warnings)
    assert any("layout_no_detections" in item for item in result.warnings)

    boundary = _build(
        page,
        [_detection(label="text", score=0.3, bbox=(1.0, 1.0, 5.0, 5.0))],
        min_score=0.3,
    )
    assert len(boundary.regions) == 1
    assert boundary.needs_review is False


def test_no_detections_and_invalid_detections_force_review(tmp_path: Path) -> None:
    page = _page(tmp_path)
    empty = _build(page, [])
    assert empty.regions == []
    assert empty.needs_review is True
    assert any("layout_no_detections" in item for item in empty.warnings)

    malformed = _build(
        page,
        [{"label": "text", "score": 2.0, "bbox_px": [0, 0, 1, 1], "order": 0}],
    )
    assert malformed.regions == []
    assert malformed.needs_review is True
    assert malformed.rejected_detections[0]["reason"] == "invalid_detection"
    assert any("layout_invalid_detection" in item for item in malformed.warnings)

    with pytest.raises(ValidationError, match="needs_review"):
        LayoutPageResult(
            page=page,
            regions=[],
            detector_id=DETECTOR_ID,
            detector_revision=DETECTOR_REVISION,
            needs_review=False,
        )


def test_header_and_footer_detections_are_audited_not_facts(tmp_path: Path) -> None:
    page = _page(tmp_path)
    header = _detection(label="header", bbox=(1.0, 0.0, 9.0, 1.0))
    footer = _detection(label="footer", bbox=(1.0, 9.0, 9.0, 10.0), order=1)

    only_chrome = _build(page, [header, footer])
    assert only_chrome.needs_review is True
    assert [region.kind for region in only_chrome.regions] == ["header", "footer"]
    assert all(
        any("nonfactual_header_footer_detection" in item for item in region.warnings)
        for region in only_chrome.regions
    )
    assert any("layout_only_header_footer_regions" in item for item in only_chrome.warnings)

    with_body = _build(
        page,
        [header, _detection(label="text", bbox=(1.0, 2.0, 9.0, 8.0), order=2)],
    )
    assert with_body.needs_review is False
    header_region = next(region for region in with_body.regions if region.kind == "header")
    assert any(
        "nonfactual_header_footer_detection" in item for item in header_region.warnings
    )


def test_bad_geometry_is_rejected_or_clipped_with_audit(tmp_path: Path) -> None:
    page = _page(tmp_path)
    result = _build(
        page,
        [
            _detection(label="text", bbox=(5.0, 5.0, 5.0, 6.0)),
            _detection(label="text", bbox=(6.0, 1.0, 4.0, 2.0), order=1),
            _detection(label="text", bbox=(20.0, 20.0, 30.0, 30.0), order=2),
            _detection(label="text", bbox=(-5.0, -5.0, -1.0, -1.0), order=3),
            _detection(label="text", bbox=(-2.0, 1.0, 4.0, 5.0), order=4),
            _detection(label="text", bbox=(8.0, 8.0, 15.0, 15.0), order=5),
        ],
    )

    reasons = [item["reason"] for item in result.rejected_detections]
    assert reasons.count("reversed_or_zero_bbox") == 2
    assert reasons.count("outside_page_bbox") == 2
    assert len(result.regions) == 2
    assert result.needs_review is True
    assert any("layout_partial_outside_bbox" in item for item in result.warnings)
    assert all(
        any("bbox_partially_outside_page" in item for item in region.warnings)
        for region in result.regions
    )


def test_duplicate_detections_keep_highest_score_with_audit(tmp_path: Path) -> None:
    page = _page(tmp_path)
    low = _detection(score=0.5, bbox=(1.0, 1.0, 5.0, 5.0), order=0)
    high = _detection(score=0.8, bbox=(1.0, 1.0, 5.0, 5.0), order=1)
    middle = _detection(score=0.6, bbox=(1.0, 1.0, 5.0, 5.0), order=2)

    result = _build(page, [low, high, middle])

    assert len(result.regions) == 1
    assert result.regions[0].score == 0.8
    assert result.regions[0].order == 1
    reasons = [item["reason"] for item in result.rejected_detections]
    assert reasons == ["replaced_lower_score", "duplicate_region_id"]
    assert {item["index"] for item in result.rejected_detections} == {0, 2}
    assert any("layout_duplicate_detection" in item for item in result.warnings)


def test_build_revalidates_constructed_detection_instances(tmp_path: Path) -> None:
    page = _page(tmp_path)
    bypassed = LayoutDetection.model_construct(
        label="text",
        score=0.9,
        bbox_px=[0.0, 0.0, math.nan, 2.0],
        order=0,
        polygon=None,
    )
    result = _build(page, [bypassed])

    assert result.regions == []
    assert result.needs_review is True
    assert result.rejected_detections[0]["reason"] == "invalid_detection"


def test_duplicate_orders_are_surfaced(tmp_path: Path) -> None:
    page = _page(tmp_path)
    result = _build(
        page,
        [
            _detection(bbox=(1.0, 1.0, 3.0, 3.0), order=0),
            _detection(bbox=(4.0, 4.0, 6.0, 6.0), order=0),
        ],
    )
    assert len(result.regions) == 2
    assert any("layout_duplicate_order" in item for item in result.warnings)


def test_crop_uses_actual_pixels_and_audits_clipping(tmp_path: Path) -> None:
    from PIL import Image

    page = _page(tmp_path, width=20, height=15)
    result = _build(
        page,
        [_detection(bbox=(3.2, 2.7, 8.1, 6.4))],
    )
    crop = crop_layout_region(
        page,
        result.regions[0],
        output_dir=tmp_path / "crops",
        padding_px=4,
    )

    assert crop.crop_bbox_px == [0, 0, 13, 11]
    assert crop.width == 13
    assert crop.height == 11
    assert crop.crop_to_page == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    assert any("crop_clipped_to_page_boundary" in item for item in crop.warnings)
    assert crop.image_path.name == f"crop-{crop.image_sha256}.png"
    assert hashlib.sha256(crop.image_path.read_bytes()).hexdigest() == crop.image_sha256

    with Image.open(page.image_path) as parent, Image.open(crop.image_path) as child:
        expected = parent.crop((0, 0, 13, 11))
        assert child.size == (13, 11)
        assert child.convert("RGB").tobytes() == expected.convert("RGB").tobytes()

    page.verify_current()
    page_after = _evidence(page.image_path)
    assert page_after.image_sha256 == page.image_sha256


def test_crop_translation_padding_and_content_addressing(tmp_path: Path) -> None:
    page = _page(tmp_path, width=20, height=15)
    result = _build(page, [_detection(bbox=(5.0, 4.0, 10.0, 9.0))])
    region = result.regions[0]
    output_dir = tmp_path / "crops"

    first = crop_layout_region(page, region, output_dir=output_dir, padding_px=0)
    second = crop_layout_region(page, region, output_dir=output_dir, padding_px=0)

    assert first.crop_bbox_px == [5, 4, 10, 9]
    assert first.crop_to_page == [1.0, 0.0, 5.0, 0.0, 1.0, 4.0]
    assert first.warnings == []
    assert first.image_path == second.image_path
    assert first.image_sha256 == second.image_sha256
    assert first.image_path.read_bytes() == second.image_path.read_bytes()
    assert list(output_dir.glob("*.part")) == []

    first.image_path.write_bytes(b"not the referenced crop")
    with pytest.raises(LayoutValidationError, match="内容不一致"):
        crop_layout_region(page, region, output_dir=output_dir, padding_px=0)


def test_crop_rejects_tampered_or_missing_parent(tmp_path: Path) -> None:
    page = _page(tmp_path, width=12, height=8)
    result = _build(page, [_detection(bbox=(2.0, 2.0, 6.0, 6.0))])
    region = result.regions[0]

    _write_png(page.image_path, width=12, height=8, offset=1)
    with pytest.raises(LayoutValidationError, match="证据"):
        crop_layout_region(page, region, output_dir=tmp_path / "crops")

    page.image_path.unlink()
    with pytest.raises(LayoutValidationError, match="无法读取"):
        crop_layout_region(page, region, output_dir=tmp_path / "crops")


def test_crop_rejects_wrong_page_and_tampered_region(tmp_path: Path) -> None:
    page = _page(tmp_path, width=12, height=8)
    result = _build(page, [_detection(bbox=(2.0, 2.0, 6.0, 6.0))])
    region = result.regions[0]
    other_page = _evidence(page.image_path, page=2)

    with pytest.raises(LayoutValidationError, match="页码"):
        crop_layout_region(other_page, region, output_dir=tmp_path / "crops")

    tampered = region.model_copy(update={"bbox_px": [0.0, 0.0, 2.0, 2.0]})
    with pytest.raises(LayoutValidationError, match="绑定"):
        crop_layout_region(page, tampered, output_dir=tmp_path / "crops")


def test_crop_rejects_wholly_outside_zero_and_bad_padding(tmp_path: Path) -> None:
    page = _page(tmp_path, width=10, height=10)
    outside = _region_for(page, bbox=(10.0, 2.0, 15.0, 8.0))
    zero = _region_for(page, bbox=(5.0, 5.0, 5.0, 6.0))
    reversed_box = _region_for(page, bbox=(5.0, 5.0, 4.0, 6.0))
    valid = _region_for(page, bbox=(2.0, 2.0, 6.0, 6.0))

    with pytest.raises(LayoutValidationError, match="完全位于页面之外"):
        crop_layout_region(page, outside, output_dir=tmp_path / "crops")
    with pytest.raises(LayoutValidationError, match="零面积或方向反转"):
        crop_layout_region(page, zero, output_dir=tmp_path / "crops")
    with pytest.raises(LayoutValidationError, match="零面积或方向反转"):
        crop_layout_region(page, reversed_box, output_dir=tmp_path / "crops")
    with pytest.raises(ValueError, match="padding_px"):
        crop_layout_region(page, valid, output_dir=tmp_path / "crops", padding_px=True)
    with pytest.raises(ValueError, match="padding_px"):
        crop_layout_region(page, valid, output_dir=tmp_path / "crops", padding_px=-1)


def test_region_crop_model_enforces_binding_and_affine(tmp_path: Path) -> None:
    page, crop = _crop_fixture(tmp_path)
    payload = crop.model_dump()

    with pytest.raises(ValidationError, match="crop_bbox_px"):
        RegionCrop.model_validate(payload | {"crop_bbox_px": [True, 4, 10, 9]})
    with pytest.raises(ValidationError, match="宽高"):
        RegionCrop.model_validate(payload | {"crop_bbox_px": [5, 4, 11, 9]})
    with pytest.raises(ValidationError, match="轴对齐平移"):
        RegionCrop.model_validate(payload | {"crop_to_page": [2, 0, 5, 0, 1, 4]})
    with pytest.raises(ValidationError, match="平移"):
        RegionCrop.model_validate(payload | {"crop_to_page": [1, 0, 4, 0, 1, 4]})

    region_payload = dict(payload["region"])
    region_payload["page"] = 2
    with pytest.raises(ValidationError, match="页码"):
        RegionCrop.model_validate(payload | {"region": region_payload})

    region_payload = dict(payload["region"])
    region_payload["label"] = "table"
    with pytest.raises(ValidationError, match="绑定"):
        RegionCrop.model_validate(payload | {"region": region_payload})

    assert page.page == crop.page.page == crop.region.page


def test_map_crop_bbox_to_page_pixels_and_normalized(tmp_path: Path) -> None:
    _, crop = _crop_fixture(tmp_path)

    assert map_crop_bbox_to_page(crop, [0, 0, 5, 5]) == [5.0, 4.0, 10.0, 9.0]
    assert map_crop_bbox_to_page(crop, [0, 0, 1, 1], unit="normalized") == [
        5.0,
        4.0,
        10.0,
        9.0,
    ]
    assert map_crop_bbox_to_page(
        crop, [0.5, 0.5, 1.0, 1.0], unit="normalized"
    ) == [7.5, 6.5, 10.0, 9.0]
    assert map_crop_bbox_to_page(crop, [1, 1, 2, 2]) == [6.0, 5.0, 7.0, 6.0]
    assert map_crop_bbox_to_page(crop, [0, 0, 5, 5], unit="pixels") == [
        5.0,
        4.0,
        10.0,
        9.0,
    ]


def test_map_crop_bbox_rejects_outside_reversed_and_bad_units(tmp_path: Path) -> None:
    _, crop = _crop_fixture(tmp_path)

    with pytest.raises(LayoutValidationError, match="超出裁剪"):
        map_crop_bbox_to_page(crop, [0, 0, 6, 5])
    with pytest.raises(LayoutValidationError, match="方向反转"):
        map_crop_bbox_to_page(crop, [3, 3, 2, 2])
    with pytest.raises(LayoutValidationError, match="方向反转"):
        map_crop_bbox_to_page(crop, [0.5, 0.5, 0.4, 0.9], unit="normalized")
    with pytest.raises(LayoutValidationError, match=r"\[0, 1\]"):
        map_crop_bbox_to_page(crop, [-0.1, 0.0, 1.0, 1.0], unit="normalized")
    with pytest.raises(LayoutValidationError, match=r"\[0, 1\]"):
        map_crop_bbox_to_page(crop, [0.0, 0.0, 1.1, 1.0], unit="normalized")
    with pytest.raises(ValueError, match="unit"):
        map_crop_bbox_to_page(crop, [0, 0, 1, 1], unit="cm")
    with pytest.raises(ValueError, match="bbox"):
        map_crop_bbox_to_page(crop, [0, 0, math.nan, 1])


def test_layout_page_result_rejects_inconsistent_regions(tmp_path: Path) -> None:
    page = _page(tmp_path)
    result = _build(
        page,
        [
            _detection(label="text", bbox=(1.0, 1.0, 4.0, 4.0)),
            _detection(label="table", bbox=(5.0, 5.0, 9.0, 9.0), order=1),
        ],
    )
    payload = result.model_dump()

    duplicate = dict(payload)
    duplicate["regions"] = [payload["regions"][0], payload["regions"][0]]
    with pytest.raises(ValidationError, match="重复的 region_id"):
        LayoutPageResult.model_validate(duplicate)

    wrong_page = dict(payload)
    wrong_page["regions"] = [dict(payload["regions"][0], page=2)]
    with pytest.raises(ValidationError, match="页码"):
        LayoutPageResult.model_validate(wrong_page)

    wrong_detector = dict(payload)
    wrong_detector["regions"] = [
        dict(payload["regions"][0], detector_revision="c" * 40)
    ]
    with pytest.raises(ValidationError, match="检测器身份"):
        LayoutPageResult.model_validate(wrong_detector)

    non_bool = dict(payload)
    non_bool["needs_review"] = 0
    with pytest.raises(ValidationError, match="布尔值"):
        LayoutPageResult.model_validate(non_bool)


def test_page_evidence_json_roundtrip_preserves_resolved_path(tmp_path: Path) -> None:
    page = _page(tmp_path)
    restored = PageImageEvidence.model_validate_json(page.model_dump_json())

    assert restored == page
    assert isinstance(restored.image_path, Path)
    assert restored.image_path == page.image_path
    assert restored.model_dump(mode="json") == page.model_dump(mode="json")


def test_layout_page_result_json_roundtrip_including_audit(tmp_path: Path) -> None:
    page = _page(tmp_path, width=12, height=10)
    result = _build(
        page,
        [
            _detection(label="table", bbox=(1.0, 1.0, 6.0, 5.0)),
            _detection(
                label="text",
                score=0.1,
                bbox=(7.0, 1.0, 11.0, 5.0),
                order=1,
            ),
        ],
    )
    assert result.rejected_detections
    assert result.warnings
    restored = LayoutPageResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert isinstance(restored.page.image_path, Path)
    assert restored.page.image_path == page.image_path
    assert restored.model_dump(mode="json") == result.model_dump(mode="json")


def test_region_crop_json_roundtrip_preserves_parent_and_crop_paths(
    tmp_path: Path,
) -> None:
    page, crop = _crop_fixture(tmp_path)
    restored = RegionCrop.model_validate_json(crop.model_dump_json())

    assert restored == crop
    assert isinstance(restored.image_path, Path)
    assert isinstance(restored.page.image_path, Path)
    assert restored.image_path == crop.image_path
    assert restored.page.image_path == page.image_path
    assert restored.model_dump(mode="json") == crop.model_dump(mode="json")


def test_nested_routed_result_json_roundtrip_keeps_crop_paths(
    tmp_path: Path,
) -> None:
    _, crop = _crop_fixture(tmp_path)
    result = RoutedRegionResult(
        crop=crop,
        local_result=None,
        fallback_result=None,
        markdown=None,
        tables=[],
        reason_codes=["local_failed"],
        errors=["local: offline failure"],
        usage=OcrUsage(),
        historical_usage=None,
    )

    restored = RoutedRegionResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert isinstance(restored.crop.image_path, Path)
    assert isinstance(restored.crop.page.image_path, Path)
    assert restored.crop.image_path == crop.image_path


@pytest.mark.parametrize(
    "bad_path",
    ["", "   ", True, 0, None, ["page.png"]],
    ids=["empty", "blank", "bool", "int", "none", "list"],
)
def test_public_models_reject_invalid_image_paths_in_python_and_json(
    tmp_path: Path, bad_path: object
) -> None:
    page = _page(tmp_path)
    result = _build(page, [_detection()])
    crop = crop_layout_region(
        page, result.regions[0], output_dir=tmp_path / "crops"
    )

    page_payload = page.model_dump(mode="json") | {"image_path": bad_path}
    result_payload = result.model_dump(mode="json")
    result_payload["page"] = result_payload["page"] | {"image_path": bad_path}
    crop_payload = crop.model_dump(mode="json") | {"image_path": bad_path}

    for model_type, payload in (
        (PageImageEvidence, page_payload),
        (LayoutPageResult, result_payload),
        (RegionCrop, crop_payload),
    ):
        with pytest.raises(ValidationError, match="路径"):
            model_type.model_validate(payload)
        with pytest.raises(ValidationError, match="路径"):
            model_type.model_validate_json(json.dumps(payload))


def test_detector_constructor_validation_and_offline_failure(tmp_path: Path) -> None:
    page = _page(tmp_path, width=6, height=4)
    detector = PPDocLayoutV3Detector(
        tmp_path / "missing-model", DETECTOR_REVISION
    )
    assert detector.detector_id == DETECTOR_ID
    assert detector.model_revision == DETECTOR_REVISION
    assert detector.min_score == 0.3

    with pytest.raises(LayoutInferenceError, match="模型目录"):
        detector.detect(page)
    with pytest.raises(LayoutInferenceError, match="模型目录"):
        detector.detect(page)

    for overrides in (
        {"model_path": ""},
        {"model_revision": ""},
        {"device": ""},
        {"min_score": True},
        {"min_score": 1.5},
        {"min_score": math.nan},
    ):
        values = {
            "model_path": tmp_path / "missing-model",
            "model_revision": DETECTOR_REVISION,
        }
        values.update(overrides)
        with pytest.raises(ValueError):
            PPDocLayoutV3Detector(**values)


def test_detector_checks_page_before_loading_model(tmp_path: Path) -> None:
    page = _page(tmp_path, width=6, height=4)
    _write_png(page.image_path, width=6, height=4, offset=1)
    detector = PPDocLayoutV3Detector(
        tmp_path / "missing-model", DETECTOR_REVISION
    )
    with pytest.raises(LayoutValidationError, match="证据"):
        detector.detect(page)


def _conversion_detector(tmp_path: Path) -> PPDocLayoutV3Detector:
    detector = PPDocLayoutV3Detector(
        tmp_path / "missing-model", DETECTOR_REVISION
    )
    detector._model = SimpleNamespace(
        config=SimpleNamespace(id2label={0: "text", 1: "table"})
    )
    return detector


def test_model_output_conversion_preserves_labels_order_and_polygons(
    tmp_path: Path,
) -> None:
    detector = _conversion_detector(tmp_path)
    result = {
        "scores": [0.9, 0.4],
        "labels": [0, 1],
        "boxes": [[1, 2, 5, 6], [2, 3, 7, 8]],
        "order_seq": [1, 0],
        "polygon_points": [
            [[1, 2], [5, 2], [5, 6]],
            [[2, 3], [7, 3], [7, 8]],
        ],
    }

    detections, warnings = detector._detections_from_result(result)

    assert warnings == []
    assert [item.label for item in detections] == ["text", "table"]
    assert [item.order for item in detections] == [1, 0]
    assert detections[0].bbox_px == [1.0, 2.0, 5.0, 6.0]
    assert detections[0].polygon == [[1.0, 2.0], [5.0, 2.0], [5.0, 6.0]]
    assert detections[1].score == 0.4


def test_model_output_conversion_surfaces_missing_metadata(tmp_path: Path) -> None:
    detector = _conversion_detector(tmp_path)
    base = {
        "scores": [0.9],
        "labels": [9],
        "boxes": [[1, 2, 5, 6]],
        "order_seq": [0],
    }

    detections, warnings = detector._detections_from_result(base)
    assert detections[0].label == "label_9"
    assert any("layout_label_metadata_missing" in item for item in warnings)
    assert any("layout_polygon_metadata_missing" in item for item in warnings)
    assert detections[0].polygon is None

    mismatched = dict(base, polygon_points=[[[0, 0], [1, 0], [1, 1]]] * 2)
    detections, warnings = detector._detections_from_result(mismatched)
    assert detections[0].polygon is None
    assert any("layout_polygon_metadata_mismatch" in item for item in warnings)

    bad_polygons = dict(base, polygon_points=[[[0, 0], [1, 0]]])
    detections, warnings = detector._detections_from_result(bad_polygons)
    assert detections[0].polygon is None
    assert any("layout_polygon_metadata_unavailable" in item for item in warnings)


def test_model_output_conversion_rejects_malformed_arrays(tmp_path: Path) -> None:
    detector = _conversion_detector(tmp_path)
    good = {
        "scores": [0.9],
        "labels": [0],
        "boxes": [[1, 2, 5, 6]],
        "order_seq": [0],
    }

    cases = (
        dict(good, scores=[0.9, 0.8]),
        dict(good, labels=[0, 1]),
        dict(good, boxes=[[1, 2, 5]]),
        dict(good, boxes=[[1, 2, math.nan, 6]]),
        dict(good, order_seq=[-1]),
        dict(good, scores=[1.5]),
        dict(good, labels=[1.5]),
    )
    for case in cases:
        with pytest.raises(LayoutInferenceError):
            detector._detections_from_result(case)

    with pytest.raises(LayoutInferenceError, match="不是对象"):
        detector._detections_from_result([])
    for missing in ("scores", "labels", "boxes", "order_seq"):
        incomplete = dict(good)
        del incomplete[missing]
        with pytest.raises(LayoutInferenceError, match=missing):
            detector._detections_from_result(incomplete)
