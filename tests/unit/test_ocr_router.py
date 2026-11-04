from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from campusguide.ingest.ocr_layout import (
    LayoutDetection,
    PageImageEvidence,
    RegionCrop,
    build_layout,
    crop_layout_region,
)
from campusguide.ingest.ocr_router import (
    FallbackResult,
    NumericExpectation,
    RegionExpectation,
    RouterConfig,
    RouterValidationError,
    _canonical_sha256,
    route_region,
)
from campusguide.ingest.visual import VisionRequestBudget
from campusguide.ingest.xiaomi_ocr import OcrBackendResult, OcrUsage

SOURCE_SHA = "a" * 64
DETECTOR_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
DETECTOR_REVISION = "b" * 40
LOCAL_MODEL = "SeerRay-Lab/Xiaomi-OCR-0"
LOCAL_REVISION = "xiaomi-test-revision"
POSTPROCESS_REVISION = "postprocess-test/1"
TABLE_HTML = (
    "<table><tr><td>Key</td><td>Value</td></tr>"
    "<tr><td>A</td><td>1</td></tr></table>"
)
MALFORMED_TABLE_HTML = (
    "<table><tr><td>A</td></tr><tr><td>B</td><td>C</td></tr></table>"
)
RAGGED_OTSL = "<fcel>A<nl><fcel>B<fcel>C<nl>"


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


def _page(
    root: Path,
    *,
    name: str = "page.png",
    width: int = 30,
    height: int = 20,
    page: int = 1,
) -> PageImageEvidence:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png(width, height))
    return PageImageEvidence.from_png(
        doc_id="doc-1",
        version_id="v1",
        source_format="pdf",
        source_sha256=SOURCE_SHA,
        page=page,
        image_path=path,
    )


def _crop_on_page(
    page: PageImageEvidence,
    output_dir: Path,
    *,
    bbox: tuple[float, float, float, float] = (2.0, 2.0, 12.0, 9.0),
    label: str = "text",
    order: int = 0,
) -> RegionCrop:
    layout = build_layout(
        page,
        [
            LayoutDetection(
                label=label,
                score=0.9,
                bbox_px=list(bbox),
                order=order,
            )
        ],
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
    )
    return crop_layout_region(
        page,
        layout.regions[0],
        output_dir=output_dir,
        padding_px=0,
    )


def _crop(
    root: Path,
    *,
    label: str = "text",
    bbox: tuple[float, float, float, float] = (2.0, 2.0, 12.0, 9.0),
) -> RegionCrop:
    page = _page(root)
    return _crop_on_page(
        page,
        root / "crops",
        bbox=bbox,
        label=label,
    )


def _expectation(crop: RegionCrop, **overrides: Any) -> RegionExpectation:
    values: dict[str, Any] = {
        "source_sha256": crop.page.source_sha256,
        "page_image_sha256": crop.page.image_sha256,
        "page": crop.page.page,
        "region_id": crop.region.region_id,
        "crop_sha256": crop.image_sha256,
    }
    values.update(overrides)
    return RegionExpectation(**values)


def _identity_markdown(text: str) -> str:
    return text


class RecordingLocal:
    backend_identity = "test-local/xiaomi-ocr"

    def __init__(
        self,
        text: str,
        *,
        tokens: dict[str, int] | None = None,
        finish_reason: str | None = "stop",
        raise_on_call: bool = False,
    ) -> None:
        self.text = text
        self.tokens = tokens or {}
        self.finish_reason = finish_reason
        self.raise_on_call = raise_on_call
        self.calls = 0
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> OcrBackendResult:
        self.calls += 1
        self.requests.append(request)
        if self.raise_on_call:
            raise AssertionError("local backend must not be invoked")
        return OcrBackendResult(
            text=self.text,
            finish_reason=self.finish_reason,
            **self.tokens,
        )


