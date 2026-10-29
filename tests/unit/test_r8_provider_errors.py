"""R8 guard cases: provider error taxonomy, bounded retry and circuit breaker.

Report case 090 and the coverage 429s: a 429/timeout/auth/quota/parse failure must
be classified, retried only when recoverable, and never rendered as "no evidence".
"""

from __future__ import annotations

import httpx
import pytest

from campusguide.agents.providers import (
    AUTH,
    CIRCUIT_OPEN,
    PARSE,
    QUOTA,
    RATE_LIMIT,
    SERVER,
    TIMEOUT,
    ChatProviderError,
    OpenAICompatibleChat,
    classify_error,
)


def _client(handler, **kwargs) -> OpenAICompatibleChat:
    params = {
        "base_url": "https://llm.example.com/v1",
        "api_key": "test-key",
        "model": "test-model",
        "transport": httpx.MockTransport(handler),
        "max_retries": 2,
        "backoff_base": 0.0,
        "sleep": lambda _seconds: None,
    }
    params.update(kwargs)
    return OpenAICompatibleChat(**params)


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


@pytest.mark.parametrize(
    "status,text,expected",
    [
        (429, "rate limited", RATE_LIMIT),
        (401, "unauthorized", AUTH),
        (403, "forbidden", AUTH),
        (503, "upstream", SERVER),
        (400, "insufficient_quota", QUOTA),
        (400, "bad request", PARSE),
    ],
)
def test_status_classification(status: int, text: str, expected: str) -> None:
    assert classify_error(status, text) == expected


def test_timeout_and_quota_are_classified_from_message() -> None:
    assert classify_error(None, "request timed out") == TIMEOUT
    assert classify_error(None, "余额不足") == QUOTA


def test_rate_limit_retries_and_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "0"})
        return _ok()

    client = _client(handler)
    assert client.complete([{"role": "user", "content": "hi"}]).content == "ok"
    assert calls == 2
    assert client.circuit_state()["consecutive_failures"] == 0


def test_auth_failure_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(ChatProviderError) as excinfo:
        _client(handler).complete([{"role": "user", "content": "hi"}])
    assert calls == 1
    assert excinfo.value.kind == AUTH
    assert excinfo.value.retryable is False


def test_quota_failure_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="insufficient_quota")

    with pytest.raises(ChatProviderError) as excinfo:
        _client(handler).complete([{"role": "user", "content": "hi"}])
    assert calls == 1
    assert excinfo.value.kind == QUOTA


def test_backoff_uses_retry_after_then_caps() -> None:
    waits: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="slow down", headers={"Retry-After": "1.5"})

    with pytest.raises(ChatProviderError):
        _client(handler, max_retries=2, sleep=waits.append).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert waits == [1.5, 1.5]


def test_circuit_opens_after_consecutive_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = _client(handler, max_retries=0, circuit_threshold=2, circuit_cooldown=60.0)
    for _ in range(2):
        with pytest.raises(ChatProviderError):
            client.complete([{"role": "user", "content": "hi"}])
    with pytest.raises(ChatProviderError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == CIRCUIT_OPEN
    assert excinfo.value.retryable is False


def test_circuit_recovers_after_cooldown() -> None:
    now = {"value": 0.0}
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="down") if calls == 1 else _ok()

    client = _client(
        handler,
        max_retries=0,
        circuit_threshold=1,
        circuit_cooldown=10.0,
        clock=lambda: now["value"],
    )
    with pytest.raises(ChatProviderError):
        client.complete([{"role": "user", "content": "hi"}])
    now["value"] = 11.0
    assert client.complete([{"role": "user", "content": "hi"}]).content == "ok"


def test_parse_failure_is_terminal_and_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(ChatProviderError) as excinfo:
        _client(handler).complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == PARSE
    assert excinfo.value.as_dict()["retryable"] is False
# --------------------------------------------------- 429 quota vs rate limit (A7)


def test_429_insufficient_quota_is_not_retried() -> None:
    calls = 0
    body = '{"error": {"code": "insufficient_quota", "message": "quota exhausted"}}'

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text=body)

    with pytest.raises(ChatProviderError) as excinfo:
        _client(handler).complete([{"role": "user", "content": "hi"}])
    assert calls == 1
    assert excinfo.value.kind == QUOTA
    assert excinfo.value.retryable is False


def test_structured_code_wins_over_status() -> None:
    body = '{"error": {"type": "insufficient_quota"}}'
    assert classify_error(429, body) == QUOTA
    assert classify_error(429, '{"error": {"code": "rate_limit_exceeded"}}') == RATE_LIMIT


def test_retry_after_http_date_is_parsed() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    target = datetime.now(timezone.utc) + timedelta(seconds=30)
    raw = format_datetime(target, usegmt=True)
    response = httpx.Response(429, headers={"Retry-After": raw})
    delay = OpenAICompatibleChat._retry_after(response)
    assert delay is not None and 25 <= delay <= 31


def test_retry_wait_is_not_truncated_to_backoff_cap() -> None:
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down", headers={"Retry-After": "20"})

    with pytest.raises(ChatProviderError):
        _client(handler, max_retries=1, sleep=waits.append, backoff_cap=8.0).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert waits == [20.0]


def test_retry_budget_stops_before_an_overlong_wait() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="slow down", headers={"Retry-After": "600"})

    with pytest.raises(ChatProviderError) as excinfo:
        _client(handler, max_retries=2, retry_budget=30.0).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert calls == 1
    assert excinfo.value.retryable is False


def test_attempt_log_records_kind_attempts_and_recovery() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="down")
        return _ok()

    client = _client(handler, backoff_base=0.0)
    client.complete([{"role": "user", "content": "hi"}])
    log = client.attempt_log()
    assert log[-1]["outcome"] == "ok"
    assert client.last_recovered is True
    assert client.last_error is None