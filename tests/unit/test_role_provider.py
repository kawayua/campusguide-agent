from __future__ import annotations

from campusguide.qa.runtime import _advisor, _role_provider


def _clear(monkeypatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_role_provider_falls_back_to_global(monkeypatch) -> None:
    _clear(
        monkeypatch,
        "ADVISOR_LLM_BASE_URL",
        "ADVISOR_LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_API_KEY",
    )
    monkeypatch.setenv("LLM_BASE_URL", "https://global.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "global-key")
    monkeypatch.setenv("ADVISOR_MODEL", "global-model")

    provider = _role_provider("ADVISOR_MODEL", 5.0)

    assert provider._base_url == "https://global.example/v1"
    assert provider._api_key == "global-key"
    assert provider.model == "global-model"


def test_role_provider_overrides_advisor_only(monkeypatch) -> None:
    _clear(monkeypatch, "PLANNER_LLM_BASE_URL", "PLANNER_LLM_API_KEY")
    monkeypatch.setenv("LLM_BASE_URL", "https://global.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "global-key")
    monkeypatch.setenv("ADVISOR_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("ADVISOR_LLM_API_KEY", "relay-key")
    monkeypatch.setenv("ADVISOR_MODEL", "relay-model")
    monkeypatch.setenv("PLANNER_MODEL", "planner-model")

    advisor = _role_provider("ADVISOR_MODEL", 5.0)
    planner = _role_provider("PLANNER_MODEL", 5.0)

    assert advisor._base_url == "https://relay.example/v1"
    assert advisor._api_key == "relay-key"
    assert advisor.model == "relay-model"
    assert planner._base_url == "https://global.example/v1"
    assert planner._api_key == "global-key"
    assert planner.model == "planner-model"


def test_role_provider_partial_override_keeps_global_key(monkeypatch) -> None:
    _clear(monkeypatch, "ADVISOR_LLM_API_KEY")
    monkeypatch.setenv("LLM_BASE_URL", "https://global.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "global-key")
    monkeypatch.setenv("ADVISOR_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("ADVISOR_MODEL", "relay-model")

    provider = _role_provider("ADVISOR_MODEL", 5.0)

    assert provider._base_url == "https://relay.example/v1"
    assert provider._api_key == "global-key"


def test_advisor_uses_role_provider(monkeypatch) -> None:
    """回归：W4/W5 的 _advisor 不能再绕过 per-role 覆盖。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://global.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "global-key")
    monkeypatch.setenv("ADVISOR_LLM_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("ADVISOR_LLM_API_KEY", "relay-key")
    monkeypatch.setenv("ADVISOR_MODEL", "relay-model")
    monkeypatch.setenv("ADVISOR_MAX_TOKENS", "123")

    advisor = _advisor()

    assert advisor._provider._base_url == "https://relay.example/v1"
    assert advisor._provider._api_key == "relay-key"
    assert advisor._provider.model == "relay-model"
    assert advisor._max_tokens == 123
