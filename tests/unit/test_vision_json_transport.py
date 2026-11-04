"""Transport-level JSON mode, truncation and bbox guards for vision recognition.

Only httpx.MockTransport is used; these tests make no network calls.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from campusguide.ingest import visual
from campusguide.ingest.visual import (
    VISION_JSON_RESPONSE_FORMAT,
    VISION_JSON_TRANSPORT_VERSION,
    GeminiVisionProvider,
    VisionConfig,
    VisionResponseError,
    VisionValidationError,
)


@pytest.fixture
def vision_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_VISION_MODEL", "json-transport-model")
    monkeypatch.setenv("TEST_VISION_BASE_URL", "https://vision.invalid/v1")
    monkeypatch.setenv("TEST_VISION_API_KEY", "test-key")


def _config(tmp_path, **overrides: Any) -> VisionConfig:
    values: dict[str, Any] = {
        "cache_dir": tmp_path / "cache",
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "model_env": "TEST_VISION_MODEL",
        "fallback_model_env": "TEST_VISION_FALLBACK_MODEL",
        "base_url_env": "TEST_VISION_BASE_URL",
        "fallback_base_url_envs": (),
        "api_key_env": "TEST_VISION_API_KEY",
        "fallback_api_key_envs": (),
    }
    values.update(overrides)
    return VisionConfig(**values)


def _completion(
    content: str,
    *,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    choice: dict[str, Any] = {"message": {"content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    body: dict[str, Any] = {"choices": [choice]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def test_page_and_asset_requests_send_json_object_response_format(
    tmp_path, vision_env
) -> None:
    requests: list[dict[str, Any]] = []
    replies = iter(
        [
            json.dumps(
                {
                    "page": 1,
                    "blocks": [
                        {"id": "b1", "kind": "paragraph", "text": "正文"}
                    ],
                },
                ensure_ascii=False,
            ),
            json.dumps({"has_text": False, "blocks": [], "warnings": []}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _completion(next(replies), finish_reason="stop")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiVisionProvider(_config(tmp_path), client=client)
        page = provider.recognize_page(
            b"png-bytes", page_no=1, context={"image_sha256": "a" * 64}
        )
        asset = provider.recognize_asset(
            b"png-bytes",
            asset_id="asset-1",
            mime_type="image/png",
            context={"image_sha256": "b" * 64},
        )

    assert page["page"] == 1
    assert asset == {"has_text": False, "blocks": [], "warnings": []}
    assert len(requests) == 2
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert requests[0]["response_format"] == VISION_JSON_RESPONSE_FORMAT


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
def test_truncated_page_response_is_rejected_without_retry(
    tmp_path, vision_env, finish_reason
) -> None:
    content = '{"page": 1, "blocks": ['
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _completion(
            content,
            finish_reason=finish_reason,
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "request_id": "must-not-persist",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiVisionProvider(
            _config(tmp_path, max_retries=2), client=client
        )
        with pytest.raises(VisionValidationError) as excinfo:
            provider.recognize_page(
                b"png-bytes", page_no=3, context={"image_sha256": "c" * 64}
            )

    assert excinfo.value.code == "response_truncated"
    assert excinfo.value.page_no == 3
    assert len(requests) == 1
    assert provider.requests_sent == 1
    raw = provider.raw_responses[-1]
    assert raw["page"] == 3
    assert raw["content"] == content
    assert raw["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert raw["finish_reason"] == finish_reason
    assert raw["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert "must-not-persist" not in json.dumps(raw, ensure_ascii=False)


def test_truncated_asset_response_is_rejected_and_audited(
    tmp_path, vision_env
) -> None:
    content = '{"has_text": true, "blocks": ['
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _completion(content, finish_reason="length")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiVisionProvider(_config(tmp_path), client=client)
        with pytest.raises(VisionValidationError) as excinfo:
            provider.recognize_asset(
                b"png-bytes",
                asset_id="asset-1",
                mime_type="image/png",
                context={"image_sha256": "d" * 64},
            )

    assert excinfo.value.code == "response_truncated"
    assert len(requests) == 1
    raw = provider.raw_responses[-1]
    assert raw["asset_id"] == "asset-1"
    assert raw["finish_reason"] == "length"
    assert raw["usage"] == {}


def test_existing_bbox_gate_still_rejects_invalid_coordinate(
    tmp_path, vision_env
) -> None:
    content = json.dumps(
        {
            "page": 1,
            "coordinate_unit": "normalized",
            "blocks": [
                {
                    "id": "b1",
                    "kind": "paragraph",
                    "text": "正文",
                    "bbox": [0, 0, 1.2, 1],
                }
            ],
            "tables": [],
            "images": [],
            "warnings": [],
        },
        ensure_ascii=False,
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _completion(content, finish_reason="stop")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiVisionProvider(_config(tmp_path), client=client)
        with pytest.raises(VisionValidationError) as excinfo:
            provider.recognize_page(
                b"png-bytes", page_no=1, context={"image_sha256": "e" * 64}
            )

    assert excinfo.value.code == "bbox_out_of_bounds"
    assert len(requests) == 1
    assert provider.raw_responses[-1]["finish_reason"] == "stop"


def test_json_mode_rejection_stays_a_provider_failure_without_fallback(
    tmp_path, vision_env
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(400, text='{"error": "response_format unsupported"}')

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiVisionProvider(
            _config(tmp_path, max_retries=0), client=client
        )
        with pytest.raises(VisionResponseError) as excinfo:
            provider.recognize_page(
                b"png-bytes", page_no=1, context={"image_sha256": "f" * 64}
            )

    assert not isinstance(excinfo.value, VisionValidationError)
    assert "HTTP 400" in str(excinfo.value)
    assert len(requests) == 1
    assert requests[0]["response_format"] == VISION_JSON_RESPONSE_FORMAT


def test_json_transport_marker_versions_provider_and_prompt_identity(
    tmp_path, vision_env
) -> None:
    provider = GeminiVisionProvider(_config(tmp_path))

    assert VISION_JSON_TRANSPORT_VERSION in provider.version
    assert provider.prompt_sha256() == visual._prompt_sha256()

    legacy_page_prompt = "\n".join(
        [
            visual._VISION_SYSTEM_PROMPT,
            visual._VISION_JSON_SCHEMA,
            visual._VISION_RULES,
        ]
    )
    legacy_asset_prompt = "\n".join(
        [
            visual._ASSET_SYSTEM_PROMPT,
            visual._ASSET_JSON_SCHEMA,
            visual._ASSET_RULES,
        ]
    )
    assert provider.prompt_sha256() != hashlib.sha256(
        legacy_page_prompt.encode("utf-8")
    ).hexdigest()
    assert visual._asset_prompt_sha256() != hashlib.sha256(
        legacy_asset_prompt.encode("utf-8")
    ).hexdigest()
