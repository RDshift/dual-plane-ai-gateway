"""Milestone 2 integration tests for the MCP security gateway proxy.

Interface: drives POST /mcp through the gateway_client fixture and inspects
stdio_bridge.downstream_call_count to prove which requests reached the child
MCP server process.

Dependencies: tests.conftest fixtures, gateway.core.config and
gateway.mcp_plane.stdio_bridge.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.core.config import settings
from gateway.core.errors import (
    JSONRPC_INVALID_PARAMS,
    MCP_UNAUTHORIZED_TOOL_CALL,
)
from gateway.mcp_plane.stdio_bridge import stdio_bridge

pytestmark = pytest.mark.usefixtures("isolated_sqlite_path")

MCP_PATH = "/mcp"
CUSTOMER_TOOL = "get_customer_record"
REFUND_TOOL = "trigger_refund"
ADMIN_TOOL = "admin_reset_key"
SAMPLE_CUSTOMER_ARGS = {"customer_id": "CUST-10001"}
SAMPLE_BAD_CUSTOMER_ARGS = {"customer_id": "CUST-BAD"}
SAMPLE_ADMIN_ARGS = {"target_service": "gateway_auth"}
BLOCKED_MESSAGE = "Unauthorized Tool Call"
MISSING_TOKEN_MESSAGE = "Unauthorized: Missing or invalid Bearer token."


def _token_for_role(role: str) -> str:
    for token, mapped_role in settings.token_registry.items():
        if mapped_role == role:
            return token
    raise AssertionError(f"No token registered for role {role}")


def _headers(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token_for_role(role)}"}


def _rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1):
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _post(
    client: TestClient,
    role: str,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
):
    return client.post(
        MCP_PATH,
        json=_rpc(method, params, request_id),
        headers=_headers(role),
    )


def _call_tool(
    client: TestClient,
    role: str,
    name: str,
    arguments: dict[str, Any],
    request_id: int = 1,
):
    return _post(
        client,
        role,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id,
    )


def test_missing_bearer_header_returns_401(gateway_client: TestClient) -> None:
    response = gateway_client.post(MCP_PATH, json=_rpc("tools/list"))
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == MCP_UNAUTHORIZED_TOOL_CALL
    assert error["message"] == MISSING_TOKEN_MESSAGE


@pytest.mark.parametrize(
    "header_value",
    ["Bearer not-a-real-token", "Bearer ", "Basic dXNlcjpwYXNz", "tok_viewer_live_3c4d5e"],
)
def test_invalid_bearer_header_returns_401(
    gateway_client: TestClient, header_value: str
) -> None:
    before = stdio_bridge.downstream_call_count
    response = gateway_client.post(
        MCP_PATH,
        json=_rpc("tools/list"),
        headers={"Authorization": header_value},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == MCP_UNAUTHORIZED_TOOL_CALL
    assert stdio_bridge.downstream_call_count == before


def test_viewer_tools_list_succeeds(gateway_client: TestClient) -> None:
    response = _post(gateway_client, "viewer", "tools/list", request_id=7)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 7
    assert "error" not in body
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert {CUSTOMER_TOOL, REFUND_TOOL} <= names


def test_viewer_can_call_get_customer_record(gateway_client: TestClient) -> None:
    before = stdio_bridge.downstream_call_count
    response = _call_tool(gateway_client, "viewer", CUSTOMER_TOOL, SAMPLE_CUSTOMER_ARGS)
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert "result" in body
    assert stdio_bridge.downstream_call_count == before + 1


def test_viewer_blocked_from_admin_tool_without_downstream_call(
    gateway_client: TestClient,
) -> None:
    before = stdio_bridge.downstream_call_count
    response = _call_tool(
        gateway_client, "viewer", ADMIN_TOOL, SAMPLE_ADMIN_ARGS, request_id=11
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 11
    assert body["error"]["code"] == MCP_UNAUTHORIZED_TOOL_CALL
    assert body["error"]["message"] == BLOCKED_MESSAGE
    assert stdio_bridge.downstream_call_count == before


@pytest.mark.parametrize(
    "tool_name", ["ADMIN_reset_key", " admin_reset_key", "Admin_Reset_Key"]
)
def test_viewer_block_ignores_case_and_padding(
    gateway_client: TestClient, tool_name: str
) -> None:
    before = stdio_bridge.downstream_call_count
    response = _call_tool(gateway_client, "viewer", tool_name, SAMPLE_ADMIN_ARGS)
    assert response.json()["error"]["code"] == MCP_UNAUTHORIZED_TOOL_CALL
    assert stdio_bridge.downstream_call_count == before


def test_admin_can_call_admin_tool_and_reaches_downstream(
    gateway_client: TestClient,
) -> None:
    before = stdio_bridge.downstream_call_count
    response = _call_tool(gateway_client, "admin", ADMIN_TOOL, SAMPLE_ADMIN_ARGS)
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert "result" in body
    assert stdio_bridge.downstream_call_count == before + 1


def test_malformed_tool_arguments_return_downstream_invalid_params(
    gateway_client: TestClient,
) -> None:
    before = stdio_bridge.downstream_call_count
    response = _call_tool(
        gateway_client, "viewer", CUSTOMER_TOOL, SAMPLE_BAD_CUSTOMER_ARGS
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert stdio_bridge.downstream_call_count == before + 1


def test_notification_is_acknowledged_without_downstream_call(
    gateway_client: TestClient,
) -> None:
    before = stdio_bridge.downstream_call_count
    response = gateway_client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_headers("viewer"),
    )
    assert response.status_code == 202
    assert stdio_bridge.downstream_call_count == before
