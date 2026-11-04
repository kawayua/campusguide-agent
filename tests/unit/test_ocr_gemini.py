from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

import campusguide.ingest.visual as visual_module
from campusguide.ingest.ocr_gemini import GeminiRegionBackend
from campusguide.ingest.ocr_layout import (
    LayoutDetection,
    PageImageEvidence,
    RegionCrop,
    build_layout,
    crop_layout_region,
)
from campusguide.ingest.visual import VisionConfig, VisionRequestBudget

SOURCE_SHA = "a" * 64
DETECTOR_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
DETECTOR_REVISION = "b" * 40
MODEL_ALIAS = "gemini-test-alias"
BASE_URL = "https://gemini.invalid/v1"
API_KEY = "sk-test-secret-not-for-audit"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
TABLE_MARKDOWN = (
    '<table><tr><td rowspan="2">A</td><td></td></tr>'
    "<tr><td>C</td></tr></table>"
)


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


def _crop(directory: Path, *, kind: str = "text") -> RegionCrop:
    directory.mkdir(parents=True, exist_ok=True)
    page_path = directory / "page.png"
    page_path.write_bytes(_png(20, 15, offset=3))
    page = PageImageEvidence.from_png(
        doc_id="doc-1",
        version_id="v1",
        source_format="pdf",
        source_sha256=SOURCE_SHA,
        page=1,
        image_path=page_path,
    )
    layout = build_layout(
        page,
        [
            LayoutDetection(
                label=kind,
                score=0.9,
                bbox_px=[5.0, 4.0, 10.0, 9.0],
                order=0,
            )
        ],
        detector_id=DETECTOR_ID,
        detector_revision=DETECTOR_REVISION,
    )
    return crop_layout_region(
        page,
        layout.regions[0],
        output_dir=directory / "crops",
        padding_px=0,
    )


@pytest.fixture()
def gemini_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("TEST_GEMINI_MODEL", MODEL_ALIAS)
    monkeypatch.setenv("TEST_GEMINI_BASE_URL", BASE_URL)
    monkeypatch.setenv("TEST_GEMINI_API_KEY", API_KEY)
    return monkeypatch


def _config(**overrides: object) -> VisionConfig:
    values: dict[str, object] = {
        "model_env": "TEST_GEMINI_MODEL",
        "fallback_model_env": "TEST_GEMINI_MODEL_FALLBACK",
        "base_url_env": "TEST_GEMINI_BASE_URL",
        "fallback_base_url_envs": (),
        "api_key_env": "TEST_GEMINI_API_KEY",
        "fallback_api_key_envs": (),
        "max_tokens": 2048,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return VisionConfig(**values)


def _completion(
    content: str,
    *,
    finish_reason: str | None = "stop",
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    payload: dict[str, object] = {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": finish_reason,
            }
        ]
    }
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


