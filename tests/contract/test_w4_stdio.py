from __future__ import annotations

import sys
from pathlib import Path

import pytest

from campusguide.mcp.client import MCPClientConfig, MCPPolicySearchClient
from campusguide.mcp.schemas import ErrorCode, SearchArticlesRequest, ToolError

SCRIPT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_search_server.py"


@pytest.mark.integration
def test_stdio_client_lifecycle_retry_and_close() -> None:
    config = MCPClientConfig(
        command=[sys.executable, str(SCRIPT)],
        timeout_seconds=10,
        retries=1,
        startup_timeout_seconds=30,
    )
    client = MCPPolicySearchClient(config)
    try:
        response = client.search_articles(SearchArticlesRequest(query="奖学金"))
    finally:
        client.close()

    assert response.data.index_version == "fake@stdio"
    assert response.data.articles == []
    assert response.retries == 1


@pytest.mark.integration
def test_stdio_client_maps_not_found_envelope() -> None:
    config = MCPClientConfig(
        command=[sys.executable, str(SCRIPT)],
        timeout_seconds=10,
        retries=0,
        startup_timeout_seconds=30,
    )
    client = MCPPolicySearchClient(config)
    try:
        with pytest.raises(ToolError) as raised:
            client.get_article("missing@v1#第一条")
    finally:
        client.close()
    assert raised.value.code is ErrorCode.NOT_FOUND


@pytest.mark.integration
def test_stdio_client_sanitizes_schema_validation_errors() -> None:
    config = MCPClientConfig(
        command=[sys.executable, str(SCRIPT)],
        timeout_seconds=10,
        retries=0,
        startup_timeout_seconds=30,
    )
    client = MCPPolicySearchClient(config)
    try:
        with pytest.raises(ToolError) as raised:
            client.call("search_articles", {"request": {"query": 123}})
    finally:
        client.close()
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert "validation errors" not in raised.value.message
    assert "pydantic" not in raised.value.message.lower()


@pytest.mark.integration
def test_stdio_client_reports_missing_flow_tool_as_w5() -> None:
    config = MCPClientConfig(
        command=[sys.executable, str(SCRIPT)],
        timeout_seconds=10,
        retries=0,
        startup_timeout_seconds=30,
    )
    client = MCPPolicySearchClient(config)
    try:
        with pytest.raises(ToolError) as raised:
            client.call("get_flow")
    finally:
        client.close()
    assert raised.value.code is ErrorCode.NOT_IMPLEMENTED
    assert "W5" in raised.value.message
