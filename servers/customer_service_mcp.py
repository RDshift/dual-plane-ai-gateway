"""Standalone MCP server over stdio transport with strict Pydantic validation."""

import asyncio
import json
import signal
import sys
from typing import Any

from mcp.server import Server
import mcp.types as mcp_types
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from gateway.core.config import settings
from gateway.core.errors import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    build_jsonrpc_error,
)
from gateway.core.logging import configure_stderr_logging

logger = configure_stderr_logging(
    level=settings.log_level,
    logger_name="quilr.mcp_server",
)

CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
MIN_REFUND_REASON_LENGTH = 10
MCP_PROTOCOL_VERSION = "2024-11-05"

mcp_server = Server("customer-service-mcp")


class GetCustomerRecordParams(BaseModel):
    """Strict input contract for get_customer_record."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str = Field(..., pattern=CUSTOMER_ID_PATTERN)


class TriggerRefundParams(BaseModel):
    """Strict input contract for trigger_refund."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str = Field(..., pattern=CUSTOMER_ID_PATTERN)
    amount: float = Field(..., gt=0.0)
    reason: str = Field(..., min_length=MIN_REFUND_REASON_LENGTH)

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("amount must be a numeric float or integer greater than 0.0")
        return float(value)


class AdminResetKeyParams(BaseModel):
    """Strict input contract for downstream administrative tool verification."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_service: str = Field(default="gateway_auth", min_length=2)


MOCK_CUSTOMER_DB: dict[str, dict[str, Any]] = {
    "CUST-10001": {
        "customer_id": "CUST-10001",
        "name": "Alice Vance",
        "tier": "enterprise",
        "status": "active",
        "balance_usd": 1420.50,
    },
    "CUST-00042": {
        "customer_id": "CUST-00042",
        "name": "Benjamin Carter",
        "tier": "founding",
        "status": "active",
        "balance_usd": 0.00,
    },
}


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return MCP Tool definitions serialized via official mcp.types.Tool."""
    tools = [
        mcp_types.Tool(
            name="get_customer_record",
            description="Retrieve a customer record by strict CUST-XXXXX identifier.",
            inputSchema=GetCustomerRecordParams.model_json_schema(),
        ),
        mcp_types.Tool(
            name="trigger_refund",
            description="Issue a positive refund with an audited reason string.",
            inputSchema=TriggerRefundParams.model_json_schema(),
        ),
    ]
    return [tool.model_dump(mode="json", exclude_none=True) for tool in tools]


