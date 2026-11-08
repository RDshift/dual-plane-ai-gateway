"""Milestone 2 integration tests for the MCP security gateway proxy.

Interface: drives POST /mcp through the gateway_client fixture and inspects
stdio_bridge.downstream_call_count to prove which requests reached the child
MCP server process.

Dependencies: tests.conftest fixtures, gateway.core.config and
gateway.mcp_plane.stdio_bridge.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.core.config import settings
from gateway.core.errors import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_AUTH_REQUIRED,
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
MISSING_TOKEN_MESSAGE = "Authentication required: missing, malformed, or invalid Bearer token."
REQUEST_ID_HEADER = "X-Request-Id"
WWW_AUTHENTICATE_HEADER = "WWW-Authenticate"


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
    extra_headers: dict[str, str] | None = None,
):
    return client.post(
        MCP_PATH,
        json=_rpc(method, params, request_id),
        headers={**_headers(role), **(extra_headers or {})},
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


def _assert_unauthenticated(response: Any, rpc_id: Any) -> None:
    assert response.status_code == 401
    assert response.headers[WWW_AUTHENTICATE_HEADER] == 'Bearer realm="mcp-plane"'
    assert response.headers[REQUEST_ID_HEADER]
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == rpc_id
    assert body["error"]["code"] == JSONRPC_AUTH_REQUIRED
    assert body["error"]["message"] == MISSING_TOKEN_MESSAGE


def test_missing_bearer_header_returns_401(gateway_client: TestClient) -> None:
    before = stdio_bridge.downstream_call_count
    response = gateway_client.post(MCP_PATH, json=_rpc("tools/list", request_id=11))
    _assert_unauthenticated(response, 11)
    assert stdio_bridge.downstream_call_count == before


@pytest.mark.parametrize(
    "header_value",
    ["Basic abc", "Bearer ", "Bearer unknown_token_xyz", "tok_viewer_live_3c4d5e"],
)
def test_invalid_bearer_header_returns_401(
    gateway_client: TestClient, header_value: str
) -> None:
    before = stdio_bridge.downstream_call_count
    response = gateway_client.post(
        MCP_PATH,
        json=_rpc("tools/list", request_id=12),
        headers={"Authorization": header_value},
    )
    _assert_unauthenticated(response, 12)
    assert stdio_bridge.downstream_call_count == before


def test_unauthenticated_with_unparseable_body_uses_null_id(gateway_client: TestClient) -> None:
    response = gateway_client.post(
        MCP_PATH, content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _assert_unauthenticated(response, None)


def test_request_id_header_is_echoed_or_generated(gateway_client: TestClient) -> None:
    echoed = _post(
        gateway_client, "viewer", "tools/list", request_id=13, extra_headers={REQUEST_ID_HEADER: "trace-abc"}
    )
    assert echoed.status_code == 200
    assert echoed.headers[REQUEST_ID_HEADER] == "trace-abc"
    generated = _post(gateway_client, "viewer", "tools/list", request_id=14)
    assert generated.status_code == 200
    assert len(generated.headers[REQUEST_ID_HEADER]) == 32


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
    assert response.headers[REQUEST_ID_HEADER]
    assert WWW_AUTHENTICATE_HEADER not in response.headers
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


if __name__ == "__main__":
    from tests.conftest import run_module_tests

    raise SystemExit(run_module_tests(__file__))