class RecordingFallback:
    backend_identity = "test-gemini/1"

    def __init__(
        self,
        markdown: str | None,
        *,
        uncertain: bool = False,
        finish_reason: str | None = "stop",
        errors: list[str] | None = None,
        reserves: int = 1,
        tokens: dict[str, int] | None = None,
        raise_on_call: bool = False,
        fail_after_reserve: bool = False,
        model: str = "fake-gemini",
        version: str = "fake-gemini/1",
        prompt_revision: str = "fake-region-prompt/1",
        api_key: str = "sk-test-secret",
        base_url: str = "https://gemini.invalid/v1",
    ) -> None:
        self.markdown = markdown
        self.uncertain = uncertain
        self.finish_reason = finish_reason
        self.errors = list(errors or [])
        self.reserves = reserves
        self.tokens = tokens or {}
        self.raise_on_call = raise_on_call
        self.fail_after_reserve = fail_after_reserve
        self.model = model
        self.version = version
        self.prompt_revision = prompt_revision
        self.api_key = api_key
        self.base_url = base_url
        self.calls = 0

    def __call__(
        self,
        crop: RegionCrop,
        *,
        kind: str,
        budget: VisionRequestBudget,
    ) -> FallbackResult:
        self.calls += 1
        if self.raise_on_call:
            raise AssertionError("fallback backend must not be invoked")
        for attempt in range(1, self.reserves + 1):
            budget.reserve(
                page_no=crop.page.page,
                stage="ocr_region_fallback",
                image_sha256=crop.image_sha256,
                image_path=str(crop.image_path),
                attempt=attempt,
            )
        if self.fail_after_reserve:
            raise RuntimeError("fallback transport failed after a real attempt")
        return FallbackResult(
            markdown=self.markdown,
            raw_response=self.markdown,
            uncertain=self.uncertain,
            finish_reason=self.finish_reason,
            model=self.model,
            model_revision=self.version,
            prompt_revision=self.prompt_revision,
            usage=OcrUsage(cloud_requests=self.reserves, **self.tokens),
            errors=list(self.errors),
        )


def _route(
    crop: RegionCrop,
    local: RecordingLocal,
    *,
    cache_dir: Path | None = None,
    fallback: RecordingFallback | None = None,
    config: RouterConfig | None = None,
    expectation: RegionExpectation | None = None,
    budget: VisionRequestBudget | None = None,
    local_model_revision: str = LOCAL_REVISION,
    postprocess_revision: str = POSTPROCESS_REVISION,
) -> Any:
    return route_region(
        crop,
        local_backend=local,
        postprocess=_identity_markdown,
        local_model=LOCAL_MODEL,
        local_model_revision=local_model_revision,
        postprocess_revision=postprocess_revision,
        config=config,
        gemini_backend=fallback,
        budget=budget,
        cache_dir=cache_dir,
        expectation=expectation,
    )


def _cache_files(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("*.json"))


def test_local_acceptance_skips_cloud_and_keeps_unknown_usage(tmp_path: Path) -> None:
    crop = _crop(tmp_path)
    local = RecordingLocal("hello world")
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)
    budget = VisionRequestBudget(limit=3)

    result = _route(crop, local, fallback=fallback, budget=budget)

    assert result.cache == "disabled"
    assert result.success is True
    assert result.selected_source == "local"
    assert result.reason_codes == ["local_accepted"]
    assert result.usage == OcrUsage(local_requests=1)
    assert result.usage.prompt_tokens is None
    assert result.usage.completion_tokens is None
    assert result.usage.total_tokens is None
    assert result.needs_review is False
    assert local.calls == 1
    assert fallback.calls == 0
    assert budget.images_sent == 0

    second = _route(crop, local, fallback=fallback, budget=budget)
    assert second.cache == "disabled"
    assert local.calls == 2
    assert budget.images_sent == 0