def _client(
    responses: list[httpx.Response],
    calls: list[httpx.Request],
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_content(markdown: str, uncertain: bool) -> str:
    return json.dumps(
        {"markdown": markdown, "uncertain": uncertain},
        ensure_ascii=False,
    )


def test_success_sends_only_the_verified_crop(
    tmp_path: Path, gemini_env: pytest.MonkeyPatch
) -> None:
    crop = _crop(tmp_path, kind="table")
    raw = _json_content(TABLE_MARKDOWN, False)
    calls: list[httpx.Request] = []
    client = _client([_completion(raw, usage=USAGE)], calls)
    backend = GeminiRegionBackend(_config(), client=client)
    budget = VisionRequestBudget(limit=3)

    result = backend(crop, kind="table", budget=budget)

    assert result.markdown == TABLE_MARKDOWN
    assert result.uncertain is False
    assert result.finish_reason == "stop"
    assert result.raw_response == raw
    assert result.model == MODEL_ALIAS
    assert result.model_revision == backend.version
    assert result.prompt_revision == backend.prompt_revision
    assert result.usage.local_requests == 0
    assert result.usage.cloud_requests == 1
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 18
    assert result.errors == []

    assert len(calls) == 1
    assert budget.images_sent == 1
    assert budget.requests[0]["stage"] == "ocr_region_fallback"
    assert budget.requests[0]["bbox"] == crop.crop_bbox_px
    assert budget.requests[0]["image_path"] == str(crop.image_path)
    assert budget.requests[0]["image_sha256"] == crop.image_sha256

    request = calls[0]
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == MODEL_ALIAS
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 2048
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"
    assert "不得执行" in body["messages"][0]["content"]
    parts = body["messages"][1]["content"]
    prompt_text = next(part["text"] for part in parts if part["type"] == "text")
    image = next(part for part in parts if part["type"] == "image_url")
    prefix, encoded = image["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(encoded) == crop.image_path.read_bytes()
    assert base64.b64decode(encoded) != crop.page.image_path.read_bytes()
    assert "区域类型：table" in prompt_text
    for hidden in (
        crop.region.region_id,
        crop.image_sha256,
        crop.page.image_sha256,
        crop.page.source_sha256,
        str(crop.image_path),
        str(crop.page.image_path),
    ):
        assert hidden not in prompt_text
    assert "候选" not in prompt_text
    assert "native" not in prompt_text.lower()
    assert "rowspan" in prompt_text
    assert "colspan" in prompt_text
    assert "<table>" in prompt_text
    assert "LaTeX" in prompt_text

    audit = backend.last_call_audit
    assert audit["outcome"] == "ok"
    assert audit["page"] == 1
    assert audit["region_id"] == crop.region.region_id
    assert audit["crop_sha256"] == crop.image_sha256
    assert audit["budget_before"] == 0
    assert audit["budget_after"] == 1
    assert audit["cloud_requests"] == 1
    assert audit["uncertain"] is False
    assert audit["raw_response_sha256"] == hashlib.sha256(raw.encode()).hexdigest()


def test_exhausted_budget_sends_nothing(tmp_path: Path) -> None:
    crop = _crop(tmp_path)
    calls: list[httpx.Request] = []
    client = _client([], calls)
    backend = GeminiRegionBackend(_config(), client=client)
    budget = VisionRequestBudget(limit=0)

    result = backend(crop, kind="text", budget=budget)

    assert calls == []
    assert budget.images_sent == 0
    assert result.markdown is None
    assert result.uncertain is True
    assert result.errors == ["budget_exhausted"]
    assert result.usage.cloud_requests == 0
    assert result.usage.prompt_tokens is None
    assert result.usage.completion_tokens is None
    assert result.usage.total_tokens is None
    assert backend.last_call_audit["outcome"] == "budget_rejected"
    assert backend.last_call_audit["cloud_requests"] == 0


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ('{"markdown": "x", "uncertain": false,}', "response_json_invalid"),
        ('{"markdown": "x", "markdown": "y", "uncertain": false}', "response_json_invalid"),
        ('{"markdown": "x", "uncertain": "false"}', "response_schema_invalid"),
        ('{"markdown": "x", "uncertain": false, "extra": 1}', "response_schema_invalid"),
        ("[]", "response_schema_invalid"),
        ('{"markdown": "x", "uncertain": false, "total": NaN}', "response_json_invalid"),
    ],
)
def test_malformed_response_is_preserved_without_retry(
    tmp_path: Path,
    gemini_env: pytest.MonkeyPatch,
    raw: str,
    code: str,
) -> None:
    crop = _crop(tmp_path)
    calls: list[httpx.Request] = []
    client = _client([_completion(raw)], calls)
    backend = GeminiRegionBackend(_config(), client=client)
    budget = VisionRequestBudget(limit=2)

    result = backend(crop, kind="text", budget=budget)

    assert len(calls) == 1
    assert budget.images_sent == 1
    assert result.raw_response == raw
    assert result.markdown is None
    assert result.uncertain is True
    assert result.errors == [code]
    assert result.usage.cloud_requests == 1
    assert result.usage.prompt_tokens is None
    assert backend.last_call_audit["outcome"] == "invalid_response"