def execute_validated_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate tool parameters via Pydantic and return MCP CallToolResult payload."""
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")

    if name == "get_customer_record":
        validated = GetCustomerRecordParams.model_validate(arguments)
        record = MOCK_CUSTOMER_DB.get(
            validated.customer_id,
            {
                "customer_id": validated.customer_id,
                "name": "Standard Account",
                "tier": "standard",
                "status": "active",
                "balance_usd": 250.00,
            },
        )
        content = mcp_types.TextContent(
            type="text",
            text=json.dumps(record, separators=(",", ":")),
        )
        return {
            "content": [content.model_dump(mode="json", exclude_none=True)],
            "isError": False,
        }

    if name == "trigger_refund":
        validated = TriggerRefundParams.model_validate(arguments)
        receipt = {
            "status": "approved",
            "customer_id": validated.customer_id,
            "refunded_amount": round(validated.amount, 2),
            "reason": validated.reason,
        }
        content = mcp_types.TextContent(
            type="text",
            text=json.dumps(receipt, separators=(",", ":")),
        )
        return {
            "content": [content.model_dump(mode="json", exclude_none=True)],
            "isError": False,
        }

    if name == "admin_reset_key":
        validated = AdminResetKeyParams.model_validate(arguments)
        audit = {
            "status": "rotated",
            "target_service": validated.target_service,
        }
        content = mcp_types.TextContent(
            type="text",
            text=json.dumps(audit, separators=(",", ":")),
        )
        return {
            "content": [content.model_dump(mode="json", exclude_none=True)],
            "isError": False,
        }

    raise KeyError(f"Unknown tool: {name}")


def handle_jsonrpc_message(raw_line: str) -> dict[str, Any] | None:
    """Process a single JSON-RPC 2.0 frame and return a response dict or None for notifications."""
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        logger.warning(f"Malformed JSON on stdio: {exc}")
        return build_jsonrpc_error(
            request_id=None,
            code=JSONRPC_PARSE_ERROR,
            message="Parse error: Invalid JSON payload.",
        )

    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        req_id = payload.get("id") if isinstance(payload, dict) else None
        return build_jsonrpc_error(
            request_id=req_id,
            code=JSONRPC_INVALID_REQUEST,
            message="Invalid Request: jsonrpc version must be '2.0'.",
        )

    method = payload.get("method")
    request_id = payload.get("id")
    is_notification = "id" not in payload

    if not isinstance(method, str) or not method:
        return build_jsonrpc_error(
            request_id=request_id,
            code=JSONRPC_INVALID_REQUEST,
            message="Invalid Request: missing method name.",
        )

    if method.startswith("notifications/"):
        logger.info(f"Processed notification: {method}")
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": mcp_server.name,
                    "version": "0.1.0",
                },
            },
        }

    if method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {},
        }

    if method == "tools/list":
        logger.info("Serving tools/list request.")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": get_tool_definitions(),
            },
        }

    if method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, dict):
            return build_jsonrpc_error(
                request_id=request_id,
                code=JSONRPC_INVALID_PARAMS,
                message="Invalid params: 'params' must be an object.",
            )

        tool_name = params.get("name")
        tool_args = params.get("arguments", {})

        if not isinstance(tool_name, str) or not tool_name:
            return build_jsonrpc_error(
                request_id=request_id,
                code=JSONRPC_INVALID_PARAMS,
                message="Invalid params: tool 'name' is required.",
            )

        try:
            result = execute_validated_tool(tool_name, tool_args)
            logger.info(f"Executed tool '{tool_name}' successfully.")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        except ValidationError as exc:
            sanitized_errors = [
                {
                    "loc": [str(item) for item in err.get("loc", ())],
                    "msg": err.get("msg", "Validation error"),
                    "type": err.get("type", "value_error"),
                }
                for err in exc.errors()
            ]
            logger.warning(f"Schema validation rejected '{tool_name}': {sanitized_errors}")
            return build_jsonrpc_error(
                request_id=request_id,
                code=JSONRPC_INVALID_PARAMS,
                message=f"Invalid params for tool '{tool_name}'.",
                data={"validation_errors": sanitized_errors},
            )
        except (KeyError, ValueError) as exc:
            logger.warning(f"Invalid tool invocation '{tool_name}': {exc}")
            return build_jsonrpc_error(
                request_id=request_id,
                code=JSONRPC_INVALID_PARAMS,
                message=str(exc),
            )
        except Exception as exc:
            logger.error(f"Unexpected error executing '{tool_name}': {exc}")
            return build_jsonrpc_error(
                request_id=request_id,
                code=JSONRPC_INTERNAL_ERROR,
                message="Internal MCP server error.",
            )

    if is_notification:
        return None

    logger.warning(f"Unsupported method requested: {method}")
    return build_jsonrpc_error(
        request_id=request_id,
        code=JSONRPC_METHOD_NOT_FOUND,
        message=f"Method not found: {method}",
    )


def _write_stdout_frame(response: dict[str, Any]) -> None:
    """Emit a single newline-delimited JSON-RPC frame to stdout and flush immediately."""
    serialized = json.dumps(response, separators=(",", ":"))
    sys.stdout.write(serialized + "\n")
    sys.stdout.flush()


async def run_stdio_server() -> None:
    """Run the line-delimited JSON-RPC loop over stdin/stdout with signal hygiene."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(getattr(signal, "SIGHUP"))

    for sig in signals:
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    logger.info("Customer Service MCP Server started on stdio transport.")

    while not stop_event.is_set():
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            logger.info("EOF detected on stdin; shutting down MCP server.")
            break

        stripped = line.strip()
        if not stripped:
            continue

        response = handle_jsonrpc_message(stripped)
        if response is not None:
            _write_stdout_frame(response)

    logger.info("Customer Service MCP Server exited cleanly.")


def main() -> None:
    """CLI entrypoint."""
    try:
        asyncio.run(run_stdio_server())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()