def test_local_acceptance_preserves_reported_tokens(tmp_path: Path) -> None:
    crop = _crop(tmp_path)
    local = RecordingLocal(
        "hello",
        tokens={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    )

    result = _route(crop, local)

    assert result.success is True
    assert result.usage == OcrUsage(
        local_requests=1,
        prompt_tokens=3,
        completion_tokens=4,
        total_tokens=7,
    )


def test_local_ragged_table_triggers_one_metered_fallback(tmp_path: Path) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(
        TABLE_HTML,
        tokens={"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
    )
    budget = VisionRequestBudget(limit=5)

    result = _route(crop, local, fallback=fallback, budget=budget)

    assert result.success is True
    assert result.selected_source == "gemini"
    assert "local_failed" in result.reason_codes
    assert "local_table_missing" in result.reason_codes
    assert "fallback_accepted" in result.reason_codes
    assert result.cache == "disabled"
    assert result.needs_review is True
    assert result.local_result is not None
    assert result.local_result.success is False
    assert result.fallback_result is not None
    assert result.fallback_result.usage.total_tokens == 11
    assert result.usage.local_requests == 1
    assert result.usage.cloud_requests == 1
    assert result.usage.prompt_tokens is None
    assert result.usage.completion_tokens is None
    assert result.usage.total_tokens is None
    assert local.calls == 1
    assert fallback.calls == 1
    assert budget.images_sent == 1

    table = result.tables[0]
    assert table.table_id.startswith(crop.region.region_id)
    assert table.bbox is None
    assert table.locator.bbox is None
    assert table.vision is not None
    assert table.vision["bbox_unit"] == "page_pixels"
    assert table.vision["region_id"] == crop.region.region_id
    assert table.vision["region_bbox_px"] == crop.region.bbox_px
    assert table.vision["crop_sha256"] == crop.image_sha256
    for row in table.rows:
        assert row.locator.bbox is None
        for cell in row.cells:
            assert cell.bbox is None


@pytest.mark.parametrize(
    ("case", "kwargs", "expected_reason"),
    [
        ("uncertain", {"uncertain": True}, "fallback_uncertain"),
        ("length", {"finish_reason": "length"}, "fallback_finish_reason"),
        ("malformed", {"markdown": MALFORMED_TABLE_HTML}, "fallback_failed"),
        ("blank", {"markdown": "   "}, "fallback_blank_output"),
        ("unmetered", {"reserves": 0}, "unmetered_fallback"),
    ],
)
def test_rejected_fallback_candidates_stay_pending(
    tmp_path: Path,
    case: str,
    kwargs: dict[str, Any],
    expected_reason: str,
) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(**{"markdown": TABLE_HTML, **kwargs})
    cache_dir = tmp_path / "cache"
    budget = VisionRequestBudget(limit=5)

    result = _route(
        crop,
        local,
        cache_dir=cache_dir,
        fallback=fallback,
        budget=budget,
    )

    assert result.success is False
    assert result.selected_source == "none"
    assert result.needs_review is True
    assert expected_reason in result.reason_codes
    assert result.fallback_result is not None
    assert local.calls == 1
    assert fallback.calls == 1
    assert budget.images_sent == fallback.reserves
    assert _cache_files(cache_dir) == []
    if case == "malformed":
        assert any(error.startswith("ocr_table_malformed") for error in result.errors)


@pytest.mark.parametrize(
    ("label", "expected_kind"),
    [("figure", "figure"), ("mystery", "unknown"), ("header", "header")],
)
def test_non_content_regions_stay_reviewable_without_calls(
    tmp_path: Path,
    label: str,
    expected_kind: str,
) -> None:
    crop = _crop(tmp_path, label=label)
    assert crop.region.kind == expected_kind
    local = RecordingLocal("unused", raise_on_call=True)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)
    cache_dir = tmp_path / "cache"

    result = _route(
        crop,
        local,
        cache_dir=cache_dir,
        fallback=fallback,
    )

    assert result.success is False
    assert result.selected_source == "none"
    assert result.needs_review is True
    assert result.reason_codes == ["kind_requires_review"]
    assert result.cache == "miss"
    assert local.calls == 0
    assert fallback.calls == 0
    assert _cache_files(cache_dir) == []


def test_blank_local_output_is_not_accepted(tmp_path: Path) -> None:
    crop = _crop(tmp_path)
    local = RecordingLocal("   ")
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)
    config = RouterConfig(fallback_enabled=False)
    cache_dir = tmp_path / "cache"

    result = _route(
        crop,
        local,
        cache_dir=cache_dir,
        fallback=fallback,
        config=config,
    )

    assert result.success is False
    assert result.selected_source == "none"
    assert "local_blank" in result.reason_codes
    assert "fallback_disabled" in result.reason_codes
    assert result.usage == OcrUsage(local_requests=1)
    assert any(error.startswith("ocr_empty_response") for error in result.errors)
    assert local.calls == 1
    assert fallback.calls == 0
    assert _cache_files(cache_dir) == []


@pytest.mark.parametrize(
    "cache_enabled", [False, True], ids=["uncached", "enabled"]
)
def test_parent_crop_and_expectation_tampering_fail_before_requests(
    tmp_path: Path,
    cache_enabled: bool,
) -> None:
    def cache(name: str) -> Path | None:
        return tmp_path / name if cache_enabled else None

    parent_crop = _crop(tmp_path / "parent")
    parent_crop.page.image_path.write_bytes(
        parent_crop.page.image_path.read_bytes() + b"tampered"
    )
    local = RecordingLocal("unused", raise_on_call=True)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)

    with pytest.raises(RouterValidationError):
        _route(
            parent_crop,
            local,
            cache_dir=cache("parent-cache"),
            fallback=fallback,
        )

    crop = _crop(tmp_path / "crop")
    crop.image_path.write_bytes(crop.image_path.read_bytes() + b"tampered")
    with pytest.raises(RouterValidationError):
        _route(
            crop,
            local,
            cache_dir=cache("crop-cache"),
            fallback=fallback,
        )

    valid_crop = _crop(tmp_path / "expectation")
    bad_expectation = _expectation(valid_crop, region_id="another-region")
    with pytest.raises(RouterValidationError):
        _route(
            valid_crop,
            local,
            cache_dir=cache("expectation-cache"),
            fallback=fallback,
            expectation=bad_expectation,
        )

    assert local.calls == 0
    assert fallback.calls == 0
    if cache_enabled:
        for name in ("parent-cache", "crop-cache", "expectation-cache"):
            assert _cache_files(tmp_path / name) == []


