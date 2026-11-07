"""Customer service MCP server over newline-delimited JSON-RPC 2.0 on stdio.

Interface: stdin receives one JSON-RPC frame per line, stdout emits exactly one
JSON-RPC frame per request and nothing else, all diagnostics go to stderr as
structured JSON. Public tools are get_customer_record and trigger_refund.
Administrative tools are callable but never advertised by tools/list, so the
gateway remains the sole authority for admin_ access control.

Dependencies: pydantic>=2, mcp (types only), gateway.core.config,
gateway.core.errors, gateway.core.logging.
"""

import asyncio
import hashlib
import json
import logging
import math
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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

SERVER_NAME = "customer-service-mcp"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2024-11-05", "2025-03-26", "2025-06-18")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]
JSONRPC_VERSION = "2.0"
WIRE_ENCODING = "utf-8"

CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
REASON_MIN_LENGTH = 10
REFUND_ID_PREFIX = "RF-"
REFUND_ID_DIGEST_CHARS = 12
REFUND_STATUS_ACCEPTED = "accepted"
ADMIN_TARGET_MIN_LENGTH = 2

MSG_PARSE_ERROR = "Parse error"
MSG_INVALID_REQUEST = "Invalid Request"
MSG_METHOD_NOT_FOUND = "Method not found"
MSG_INVALID_PARAMS = "Invalid params"
MSG_INTERNAL_ERROR = "Internal error"

EVENT_SERVER_STARTED = "server_started"
EVENT_SERVER_STOPPED = "server_stopped"
EVENT_PARSE_ERROR = "parse_error"
EVENT_INVALID_REQUEST = "invalid_request"
EVENT_INVALID_PARAMS = "invalid_params"
EVENT_METHOD_NOT_FOUND = "method_not_found"
EVENT_TOOL_EXECUTED = "tool_executed"
EVENT_INTERNAL_ERROR = "internal_error"

SANITIZED_ERROR_KEYS = ("loc", "type", "msg")


