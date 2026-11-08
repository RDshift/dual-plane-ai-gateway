"""MCP security gateway router exposing POST /mcp.

Interface: authenticates a Bearer token, validates the JSON-RPC 2.0 envelope,
blocks admin_ tool calls for non-admin callers before they reach the
downstream process, and forwards everything else through stdio_bridge.

Auth contract: unauthenticated callers (missing, malformed, empty or unknown
Bearer token) receive HTTP 401 with WWW-Authenticate and JSON-RPC -32000.
Authenticated callers denied by role receive HTTP 200 with JSON-RPC -32001.
Every response carries X-Request-Id (echoed from the request or generated).

Dependencies: gateway.core.config, gateway.core.errors and
gateway.mcp_plane.stdio_bridge.
"""

import hmac
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from gateway.core.config import settings
from gateway.core.errors import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_AUTH_REQUIRED,
    JSONRPC_PARSE_ERROR,
    MCP_UNAUTHORIZED_TOOL_CALL,
    build_jsonrpc_error,
)
from gateway.mcp_plane.stdio_bridge import (
    BridgeError,
    BridgeTimeoutError,
    stdio_bridge,
)

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
FALLBACK_TOKEN_ROLES = {"admin": ROLE_ADMIN, "viewer": ROLE_VIEWER}
ADMIN_TOOL_PREFIX = "admin_"
JSONRPC_VERSION = "2.0"
FORWARDED_METHODS = frozenset({"initialize", "ping", "tools/list", "tools/call"})
TOOLS_CALL_METHOD = "tools/call"

HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_BAD_GATEWAY = 502
HTTP_GATEWAY_TIMEOUT = 504

HEADER_REQUEST_ID = "X-Request-Id"
HEADER_WWW_AUTHENTICATE = "WWW-Authenticate"
WWW_AUTHENTICATE_VALUE = 'Bearer realm="mcp-plane"'

MSG_UNAUTHORIZED_TOKEN = "Authentication required: missing, malformed, or invalid Bearer token."
MSG_UNAUTHORIZED_TOOL = "Unauthorized Tool Call"
MSG_PARSE_ERROR = "Parse error"
MSG_INVALID_REQUEST = "Invalid Request"
MSG_METHOD_NOT_FOUND = "Method not found"
MSG_INVALID_PARAMS = "Invalid params"
MSG_UPSTREAM_TIMEOUT = "Downstream MCP server timed out"
MSG_UPSTREAM_UNAVAILABLE = "Downstream MCP server unavailable"

logger = logging.getLogger(__name__)

mcp_router = APIRouter(tags=["mcp_plane"])


def _resolve_role(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    known_tokens = {**FALLBACK_TOKEN_ROLES, **settings.token_registry}
    presented = token.encode(encoding="utf-8")
    resolved: str | None = None
    for known_token, role in known_tokens.items():
        if hmac.compare_digest(known_token.encode(encoding="utf-8"), presented):
            resolved = role
    return resolved


def resolve_bearer_role(authorization: str | None) -> str | None:
    """Public alias used by other routes that share the MCP token registry."""
    return _resolve_role(authorization)


def _extract_request_id(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("id")
    if isinstance(candidate, bool):
        return None
    if candidate is None or isinstance(candidate, (str, int)):
        return candidate
    return None


def _resolve_request_id_header(request: Request) -> str:
    supplied = request.headers.get(HEADER_REQUEST_ID, "").strip()
    return supplied or uuid.uuid4().hex


def _error_response(
    request_id: Any,
    code: int,
    message: str,
    request_id_hdr: str,
    status_code: int = HTTP_OK,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=build_jsonrpc_error(request_id, code, message),
        headers={HEADER_REQUEST_ID: request_id_hdr, **(extra_headers or {})},
    )


def _is_valid_envelope(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("jsonrpc") != JSONRPC_VERSION:
        return False
    if not isinstance(payload.get("method"), str):
        return False
    if "id" in payload:
        candidate = payload["id"]
        if isinstance(candidate, bool):
            return False
        if candidate is not None and not isinstance(candidate, (str, int)):
            return False
    return True


def _validate_tool_call(params: Any) -> str | None:
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return None
    return name


def _is_admin_tool(name: str) -> bool:
    return name.strip().casefold().startswith(ADMIN_TOOL_PREFIX)


@mcp_router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Authenticate, authorize and forward one JSON-RPC request to the MCP server."""
    request_id_hdr = _resolve_request_id_header(request)
    raw_body = await request.body()
    parsed_ok = True
    payload: Any = None
    try:
        payload = json.loads(raw_body)
    except ValueError:
        parsed_ok = False

    request_id = _extract_request_id(payload)

    role = _resolve_role(authorization)
    if role is None:
        logger.warning(
            "event=mcp_auth_failed request_id=%s reason=%s",
            request_id_hdr,
            "missing_header" if not authorization else "invalid_token",
        )
        return _error_response(
            request_id,
            JSONRPC_AUTH_REQUIRED,
            MSG_UNAUTHORIZED_TOKEN,
            request_id_hdr,
            HTTP_UNAUTHORIZED,
            {HEADER_WWW_AUTHENTICATE: WWW_AUTHENTICATE_VALUE},
        )

    if not parsed_ok:
        return _error_response(
            None, JSONRPC_PARSE_ERROR, MSG_PARSE_ERROR, request_id_hdr, HTTP_BAD_REQUEST
        )

    if not _is_valid_envelope(payload):
        return _error_response(
            request_id,
            JSONRPC_INVALID_REQUEST,
            MSG_INVALID_REQUEST,
            request_id_hdr,
            HTTP_BAD_REQUEST,
        )

    method = payload["method"]
    if method not in FORWARDED_METHODS:
        if "id" not in payload and method.startswith("notifications/"):
            return Response(status_code=HTTP_ACCEPTED, headers={HEADER_REQUEST_ID: request_id_hdr})
        return _error_response(
            request_id, JSONRPC_METHOD_NOT_FOUND, MSG_METHOD_NOT_FOUND, request_id_hdr
        )

    if "id" not in payload:
        return Response(status_code=HTTP_ACCEPTED, headers={HEADER_REQUEST_ID: request_id_hdr})

    if method == TOOLS_CALL_METHOD:
        tool_name = _validate_tool_call(payload.get("params"))
        if tool_name is None:
            return _error_response(
                request_id, JSONRPC_INVALID_PARAMS, MSG_INVALID_PARAMS, request_id_hdr
            )
        if _is_admin_tool(tool_name) and role != ROLE_ADMIN:
            logger.warning(
                "event=mcp_rbac_denied request_id=%s role=%s tool=%s",
                request_id_hdr,
                role,
                tool_name,
            )
            return _error_response(
                request_id, MCP_UNAUTHORIZED_TOOL_CALL, MSG_UNAUTHORIZED_TOOL, request_id_hdr
            )

    try:
        downstream_response = await stdio_bridge.exchange(payload)
    except BridgeTimeoutError:
        return _error_response(
            request_id,
            JSONRPC_INTERNAL_ERROR,
            MSG_UPSTREAM_TIMEOUT,
            request_id_hdr,
            HTTP_GATEWAY_TIMEOUT,
        )
    except BridgeError:
        return _error_response(
            request_id,
            JSONRPC_INTERNAL_ERROR,
            MSG_UPSTREAM_UNAVAILABLE,
            request_id_hdr,
            HTTP_BAD_GATEWAY,
        )
    return JSONResponse(
        status_code=HTTP_OK,
        content=downstream_response,
        headers={HEADER_REQUEST_ID: request_id_hdr},
    )
