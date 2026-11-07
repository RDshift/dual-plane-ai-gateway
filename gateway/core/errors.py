"""Standardized JSON-RPC 2.0 and Gateway HTTP error envelopes."""

from typing import Any
from pydantic import BaseModel

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
MCP_UNAUTHORIZED_TOOL_CALL = -32001


class JsonRpcErrorObject(BaseModel):
    """Wire structure for JSON-RPC 2.0 error detail."""

    code: int
    message: str
    data: Any | None = None


class JsonRpcErrorResponse(BaseModel):
    """Top-level JSON-RPC 2.0 error response."""

    jsonrpc: str = "2.0"
    id: str | int | None = None
    error: JsonRpcErrorObject


class SanitizedGatewayError(BaseModel):
    """Sanitized error envelope preventing upstream stack trace leakage."""

    error: dict[str, Any]


def build_jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    """Construct a compliant JSON-RPC 2.0 error dictionary."""
    resp = JsonRpcErrorResponse(
        id=request_id,
        error=JsonRpcErrorObject(code=code, message=message, data=data),
    )
    payload = resp.model_dump()
    if payload["error"].get("data") is None:
        del payload["error"]["data"]
    return payload


def build_gateway_error(
    code: str,
    message: str,
    http_status: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Construct a sanitized HTTP error envelope for the Model Inference Plane."""
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "status": http_status,
    }
    if request_id:
        payload["request_id"] = request_id
    return {"error": payload}