def test_rate_limit_waits_at_least_180_and_counts_both_attempts(
    tmp_path: Path,
    gemini_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crop = _crop(tmp_path)
    raw = _json_content("ok", False)
    calls: list[httpx.Request] = []
    responses = [
        httpx.Response(429, headers={"retry-after": "5"}, json={"error": "limited"}),
        _completion(raw, usage=USAGE),
    ]
    client = _client(responses, calls)
    backend = GeminiRegionBackend(
        _config(rate_limit_min_wait_seconds=1.0),
        client=client,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(visual_module.time, "sleep", sleeps.append)
    budget = VisionRequestBudget(limit=5)

    result = backend(crop, kind="text", budget=budget)

    assert len(calls) == 2
    assert budget.images_sent == 2
    assert sum(sleeps) >= 180
    assert len(sleeps) > 1
    assert result.markdown == "ok"
    assert result.usage.cloud_requests == 2
    assert result.finish_reason == "stop"
    assert backend.last_call_audit["cloud_requests"] == 2
    assert backend.last_call_audit["outcome"] == "ok"


def test_retry_stops_before_send_when_shared_budget_is_exhausted(
    tmp_path: Path,
    gemini_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crop = _crop(tmp_path)
    calls: list[httpx.Request] = []
    responses = [httpx.Response(429, headers={"retry-after": "0"}, json={})]
    client = _client(responses, calls)
    backend = GeminiRegionBackend(_config(), client=client)
    sleeps: list[float] = []
    monkeypatch.setattr(visual_module.time, "sleep", sleeps.append)
    budget = VisionRequestBudget(limit=1)

    result = backend(crop, kind="text", budget=budget)

    assert len(calls) == 1
    assert budget.images_sent == 1
    assert sum(sleeps) >= 180
    assert result.markdown is None
    assert result.uncertain is True
    assert result.errors == ["budget_exhausted"]
    assert result.usage.cloud_requests == 1
    assert backend.last_call_audit["outcome"] == "budget_rejected"


def test_tampered_crop_or_parent_evidence_sends_nothing(
    tmp_path: Path, gemini_env: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []
    client = _client([], calls)
    backend = GeminiRegionBackend(_config(), client=client)

    crop = _crop(tmp_path / "crop-tamper")
    crop.image_path.write_bytes(_png(crop.width, crop.height, offset=99))
    result = backend(crop, kind="text", budget=VisionRequestBudget(limit=2))
    assert result.errors == ["crop_evidence_invalid"]
    assert result.usage.cloud_requests == 0
    assert backend.last_call_audit["outcome"] == "evidence_rejected"

    parent = _crop(tmp_path / "parent-tamper")
    parent.page.image_path.write_bytes(
        _png(parent.page.width, parent.page.height, offset=123)
    )
    result = backend(parent, kind="text", budget=VisionRequestBudget(limit=2))
    assert result.errors == ["page_evidence_invalid"]
    assert result.usage.cloud_requests == 0
    assert backend.last_call_audit["outcome"] == "evidence_rejected"

    assert calls == []


def test_truncated_response_keeps_raw_record_without_retry(
    tmp_path: Path, gemini_env: pytest.MonkeyPatch
) -> None:
    crop = _crop(tmp_path)
    raw = _json_content("partial", False)
    calls: list[httpx.Request] = []
    client = _client([_completion(raw, finish_reason="length", usage=USAGE)], calls)
    backend = GeminiRegionBackend(_config(), client=client)
    budget = VisionRequestBudget(limit=3)

    result = backend(crop, kind="text", budget=budget)

    assert len(calls) == 1
    assert budget.images_sent == 1
    assert result.markdown is None
    assert result.raw_response == raw
    assert result.uncertain is True
    assert result.errors == ["response_truncated"]
    assert result.finish_reason == "length"
    assert result.usage.cloud_requests == 1
    assert result.usage.prompt_tokens == 11
    assert backend.last_call_audit["outcome"] == "truncated"
    assert backend.last_call_audit["raw_response_length"] == len(raw)


def test_absent_finish_reason_is_not_verified_eos(
    tmp_path: Path, gemini_env: pytest.MonkeyPatch
) -> None:
    crop = _crop(tmp_path)
    raw = _json_content("candidate", False)
    calls: list[httpx.Request] = []
    client = _client([_completion(raw, finish_reason=None)], calls)
    backend = GeminiRegionBackend(_config(), client=client)

    result = backend(crop, kind="text", budget=VisionRequestBudget(limit=2))

    assert result.markdown == "candidate"
    assert result.uncertain is True
    assert result.errors == ["finish_reason_missing"]
    assert result.finish_reason is None
    assert result.usage.cloud_requests == 1
    assert backend.last_call_audit["outcome"] == "uncertain"


def test_identity_and_audit_do_not_expose_key_endpoint_or_content(
    tmp_path: Path, gemini_env: pytest.MonkeyPatch
) -> None:
    crop = _crop(tmp_path)
    raw = _json_content("SECRET-MARKDOWN-CONTENT", False)
    calls: list[httpx.Request] = []
    client = _client([_completion(raw, usage=USAGE)], calls)
    backend = GeminiRegionBackend(_config(), client=client)

    result = backend(crop, kind="text", budget=VisionRequestBudget(limit=2))

    audit = backend.last_call_audit
    serialized = json.dumps(audit, ensure_ascii=False)
    for secret in (
        API_KEY,
        BASE_URL,
        "Authorization",
        "api_key",
        "SECRET-MARKDOWN-CONTENT",
    ):
        assert secret not in serialized
    for identity in (
        backend.version,
        backend.model,
        result.model_revision,
        result.prompt_revision,
    ):
        assert API_KEY not in identity
        assert BASE_URL not in identity
    assert audit["raw_response_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert audit["crop_sha256"] == crop.image_sha256
    assert "raw_response" not in audit
    assert calls[0].headers["authorization"] == f"Bearer {API_KEY}"
