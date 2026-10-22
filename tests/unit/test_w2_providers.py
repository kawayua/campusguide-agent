from __future__ import annotations

import httpx
import pytest

from campusguide.agents.providers import (
    ChatConfigurationError,
    ChatProviderError,
    OpenAICompatibleChat,
)


def _client(handler) -> OpenAICompatibleChat:
    return OpenAICompatibleChat(
        base_url="https://llm.example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        max_retries=1,
    )


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "choices": [{"message": {"content": '{"claims": []}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_missing_config_is_rejected_at_startup() -> None:
    with pytest.raises(ChatConfigurationError):
        OpenAICompatibleChat(base_url="", api_key="", model="")


def test_complete_parses_openai_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ok_response()

    response = _client(handler).complete([{"role": "user", "content": "hi"}])
    assert response.content == '{"claims": []}'
    assert response.prompt_tokens == 10
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    assert requests[0].url.path == "/v1/chat/completions"


def test_complete_retries_once_on_500() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="upstream error")
        return _ok_response()

    response = _client(handler).complete([{"role": "user", "content": "hi"}])
    assert calls == 2
    assert response.content


def test_complete_does_not_retry_on_400() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    with pytest.raises(ChatProviderError):
        _client(handler).complete([{"role": "user", "content": "hi"}])
    assert calls == 1


def test_complete_raises_on_invalid_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(ChatProviderError):
        _client(handler).complete([{"role": "user", "content": "hi"}])