def test_mutated_crop_and_config_identities_are_rejected_before_requests(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path)
    bad_region = crop.region.model_copy(
        update={"region_id": crop.region.region_id + "-mutated"}
    )
    mutated_crop = crop.model_copy(update={"region": bad_region})
    local = RecordingLocal("unused", raise_on_call=True)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)

    with pytest.raises(RouterValidationError):
        _route(
            mutated_crop,
            local,
            cache_dir=tmp_path / "crop-cache",
            fallback=fallback,
        )

    bad_config = RouterConfig().model_copy(update={"max_text_tokens": 0})
    with pytest.raises(RouterValidationError):
        _route(
            crop,
            local,
            cache_dir=tmp_path / "config-cache",
            fallback=fallback,
            config=bad_config,
        )

    assert local.calls == 0
    assert fallback.calls == 0
    assert _cache_files(tmp_path / "crop-cache") == []
    assert _cache_files(tmp_path / "config-cache") == []


def test_forged_region_id_is_rejected_before_uncached_local_call(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path)
    forged = crop.model_copy(
        update={
            "region": crop.region.model_copy(
                update={"region_id": crop.region.region_id + "-forged"}
            )
        }
    )
    local = RecordingLocal("unused", raise_on_call=True)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)

    with pytest.raises(RouterValidationError):
        _route(forged, local, cache_dir=None, fallback=fallback)

    assert local.calls == 0
    assert fallback.calls == 0


@pytest.mark.parametrize(
    "cache_enabled", [False, True], ids=["uncached", "enabled"]
)
@pytest.mark.parametrize(
    "case",
    [
        "bool-as-int",
        "string-as-bool",
        "bool-as-token-limit",
        "wrong-config-type",
        "wrong-expectation-type",
        "wrong-nested-crop-type",
        "zero-expectation-page",
    ],
)
def test_mutated_types_and_bools_are_rejected_before_calls(
    tmp_path: Path,
    cache_enabled: bool,
    case: str,
) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(TABLE_HTML, raise_on_call=True)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)
    cache_dir = tmp_path / "cache" if cache_enabled else None
    config: Any = None
    expectation: Any = None

    if case == "bool-as-int":
        config = RouterConfig().model_copy(update={"enabled": 1})
    elif case == "string-as-bool":
        config = RouterConfig().model_copy(
            update={"fallback_enabled": "true"}
        )
    elif case == "bool-as-token-limit":
        config = RouterConfig().model_copy(update={"max_table_tokens": True})
    elif case == "wrong-config-type":
        config = {"enabled": True}
    elif case == "wrong-expectation-type":
        expectation = {"source_sha256": crop.page.source_sha256}
    elif case == "wrong-nested-crop-type":
        crop = crop.model_copy(update={"region": "not-a-region"})
    elif case == "zero-expectation-page":
        expectation = _expectation(crop).model_copy(update={"page": 0})

    with pytest.raises(RouterValidationError):
        _route(
            crop,
            local,
            cache_dir=cache_dir,
            fallback=fallback,
            config=config,
            expectation=expectation,
        )

    assert local.calls == 0
    assert fallback.calls == 0
    if cache_enabled:
        assert _cache_files(tmp_path / "cache") == []


def test_two_regions_on_same_page_have_disjoint_table_ids(tmp_path: Path) -> None:
    page = _page(tmp_path, width=40, height=24)
    layout = build_layout(
        page,
        [
            LayoutDetection(
                label="table", score=0.9, bbox_px=[2.0, 2.0, 18.0, 12.0], order=0
            ),
            LayoutDetection(
                label="table", score=0.9, bbox_px=[22.0, 2.0, 38.0, 12.0], order=1
            ),
        ],
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
    )
    crops = [
        crop_layout_region(
            page,
            region,
            output_dir=tmp_path / "crops",
            padding_px=0,
        )
        for region in layout.regions
    ]
    local = RecordingLocal(TABLE_HTML)

    results = [_route(crop, local) for crop in crops]

    assert all(result.success for result in results)
    assert local.calls == 2
    first_table = results[0].tables[0]
    second_table = results[1].tables[0]
    assert first_table.table_id != second_table.table_id
    assert first_table.table_id.startswith(crops[0].region.region_id)
    assert second_table.table_id.startswith(crops[1].region.region_id)
    assert first_table.rows[0].row_id != second_table.rows[0].row_id
    assert (
        first_table.rows[0].cells[0].cell_id
        != second_table.rows[0].cells[0].cell_id
    )
    assert first_table.vision is not None
    assert second_table.vision is not None
    assert first_table.vision["region_id"] == crops[0].region.region_id
    assert second_table.vision["region_id"] == crops[1].region.region_id