class GetCustomerRecordArgs(BaseModel):
    """Strict input contract for get_customer_record."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(..., pattern=CUSTOMER_ID_PATTERN)

    @field_validator("customer_id", mode="before")
    @classmethod
    def reject_surrounding_whitespace(cls, value: Any) -> Any:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("customer_id must not contain surrounding whitespace")
        return value


class TriggerRefundArgs(BaseModel):
    """Strict input contract for trigger_refund."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(..., pattern=CUSTOMER_ID_PATTERN)
    amount: float = Field(..., gt=0.0)
    reason: str = Field(..., min_length=REASON_MIN_LENGTH)

    @field_validator("customer_id", mode="before")
    @classmethod
    def reject_surrounding_whitespace(cls, value: Any) -> Any:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("customer_id must not contain surrounding whitespace")
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_strict_numeric(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("amount must be a finite number greater than 0.0")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("amount must be a finite number greater than 0.0")
        return numeric

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class AdminResetKeyArgs(BaseModel):
    """Strict input contract for the unadvertised admin_reset_key tool."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_service: str = Field(default="gateway_auth", min_length=ADMIN_TARGET_MIN_LENGTH)


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


@dataclass(frozen=True)
class ToolOutcome:
    """Result of a tool handler. is_error maps to CallToolResult.isError."""

    payload: dict[str, Any]
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """Tool registration: schema model, handler, and description."""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any], ToolOutcome]

    def definition(self) -> dict[str, Any]:
        tool = mcp_types.Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.args_model.model_json_schema(),
        )
        return tool.model_dump(mode="json", exclude_none=True, by_alias=True)


def get_customer_record(args: GetCustomerRecordArgs) -> ToolOutcome:
    """Return a customer record or a tool-level customer_not_found error."""
    record = MOCK_CUSTOMER_DB.get(args.customer_id)
    if record is None:
        return ToolOutcome(
            payload={"error": "customer_not_found", "customer_id": args.customer_id},
            is_error=True,
        )
    return ToolOutcome(payload=dict(record))


def trigger_refund(args: TriggerRefundArgs) -> ToolOutcome:
    """Issue a deterministic refund receipt keyed on the validated arguments."""
    if args.customer_id not in MOCK_CUSTOMER_DB:
        return ToolOutcome(
            payload={"error": "customer_not_found", "customer_id": args.customer_id},
            is_error=True,
        )
    fingerprint = json.dumps(
        [args.customer_id, args.amount, args.reason], separators=(",", ":")
    ).encode(WIRE_ENCODING)
    digest = hashlib.sha256(fingerprint).hexdigest()[:REFUND_ID_DIGEST_CHARS]
    return ToolOutcome(
        payload={
            "refund_id": f"{REFUND_ID_PREFIX}{digest}",
            "status": REFUND_STATUS_ACCEPTED,
            "customer_id": args.customer_id,
            "amount": round(args.amount, 2),
            "reason": args.reason,
        }
    )


def admin_reset_key(args: AdminResetKeyArgs) -> ToolOutcome:
    """Simulated administrative key rotation (never advertised via tools/list)."""
    return ToolOutcome(payload={"status": "rotated", "target_service": args.target_service})


def build_tool_registry() -> dict[str, ToolSpec]:
    """Public tools advertised by tools/list."""
    specs = (
        ToolSpec(
            name="get_customer_record",
            description="Retrieve a customer record by strict CUST-XXXXX identifier.",
            args_model=GetCustomerRecordArgs,
            handler=get_customer_record,
        ),
        ToolSpec(
            name="trigger_refund",
            description="Issue a positive refund with an audited reason string.",
            args_model=TriggerRefundArgs,
            handler=trigger_refund,
        ),
    )
    return {spec.name: spec for spec in specs}


def build_admin_tool_registry() -> dict[str, ToolSpec]:
    """Administrative tools: callable, but excluded from tools/list."""
    spec = ToolSpec(
        name="admin_reset_key",
        description="Rotate a service credential. Gateway admin role required.",
        args_model=AdminResetKeyArgs,
        handler=admin_reset_key,
    )
    return {spec.name: spec}


def sanitize_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Strip input values and context from Pydantic errors before they hit the wire."""
    return [
        {
            "loc": [str(item) for item in err.get("loc", ())],
            "type": str(err.get("type", "value_error")),
            "msg": str(err.get("msg", "Validation error")),
        }
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def _reject_non_finite(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant not allowed: {token}")


def _is_valid_id(candidate: Any) -> bool:
    if isinstance(candidate, bool):
        return False
    return candidate is None or isinstance(candidate, (str, int))


@dataclass
class McpServer:
    """Pure JSON-RPC dispatcher. No I/O so it is unit-testable in isolation."""

    registry: dict[str, ToolSpec]
    logger: logging.Logger
    admin_registry: dict[str, ToolSpec] = field(default_factory=dict)

    def handle_line(self, raw: str | bytes) -> dict[str, Any] | None:
        """Decode one wire line and dispatch it. Parse failures yield -32700."""
        try:
            text = raw.decode(WIRE_ENCODING) if isinstance(raw, bytes) else raw
            message = json.loads(text, parse_constant=_reject_non_finite)
        except (UnicodeDecodeError, ValueError) as exc:
            self.logger.warning(f"event={EVENT_PARSE_ERROR} detail={type(exc).__name__}")
            return build_jsonrpc_error(None, JSONRPC_PARSE_ERROR, MSG_PARSE_ERROR)
        return self.handle_message(message)

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Dispatch one decoded JSON-RPC message. Returns None for notifications."""
        if not isinstance(message, dict):
            self.logger.warning(f"event={EVENT_INVALID_REQUEST} reason=not_object")
            return build_jsonrpc_error(None, JSONRPC_INVALID_REQUEST, MSG_INVALID_REQUEST)

        request_id = message.get("id")
        if not _is_valid_id(request_id):
            self.logger.warning(f"event={EVENT_INVALID_REQUEST} reason=bad_id")
            return build_jsonrpc_error(None, JSONRPC_INVALID_REQUEST, MSG_INVALID_REQUEST)

        method = message.get("method")
        if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str) or not method:
            self.logger.warning(f"event={EVENT_INVALID_REQUEST} reason=bad_envelope")
            return build_jsonrpc_error(request_id, JSONRPC_INVALID_REQUEST, MSG_INVALID_REQUEST)

        is_notification = "id" not in message
        params = message.get("params")

        if method.startswith("notifications/"):
            self.logger.info(f"event=notification method={method}")
            return None

        if method == "initialize":
            result = self._initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [spec.definition() for spec in self.registry.values()]}
        elif method == "tools/call":
            return None if is_notification else self._tools_call(request_id, params)
        else:
            self.logger.warning(f"event={EVENT_METHOD_NOT_FOUND} method={method}")
            if is_notification:
                return None
            return build_jsonrpc_error(request_id, JSONRPC_METHOD_NOT_FOUND, MSG_METHOD_NOT_FOUND)

        if is_notification:
            return None
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}

    def _initialize(self, params: Any) -> dict[str, Any]:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _tools_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._invalid_params(request_id, {"reason": "params_not_object"})
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return self._invalid_params(request_id, {"reason": "missing_tool_name"})
        spec = self.registry.get(name) or self.admin_registry.get(name)
        if spec is None:
            return self._invalid_params(request_id, {"reason": "unknown_tool", "tool": name})
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self._invalid_params(
                request_id, {"reason": "arguments_not_object", "tool": name}
            )
        try:
            validated = spec.args_model.model_validate(arguments)
        except ValidationError as exc:
            return self._invalid_params(
                request_id, {"tool": name, "errors": sanitize_validation_errors(exc)}
            )
        try:
            outcome = spec.handler(validated)
        except Exception as exc:
            self.logger.error(f"event={EVENT_INTERNAL_ERROR} tool={name} error={type(exc).__name__}")
            return build_jsonrpc_error(request_id, JSONRPC_INTERNAL_ERROR, MSG_INTERNAL_ERROR)
        self.logger.info(f"event={EVENT_TOOL_EXECUTED} tool={name} is_error={outcome.is_error}")
        text = json.dumps(outcome.payload, separators=(",", ":"))
        content = mcp_types.TextContent(type="text", text=text)
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": {
                "content": [content.model_dump(mode="json", exclude_none=True, by_alias=True)],
                "structuredContent": outcome.payload,
                "isError": outcome.is_error,
            },
        }

    def _invalid_params(self, request_id: Any, data: dict[str, Any]) -> dict[str, Any]:
        self.logger.warning(
            f"event={EVENT_INVALID_PARAMS} tool={data.get('tool')} reason={data.get('reason', 'schema')}"
        )
        return build_jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, MSG_INVALID_PARAMS, data)


def _stdin_pump(
    loop: asyncio.AbstractEventLoop, lines: "asyncio.Queue[bytes]", stream: Any
) -> None:
    """Blocking stdin reader on a daemon thread; b"" signals EOF to the loop."""
    try:
        for line in iter(stream.readline, b""):
            loop.call_soon_threadsafe(lines.put_nowait, line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            loop.call_soon_threadsafe(lines.put_nowait, b"")
        except RuntimeError:
            pass


def _write_stdout_frame(response: dict[str, Any]) -> None:
    """Emit one newline-delimited JSON-RPC frame to stdout and flush immediately."""
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def run_stdio_server(server: McpServer, logger: logging.Logger) -> None:
    """Line-delimited JSON-RPC loop on stdin/stdout with POSIX signal hygiene."""
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

    logger.info(f"event={EVENT_SERVER_STARTED} name={SERVER_NAME} protocol={LATEST_PROTOCOL_VERSION}")

    lines: asyncio.Queue[bytes] = asyncio.Queue()
    pump = threading.Thread(
        target=_stdin_pump, args=(loop, lines, sys.stdin.buffer), name="stdin-pump", daemon=True
    )
    pump.start()
    stopper = asyncio.ensure_future(stop_event.wait())

    while not stop_event.is_set():
        reader = asyncio.ensure_future(lines.get())
        done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if reader not in done:
            reader.cancel()
            break
        line = reader.result()
        if not line:
            logger.info("event=stdin_eof")
            break
        stripped = line.strip()
        if not stripped:
            continue
        response = server.handle_line(stripped)
        if response is not None:
            _write_stdout_frame(response)

    stopper.cancel()
    logger.info(f"event={EVENT_SERVER_STOPPED} name={SERVER_NAME}")


def main() -> None:
    """CLI entrypoint. All diagnostics are bound to stderr."""
    logger = configure_stderr_logging(level=settings.log_level, logger_name="quilr.mcp_server")
    server = McpServer(build_tool_registry(), logger, build_admin_tool_registry())
    try:
        asyncio.run(run_stdio_server(server, logger))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