def test_fallback_retries_are_counted_as_real_cloud_requests(tmp_path: Path) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(TABLE_HTML, reserves=2)
    budget = VisionRequestBudget(limit=5)

    result = _route(crop, local, fallback=fallback, budget=budget)

    assert result.success is True
    assert result.selected_source == "gemini"
    assert result.usage.local_requests == 1
    assert result.usage.cloud_requests == 2
    assert result.usage.total_tokens is None
    assert budget.images_sent == 2
    assert [request["attempt"] for request in budget.requests] == [1, 2]


@pytest.mark.parametrize(
    ("parent_limit", "expected_count"),
    [(10, 2), (1, 1)],
    ids=["per-region-cap", "shared-cap"],
)
def test_per_region_and_shared_cloud_caps_stop_retries(
    tmp_path: Path,
    parent_limit: int,
    expected_count: int,
) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(TABLE_HTML, reserves=3)
    budget = VisionRequestBudget(limit=parent_limit)
    config = RouterConfig(max_cloud_requests_per_region=2)

    result = _route(
        crop,
        local,
        fallback=fallback,
        config=config,
        budget=budget,
    )

    assert result.success is False
    assert result.selected_source == "none"
    assert "fallback_budget_exhausted" in result.reason_codes
    assert result.usage.cloud_requests == expected_count
    assert budget.images_sent == expected_count
    assert fallback.calls == 1


def test_failed_fallback_attempt_is_still_counted(tmp_path: Path) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(
        TABLE_HTML,
        reserves=1,
        fail_after_reserve=True,
    )
    budget = VisionRequestBudget(limit=5)

    result = _route(crop, local, fallback=fallback, budget=budget)

    assert result.success is False
    assert result.selected_source == "none"
    assert "fallback_exception" in result.reason_codes
    assert result.fallback_result is None
    assert result.usage.cloud_requests == 1
    assert result.usage.total_tokens is None
    assert budget.images_sent == 1
    assert any(
        error.startswith("fallback_backend_error") for error in result.errors
    )


def test_valid_local_cache_hit_skips_backends_and_keeps_history(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path)
    cache_dir = tmp_path / "cache"
    local = RecordingLocal(
        "hello",
        tokens={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    )
    fallback = RecordingFallback(TABLE_HTML)

    first = _route(crop, local, cache_dir=cache_dir, fallback=fallback)

    assert first.cache == "miss"
    assert first.success is True
    assert local.calls == 1
    assert fallback.calls == 0
    assert len(_cache_files(cache_dir)) == 1
    payload = json.loads(
        _cache_files(cache_dir)[0].read_text(encoding="utf-8")
    )
    assert payload["record_version"] == "ocr-router-cache/3"
    assert payload["binding"]["record_version"] == "ocr-router-cache/3"

    local_again = RecordingLocal("unused", raise_on_call=True)
    fallback_again = RecordingFallback(TABLE_HTML, raise_on_call=True)
    second = _route(
        crop,
        local_again,
        cache_dir=cache_dir,
        fallback=fallback_again,
    )

    assert second.cache == "hit"
    assert second.success is True
    assert second.selected_source == "local"
    assert second.usage == OcrUsage()
    assert second.usage.prompt_tokens is None
    assert second.usage.completion_tokens is None
    assert second.usage.total_tokens is None
    assert second.historical_usage == first.usage
    assert second.historical_usage is not None
    assert second.historical_usage.total_tokens == 7
    assert second.historical_elapsed_seconds == first.elapsed_seconds
    assert second.local_result is not None
    assert second.local_result == first.local_result
    assert second.local_result.usage.local_requests == 1
    assert second.local_result.cache == "disabled"
    assert second.markdown == first.markdown
    assert second.tables == first.tables
    assert second.reason_codes == first.reason_codes
    assert local_again.calls == 0
    assert fallback_again.calls == 0


def test_valid_gemini_cache_hit_revalidates_offline_without_cloud_calls(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path, label="table")
    cache_dir = tmp_path / "cache"
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(
        TABLE_HTML,
        tokens={"total_tokens": 9},
    )
    budget = VisionRequestBudget(limit=4)

    first = _route(
        crop,
        local,
        cache_dir=cache_dir,
        fallback=fallback,
        budget=budget,
    )

    assert first.cache == "miss"
    assert first.success is True
    assert first.selected_source == "gemini"
    assert local.calls == 1
    assert fallback.calls == 1
    assert budget.images_sent == 1

    local_again = RecordingLocal(RAGGED_OTSL, raise_on_call=True)
    fallback_again = RecordingFallback(
        TABLE_HTML,
        tokens={"total_tokens": 9},
        raise_on_call=True,
    )
    second = _route(
        crop,
        local_again,
        cache_dir=cache_dir,
        fallback=fallback_again,
        budget=budget,
    )

    assert second.cache == "hit"
    assert second.success is True
    assert second.selected_source == "gemini"
    assert second.usage == OcrUsage()
    assert second.historical_usage == first.usage
    assert second.historical_usage is not None
    assert second.historical_usage.local_requests == 1
    assert second.historical_usage.cloud_requests == 1
    assert second.local_result == first.local_result
    assert second.fallback_result == first.fallback_result
    assert second.markdown == first.markdown
    assert second.tables == first.tables
    assert local_again.calls == 0
    assert fallback_again.calls == 0
    assert budget.images_sent == 1


def test_gemini_cache_hit_after_local_failure_without_raw_response(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path, label="table")
    cache_dir = tmp_path / "cache"
    local = RecordingLocal("unused", raise_on_call=True)
    fallback = RecordingFallback(TABLE_HTML)
    budget = VisionRequestBudget(limit=3)

    first = _route(
        crop,
        local,
        cache_dir=cache_dir,
        fallback=fallback,
        budget=budget,
    )

    assert first.cache == "miss"
    assert first.success is True
    assert first.selected_source == "gemini"
    assert first.local_result is not None
    assert first.local_result.raw_response is None
    assert first.local_result.success is False
    assert local.calls == 1
    assert fallback.calls == 1
    assert budget.images_sent == 1

    local_again = RecordingLocal("unused", raise_on_call=True)
    fallback_again = RecordingFallback(TABLE_HTML, raise_on_call=True)
    second = _route(
        crop,
        local_again,
        cache_dir=cache_dir,
        fallback=fallback_again,
        budget=budget,
    )

    assert second.cache == "hit"
    assert second.success is True
    assert second.selected_source == "gemini"
    assert second.usage == OcrUsage()
    assert second.historical_usage == first.usage
    assert second.local_result == first.local_result
    assert local_again.calls == 0
    assert fallback_again.calls == 0
    assert budget.images_sent == 1


def test_cache_invalidated_by_local_model_and_postprocess_revision(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path)
    cache_dir = tmp_path / "cache"
    local = RecordingLocal("hello")

    first = _route(crop, local, cache_dir=cache_dir)
    assert first.cache == "miss"
    assert local.calls == 1

    changed_model = _route(
        crop,
        local,
        cache_dir=cache_dir,
        local_model_revision="xiaomi-other-revision",
    )
    assert changed_model.cache == "miss"
    assert local.calls == 2

    changed_postprocess = _route(
        crop,
        local,
        cache_dir=cache_dir,
        postprocess_revision="postprocess-test/2",
    )
    assert changed_postprocess.cache == "miss"
    assert local.calls == 3
    assert len(_cache_files(cache_dir)) == 3

    original_again = _route(crop, local, cache_dir=cache_dir)
    assert original_again.cache == "hit"
    assert local.calls == 3


def test_cache_invalidated_by_config_token_limit(tmp_path: Path) -> None:
    crop = _crop(tmp_path)
    cache_dir = tmp_path / "cache"
    local = RecordingLocal("hello")

    first = _route(crop, local, cache_dir=cache_dir)
    assert first.cache == "miss"

    changed = _route(
        crop,
        local,
        cache_dir=cache_dir,
        config=RouterConfig(max_text_tokens=512),
    )
    assert changed.cache == "miss"
    assert local.calls == 2
    assert len(_cache_files(cache_dir)) == 2

    original_again = _route(crop, local, cache_dir=cache_dir)
    assert original_again.cache == "hit"
    assert local.calls == 2


@pytest.mark.parametrize("field", ["model", "version", "prompt_revision"])
def test_cache_invalidated_by_fallback_identity(
    tmp_path: Path,
    field: str,
) -> None:
    crop = _crop(tmp_path)
    cache_dir = tmp_path / "cache"
    local = RecordingLocal("hello")
    original = RecordingFallback(TABLE_HTML)
    changed_kwargs: dict[str, Any] = {field: "changed-fallback-identity"}
    changed = RecordingFallback(TABLE_HTML, **changed_kwargs)

    first = _route(crop, local, cache_dir=cache_dir, fallback=original)
    assert first.cache == "miss"
    assert local.calls == 1

    second = _route(crop, local, cache_dir=cache_dir, fallback=changed)
    assert second.cache == "miss"
    assert local.calls == 2
    assert len(_cache_files(cache_dir)) == 2

    third = _route(crop, local, cache_dir=cache_dir, fallback=original)
    assert third.cache == "hit"
    assert local.calls == 2
    assert original.calls == 0
    assert changed.calls == 0


def test_cache_invalidated_by_expectation_and_crop(tmp_path: Path) -> None:
    page = _page(tmp_path)
    crop_a = _crop_on_page(page, tmp_path / "crops-a", bbox=(2.0, 2.0, 12.0, 9.0))
    crop_b = _crop_on_page(
        page,
        tmp_path / "crops-b",
        bbox=(16.0, 2.0, 26.0, 9.0),
        order=1,
    )
    cache_dir = tmp_path / "cache"
    local = RecordingLocal("hello")
    expectation = _expectation(crop_a, expected_text_anchors=["hello"])

    first = _route(crop_a, local, cache_dir=cache_dir)
    assert first.cache == "miss"
    assert local.calls == 1

    with_expectation = _route(
        crop_a,
        local,
        cache_dir=cache_dir,
        expectation=expectation,
    )
    assert with_expectation.cache == "miss"
    assert with_expectation.success is True
    assert local.calls == 2

    expectation_again = _route(
        crop_a,
        local,
        cache_dir=cache_dir,
        expectation=expectation,
    )
    assert expectation_again.cache == "hit"
    assert local.calls == 2

    other_crop = _route(crop_b, local, cache_dir=cache_dir)
    assert other_crop.cache == "miss"
    assert local.calls == 3
    assert len(_cache_files(cache_dir)) == 3


def _tamper_row_text(payload: dict[str, Any]) -> None:
    payload["result"]["tables"][0]["rows"][0]["cells"][0]["text"] = "tampered"


def _tamper_table_id(payload: dict[str, Any]) -> None:
    payload["result"]["tables"][0]["table_id"] = "tampered-table"


def _tamper_local_model(payload: dict[str, Any]) -> None:
    payload["result"]["local_result"]["model_revision"] = "tampered-revision"


def _tamper_local_raw(payload: dict[str, Any]) -> None:
    payload["result"]["local_result"]["raw_response"] = "different raw response"


def _tamper_local_and_final_tables(payload: dict[str, Any]) -> None:
    for tables in (
        payload["result"]["local_result"]["tables"],
        payload["result"]["tables"],
    ):
        tables[0]["rows"][0]["cells"][0]["text"] = "tampered-together"


def _tamper_crop_source(payload: dict[str, Any]) -> None:
    payload["result"]["crop"]["page"]["source_sha256"] = "c" * 64


def _tamper_seal(payload: dict[str, Any]) -> None:
    payload["seal"] = "0" * 64


def _reseal(payload: dict[str, Any]) -> None:
    result_sha256 = _canonical_sha256(payload["result"])
    payload["result_sha256"] = result_sha256
    payload["seal"] = _canonical_sha256(
        {"key": payload["key"], "result_sha256": result_sha256}
    )


def _write_cache_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("mutate", "reseal"),
    [
        (_tamper_row_text, True),
        (_tamper_table_id, True),
        (_tamper_local_model, True),
        (_tamper_local_raw, True),
        (_tamper_crop_source, True),
        (_tamper_seal, False),
    ],
    ids=[
        "rows",
        "table-id",
        "model-binding",
        "local-raw",
        "crop-binding",
        "seal",
    ],
)
def test_tampered_cache_is_rejected_audited_and_rerun(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    reseal: bool,
) -> None:
    crop = _crop(tmp_path, label="table")
    cache_dir = tmp_path / "cache"
    local = RecordingLocal(TABLE_HTML)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)

    first = _route(crop, local, cache_dir=cache_dir, fallback=fallback)
    assert first.cache == "miss"
    assert first.success is True
    assert local.calls == 1

    files = _cache_files(cache_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    mutate(payload)
    if reseal:
        _reseal(payload)
    _write_cache_payload(files[0], payload)

    second = _route(crop, local, cache_dir=cache_dir, fallback=fallback)
    assert second.cache == "miss"
    assert second.success is True
    assert "cache_corrupt" in second.reason_codes
    assert local.calls == 2
    assert fallback.calls == 0

    third = _route(crop, local, cache_dir=cache_dir, fallback=fallback)
    assert third.cache == "hit"
    assert local.calls == 2


def test_resealed_consistent_local_and_final_table_corruption_reruns(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path, label="table")
    cache_dir = tmp_path / "cache"
    local = RecordingLocal(TABLE_HTML)
    fallback = RecordingFallback(TABLE_HTML, raise_on_call=True)

    first = _route(crop, local, cache_dir=cache_dir, fallback=fallback)
    assert first.cache == "miss"
    assert first.success is True
    assert first.selected_source == "local"
    assert local.calls == 1

    files = _cache_files(cache_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    _tamper_local_and_final_tables(payload)
    _reseal(payload)
    _write_cache_payload(files[0], payload)

    second = _route(crop, local, cache_dir=cache_dir, fallback=fallback)
    assert second.cache == "miss"
    assert second.success is True
    assert "cache_corrupt" in second.reason_codes
    assert local.calls == 2
    assert fallback.calls == 0

    third = _route(crop, local, cache_dir=cache_dir, fallback=fallback)
    assert third.cache == "hit"
    assert local.calls == 2
    assert third.tables[0].rows[0].cells[0].text == "Key"


def test_unreadable_cache_json_is_rejected_and_rebuilt(tmp_path: Path) -> None:
    crop = _crop(tmp_path)
    cache_dir = tmp_path / "cache"
    local = RecordingLocal("hello")

    first = _route(crop, local, cache_dir=cache_dir)
    assert first.cache == "miss"
    files = _cache_files(cache_dir)
    assert len(files) == 1
    files[0].write_text("{interrupted", encoding="utf-8")

    second = _route(crop, local, cache_dir=cache_dir)
    assert second.cache == "miss"
    assert second.success is True
    assert "cache_corrupt" in second.reason_codes
    assert local.calls == 2

    third = _route(crop, local, cache_dir=cache_dir)
    assert third.cache == "hit"
    assert local.calls == 2


def test_cache_does_not_serialize_endpoint_or_credentials(tmp_path: Path) -> None:
    crop = _crop(tmp_path, label="table")
    cache_dir = tmp_path / "cache"
    local = RecordingLocal(RAGGED_OTSL)
    fallback = RecordingFallback(
        TABLE_HTML,
        api_key="sk-test-secret-not-for-cache",
        base_url="https://gemini.invalid/v1",
    )

    result = _route(
        crop,
        local,
        cache_dir=cache_dir,
        fallback=fallback,
    )

    assert result.success is True
    assert result.selected_source == "gemini"
    files = _cache_files(cache_dir)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "sk-test-secret-not-for-cache" not in text
    assert "gemini.invalid" not in text
    assert "api_key" not in text


def test_router_shadow_config_is_opt_in_and_loadable() -> None:
    path = Path(__file__).resolve().parents[2] / "configs" / "ocr.router.yaml"

    config = RouterConfig.from_yaml(path)

    assert config.enabled is True
    assert config.mode == "shadow"
    assert config.primary == "xiaomi-local"
    assert config.secondary == "gemini"
    assert config.difficult_only is True
    assert config.fallback_enabled is True
    assert config.max_cloud_requests_per_region == 2
    assert config.max_text_tokens == 1024
    assert config.max_table_tokens == 4096
    assert config.max_formula_tokens == 1024


def test_png_fixture_crop_hash_matches_parent_pixels(tmp_path: Path) -> None:
    crop = _crop(tmp_path)

    assert hashlib.sha256(crop.image_path.read_bytes()).hexdigest() == (
        crop.image_sha256
    )
    assert crop.width == crop.crop_bbox_px[2] - crop.crop_bbox_px[0]
    assert crop.height == crop.crop_bbox_px[3] - crop.crop_bbox_px[1]


def test_expectation_row_key_matches_linebreak_and_fullwidth_variant(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(
        "<table><tr><td>专业</td><td>计划数</td></tr>"
        "<tr><td>计算机科学与技术(本科)</td><td>42</td></tr></table>"
    )
    expectation = _expectation(
        crop,
        numeric_checks=[
            NumericExpectation(
                row_key="计算机科学与技术\n（本科）",
                key_col=0,
                col=1,
                value="42",
            )
        ],
    )

    result = _route(crop, local, expectation=expectation)

    assert result.success is True
    assert result.selected_source == "local"
    assert result.reason_codes == ["local_accepted"]


def test_expectation_row_key_matches_roman_numeral_variant(
    tmp_path: Path,
) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(
        "<table><tr><td>课程</td><td>学分</td></tr>"
        "<tr><td>高等数学AI</td><td>5</td></tr>"
        "<tr><td>高等数学AII</td><td>4</td></tr></table>"
    )
    expectation = _expectation(
        crop,
        numeric_checks=[
            NumericExpectation(
                row_key="高等数学AⅠ", key_col=0, col=1, value="5"
            ),
            NumericExpectation(
                row_key="高等数学AⅡ", key_col=0, col=1, value="4"
            ),
        ],
    )

    result = _route(crop, local, expectation=expectation)

    assert result.success is True
    assert result.selected_source == "local"
    assert result.reason_codes == ["local_accepted"]


def test_colliding_normalized_row_keys_require_review(tmp_path: Path) -> None:
    crop = _crop(tmp_path, label="table")
    local = RecordingLocal(
        "<table><tr><td>专业</td><td>名额</td></tr>"
        "<tr><td>软件工程(本科)</td><td>12</td></tr>"
        "<tr><td>软件工程（本科）</td><td>13</td></tr></table>"
    )
    expectation = _expectation(
        crop,
        numeric_checks=[
            NumericExpectation(
                row_key="软件工程（本科）", key_col=0, col=1, value="13"
            ),
            NumericExpectation(
                row_key="软件工程(本科)", key_col=0, col=1, value="12"
            ),
        ],
    )

    result = _route(crop, local, expectation=expectation)

    assert result.success is False
    assert result.selected_source == "none"
    assert result.needs_review is True
    assert "local_expectation_row_key" in result.reason_codes
    assert "local_expectation_value" not in result.reason_codes
