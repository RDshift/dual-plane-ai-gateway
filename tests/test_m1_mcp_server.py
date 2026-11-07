"""Milestone 1 verification for servers/customer_service_mcp.py.

Purpose: validate Pydantic schemas, JSON-RPC dispatch, and end-to-end stdio
framing of the customer service MCP server running as a subprocess.
Interface: unittest-discoverable; no network or filesystem side effects.
Dependencies: pydantic>=2, gateway.core.errors, servers/customer_service_mcp.py.
"""

import importlib.util
import json
import logging
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PROJECT_ROOT / "servers" / "customer_service_mcp.py"
SERVER_MODULE_NAME = "customer_service_mcp_under_test"

SESSION_TIMEOUT_SECONDS = 30
JSONRPC_VERSION = "2.0"
WIRE_ENCODING = "utf-8"
WIRE_NEWLINE = "\n"

CODE_PARSE_ERROR = -32700
CODE_INVALID_REQUEST = -32600
CODE_METHOD_NOT_FOUND = -32601
CODE_INVALID_PARAMS = -32602

KNOWN_CUSTOMER_ID = "CUST-10001"
UNKNOWN_CUSTOMER_ID = "CUST-99999"
VALID_REASON = "Duplicate charge on invoice"
VALID_AMOUNT = 25.5
EXPECTED_TOOL_NAMES = {"get_customer_record", "trigger_refund"}
CUSTOMER_ID_PATTERN = "^CUST-[0-9]{5}$"
REASON_MIN_LENGTH = 10
INVALID_CUSTOMER_IDS = ("CUST-123", "ABC-12345")
INVALID_AMOUNTS = (0, -15.5)
SHORT_REASON = "too short"
PADDED_SHORT_REASON = "   short   "
PADDED_VALID_REASON = "   Duplicate charge on invoice   "
SECRET_MARKER = "SECRET-MARKER-VALUE"
STARTUP_LOG_EVENT = "server_started"
INVALID_PARAMS_LOG_EVENT = "invalid_params"

ID_INITIALIZE = 1
ID_PING = 2
ID_TOOLS_LIST = 3
ID_GET_VALID = 4
ID_REFUND_VALID = 5
ID_GET_NOT_FOUND = 6
ID_UNKNOWN_METHOD = 7
ID_UNKNOWN_TOOL = 8
ID_GET_EXTRA = 9
ID_REFUND_EXTRA = 10
ID_GET_BAD_BASE = 100
ID_REFUND_BAD_AMOUNT_BASE = 200
ID_REFUND_BAD_ID = 300
ID_REFUND_SHORT_REASON = 301
ID_REFUND_MISSING = 302
ID_REFUND_STRING_AMOUNT = 303
ID_ARGUMENTS_NOT_OBJECT = 304


def load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(SERVER_MODULE_NAME, SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[SERVER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


SERVER = load_server_module()


def rpc(request_id: Any, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def call(request_id: Any, tool: str, arguments: Any) -> dict[str, Any]:
    return rpc(request_id, "tools/call", {"name": tool, "arguments": arguments})


def refund_args(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "customer_id": KNOWN_CUSTOMER_ID,
        "amount": VALID_AMOUNT,
        "reason": VALID_REASON,
    }
    base.update(overrides)
    return base


class SessionResult:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self.stdout_text = stdout.decode(WIRE_ENCODING)
        self.stderr_text = stderr.decode(WIRE_ENCODING)
        self.returncode = returncode
        self.raw_lines = [line for line in self.stdout_text.split(WIRE_NEWLINE) if line]
        self.frames = [json.loads(line) for line in self.raw_lines]
        self.by_id = {frame.get("id"): frame for frame in self.frames}


def run_session(lines: list[Any]) -> SessionResult:
    payload = "".join(
        (item if isinstance(item, str) else json.dumps(item)) + WIRE_NEWLINE for item in lines
    ).encode(WIRE_ENCODING)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(PROJECT_ROOT), env.get("PYTHONPATH", "")])
    )
    env["PYTHONIOENCODING"] = WIRE_ENCODING
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(
        [sys.executable, str(SERVER_PATH)],
        input=payload,
        capture_output=True,
        timeout=SESSION_TIMEOUT_SECONDS,
        cwd=str(PROJECT_ROOT),
        env=env,
        check=False,
    )
    return SessionResult(completed.stdout, completed.stderr, completed.returncode)


class SchemaValidationTests(unittest.TestCase):
    def test_customer_id_accepts_valid_pattern(self) -> None:
        parsed = SERVER.GetCustomerRecordArgs.model_validate({"customer_id": KNOWN_CUSTOMER_ID})
        self.assertEqual(parsed.customer_id, KNOWN_CUSTOMER_ID)

    def test_customer_id_rejects_invalid_values(self) -> None:
        invalid = INVALID_CUSTOMER_IDS + ("CUST-123456", "cust-12345", "CUST-1234a", "CUST-12345\n", "")
        for value in invalid:
            with self.subTest(customer_id=value):
                with self.assertRaises(SERVER.ValidationError):
                    SERVER.GetCustomerRecordArgs.model_validate({"customer_id": value})

    def test_get_customer_record_forbids_extra_fields(self) -> None:
        with self.assertRaises(SERVER.ValidationError):
            SERVER.GetCustomerRecordArgs.model_validate(
                {"customer_id": KNOWN_CUSTOMER_ID, "extra": 1}
            )

    def test_refund_accepts_valid_payload_and_strips_reason(self) -> None:
        parsed = SERVER.TriggerRefundArgs.model_validate(refund_args(reason=PADDED_VALID_REASON))
        self.assertEqual(parsed.reason, VALID_REASON)
        self.assertEqual(parsed.amount, VALID_AMOUNT)

    def test_refund_accepts_integer_amount_as_float(self) -> None:
        parsed = SERVER.TriggerRefundArgs.model_validate(refund_args(amount=5))
        self.assertIsInstance(parsed.amount, float)

    def test_refund_rejects_non_positive_and_non_finite_amounts(self) -> None:
        for value in (*INVALID_AMOUNTS, float("inf"), float("nan")):
            with self.subTest(amount=value):
                with self.assertRaises(SERVER.ValidationError):
                    SERVER.TriggerRefundArgs.model_validate(refund_args(amount=value))

    def test_refund_rejects_non_numeric_amount_types(self) -> None:
        for value in ("10.0", True, None):
            with self.subTest(amount=value):
                with self.assertRaises(SERVER.ValidationError):
                    SERVER.TriggerRefundArgs.model_validate(refund_args(amount=value))

    def test_refund_rejects_short_reason_after_strip(self) -> None:
        for value in (SHORT_REASON, PADDED_SHORT_REASON, ""):
            with self.subTest(reason=value):
                with self.assertRaises(SERVER.ValidationError):
                    SERVER.TriggerRefundArgs.model_validate(refund_args(reason=value))

    def test_refund_accepts_reason_at_minimum_length(self) -> None:
        boundary = "x" * REASON_MIN_LENGTH
        parsed = SERVER.TriggerRefundArgs.model_validate(refund_args(reason=boundary))
        self.assertEqual(parsed.reason, boundary)

    def test_refund_forbids_extra_and_missing_fields(self) -> None:
        with self.assertRaises(SERVER.ValidationError):
            SERVER.TriggerRefundArgs.model_validate(refund_args(unexpected="x"))
        with self.assertRaises(SERVER.ValidationError):
            SERVER.TriggerRefundArgs.model_validate({"customer_id": KNOWN_CUSTOMER_ID})

    def test_sanitized_errors_exclude_input_values(self) -> None:
        with self.assertRaises(SERVER.ValidationError) as ctx:
            SERVER.GetCustomerRecordArgs.model_validate({"customer_id": SECRET_MARKER})
        sanitized = SERVER.sanitize_validation_errors(ctx.exception)
        self.assertTrue(sanitized)
        self.assertNotIn(SECRET_MARKER, json.dumps(sanitized))
        for item in sanitized:
            self.assertEqual(set(item), {"loc", "type", "msg"})


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        logger = logging.getLogger("quilr.test.m1")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.server = SERVER.McpServer(SERVER.build_tool_registry(), logger)

    def test_registry_exposes_exactly_two_tools(self) -> None:
        self.assertEqual(set(SERVER.build_tool_registry()), EXPECTED_TOOL_NAMES)

    def test_get_customer_record_handler(self) -> None:
        found = SERVER.get_customer_record(
            SERVER.GetCustomerRecordArgs(customer_id=KNOWN_CUSTOMER_ID)
        )
        self.assertFalse(found.is_error)
        self.assertEqual(found.payload["customer_id"], KNOWN_CUSTOMER_ID)
        missing = SERVER.get_customer_record(
            SERVER.GetCustomerRecordArgs(customer_id=UNKNOWN_CUSTOMER_ID)
        )
        self.assertTrue(missing.is_error)
        self.assertEqual(missing.payload["error"], "customer_not_found")

    def test_trigger_refund_handler_is_deterministic(self) -> None:
        args = SERVER.TriggerRefundArgs.model_validate(refund_args())
        first = SERVER.trigger_refund(args)
        second = SERVER.trigger_refund(args)
        self.assertFalse(first.is_error)
        self.assertEqual(first.payload, second.payload)
        self.assertTrue(first.payload["refund_id"].startswith(SERVER.REFUND_ID_PREFIX))
        self.assertEqual(first.payload["status"], SERVER.REFUND_STATUS_ACCEPTED)

    def test_trigger_refund_unknown_customer_is_tool_error(self) -> None:
        args = SERVER.TriggerRefundArgs.model_validate(
            refund_args(customer_id=UNKNOWN_CUSTOMER_ID)
        )
        self.assertTrue(SERVER.trigger_refund(args).is_error)

    def test_initialize_negotiates_protocol_version(self) -> None:
        supported = SERVER.SUPPORTED_PROTOCOL_VERSIONS[-1]
        response = self.server.handle_message(
            rpc(ID_INITIALIZE, "initialize", {"protocolVersion": supported})
        )
        self.assertEqual(response["result"]["protocolVersion"], supported)
        fallback = self.server.handle_message(
            rpc(ID_INITIALIZE, "initialize", {"protocolVersion": "1999-01-01"})
        )
        self.assertEqual(
            fallback["result"]["protocolVersion"], SERVER.LATEST_PROTOCOL_VERSION
        )
        self.assertIn("tools", fallback["result"]["capabilities"])

    def test_notification_yields_no_response(self) -> None:
        message = {"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized"}
        self.assertIsNone(self.server.handle_message(message))

    def test_invalid_envelopes_return_invalid_request(self) -> None:
        bad_messages = (
            [],
            "text",
            {"jsonrpc": "1.0", "id": 1, "method": "ping"},
            {"jsonrpc": JSONRPC_VERSION, "id": 1},
            {"jsonrpc": JSONRPC_VERSION, "id": [1], "method": "ping"},
        )
        for message in bad_messages:
            with self.subTest(message=message):
                response = self.server.handle_message(message)
                self.assertEqual(response["error"]["code"], CODE_INVALID_REQUEST)

    def test_parse_error_for_malformed_and_non_finite_json(self) -> None:
        for raw in ("{not json", b"\xff\xfe", '{"jsonrpc":"2.0","id":1,"method":NaN}'):
            with self.subTest(raw=raw):
                response = self.server.handle_line(raw)
                self.assertEqual(response["error"]["code"], CODE_PARSE_ERROR)
                self.assertIsNone(response["id"])

    def test_method_not_found(self) -> None:
        response = self.server.handle_message(rpc(ID_UNKNOWN_METHOD, "resources/list"))
        self.assertEqual(response["error"]["code"], CODE_METHOD_NOT_FOUND)

    def test_tools_call_invalid_params_maps_to_32602(self) -> None:
        cases = (
            call(ID_GET_BAD_BASE, "get_customer_record", {"customer_id": "CUST-123"}),
            call(ID_UNKNOWN_TOOL, "no_such_tool", {}),
            call(ID_ARGUMENTS_NOT_OBJECT, "get_customer_record", ["x"]),
            rpc(ID_ARGUMENTS_NOT_OBJECT, "tools/call", {"name": 7}),
            rpc(ID_ARGUMENTS_NOT_OBJECT, "tools/call", ["x"]),
        )
        for message in cases:
            with self.subTest(message=message):
                response = self.server.handle_message(message)
                self.assertEqual(response["error"]["code"], CODE_INVALID_PARAMS)
                self.assertNotIn("result", response)


class StdioEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.requests = [
            rpc(ID_INITIALIZE, "initialize", {"protocolVersion": SERVER.LATEST_PROTOCOL_VERSION}),
            {"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized"},
            rpc(ID_PING, "ping"),
            rpc(ID_TOOLS_LIST, "tools/list"),
            call(ID_GET_VALID, "get_customer_record", {"customer_id": KNOWN_CUSTOMER_ID}),
            call(ID_REFUND_VALID, "trigger_refund", refund_args()),
            call(ID_GET_NOT_FOUND, "get_customer_record", {"customer_id": UNKNOWN_CUSTOMER_ID}),
            rpc(ID_UNKNOWN_METHOD, "resources/list"),
            call(ID_UNKNOWN_TOOL, "no_such_tool", {}),
            call(ID_GET_EXTRA, "get_customer_record", {"customer_id": KNOWN_CUSTOMER_ID, "x": 1}),
            call(ID_REFUND_EXTRA, "trigger_refund", refund_args(unexpected="x")),
            call(ID_REFUND_BAD_ID, "trigger_refund", refund_args(customer_id=INVALID_CUSTOMER_IDS[0])),
            call(ID_REFUND_SHORT_REASON, "trigger_refund", refund_args(reason=SHORT_REASON)),
            call(ID_REFUND_MISSING, "trigger_refund", {"customer_id": KNOWN_CUSTOMER_ID}),
            call(ID_REFUND_STRING_AMOUNT, "trigger_refund", refund_args(amount="10")),
        ]
        for offset, value in enumerate(INVALID_CUSTOMER_IDS):
            cls.requests.append(
                call(ID_GET_BAD_BASE + offset, "get_customer_record", {"customer_id": value})
            )
        for offset, value in enumerate(INVALID_AMOUNTS):
            cls.requests.append(
                call(ID_REFUND_BAD_AMOUNT_BASE + offset, "trigger_refund", refund_args(amount=value))
            )
        cls.expected_response_count = sum(1 for item in cls.requests if "id" in item)
        cls.session = run_session(cls.requests)

    def assert_invalid_params(self, request_id: int) -> dict[str, Any]:
        frame = self.session.by_id[request_id]
        self.assertNotIn("result", frame)
        self.assertEqual(frame["error"]["code"], CODE_INVALID_PARAMS)
        self.assertEqual(frame["error"]["message"], "Invalid params")
        return frame

    def test_process_exits_cleanly_on_eof(self) -> None:
        self.assertEqual(self.session.returncode, 0)

    def test_stdout_contains_only_jsonrpc_frames(self) -> None:
        self.assertEqual(len(self.session.frames), self.expected_response_count)
        for line, frame in zip(self.session.raw_lines, self.session.frames):
            with self.subTest(line=line[:80]):
                self.assertEqual(frame["jsonrpc"], JSONRPC_VERSION)
                self.assertIn("id", frame)
                self.assertEqual("result" in frame, "error" not in frame)
        self.assertNotIn("\r", self.session.stdout_text)
        self.assertNotIn(STARTUP_LOG_EVENT, self.session.stdout_text)

    def test_startup_diagnostics_go_to_stderr(self) -> None:
        self.assertIn(STARTUP_LOG_EVENT, self.session.stderr_text)

    def test_initialize_and_ping(self) -> None:
        init = self.session.by_id[ID_INITIALIZE]["result"]
        self.assertEqual(init["protocolVersion"], SERVER.LATEST_PROTOCOL_VERSION)
        self.assertEqual(init["serverInfo"]["name"], SERVER.SERVER_NAME)
        self.assertEqual(self.session.by_id[ID_PING]["result"], {})
        self.assertNotIn(None, self.session.by_id)

    def test_tools_list_returns_both_tools_with_schemas(self) -> None:
        tools = {t["name"]: t for t in self.session.by_id[ID_TOOLS_LIST]["result"]["tools"]}
        self.assertEqual(set(tools), EXPECTED_TOOL_NAMES)
        get_schema = tools["get_customer_record"]["inputSchema"]
        self.assertEqual(get_schema["properties"]["customer_id"]["pattern"], CUSTOMER_ID_PATTERN)
        self.assertEqual(get_schema["required"], ["customer_id"])
        self.assertIs(get_schema["additionalProperties"], False)
        refund_schema = tools["trigger_refund"]["inputSchema"]
        self.assertEqual(
            set(refund_schema["required"]), {"customer_id", "amount", "reason"}
        )
        self.assertIs(refund_schema["additionalProperties"], False)
        self.assertEqual(refund_schema["properties"]["amount"]["exclusiveMinimum"], 0)
        self.assertEqual(refund_schema["properties"]["reason"]["minLength"], REASON_MIN_LENGTH)
        self.assertEqual(
            refund_schema["properties"]["customer_id"]["pattern"], CUSTOMER_ID_PATTERN
        )

    def test_valid_get_customer_record_result(self) -> None:
        result = self.session.by_id[ID_GET_VALID]["result"]
        self.assertIs(result["isError"], False)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(result["structuredContent"]["customer_id"], KNOWN_CUSTOMER_ID)

    def test_valid_trigger_refund_result(self) -> None:
        result = self.session.by_id[ID_REFUND_VALID]["result"]
        self.assertIs(result["isError"], False)
        payload = result["structuredContent"]
        self.assertEqual(payload["status"], SERVER.REFUND_STATUS_ACCEPTED)
        self.assertEqual(payload["amount"], VALID_AMOUNT)
        self.assertEqual(json.loads(result["content"][0]["text"]), payload)

    def test_unknown_customer_is_tool_level_error_not_protocol_error(self) -> None:
        frame = self.session.by_id[ID_GET_NOT_FOUND]
        self.assertNotIn("error", frame)
        self.assertIs(frame["result"]["isError"], True)

    def test_invalid_customer_ids_return_32602(self) -> None:
        for offset, value in enumerate(INVALID_CUSTOMER_IDS):
            with self.subTest(customer_id=value):
                frame = self.assert_invalid_params(ID_GET_BAD_BASE + offset)
                errors = frame["error"]["data"]["errors"]
                self.assertEqual(errors[0]["loc"], ["customer_id"])
                self.assertNotIn(value, json.dumps(frame["error"]["data"]))
        frame = self.assert_invalid_params(ID_REFUND_BAD_ID)
        self.assertEqual(frame["error"]["data"]["tool"], "trigger_refund")

    def test_non_positive_amounts_return_32602(self) -> None:
        for offset, value in enumerate(INVALID_AMOUNTS):
            with self.subTest(amount=value):
                frame = self.assert_invalid_params(ID_REFUND_BAD_AMOUNT_BASE + offset)
                self.assertEqual(frame["error"]["data"]["errors"][0]["loc"], ["amount"])

    def test_short_reason_returns_32602(self) -> None:
        frame = self.assert_invalid_params(ID_REFUND_SHORT_REASON)
        self.assertEqual(frame["error"]["data"]["errors"][0]["loc"], ["reason"])

    def test_extra_fields_return_32602(self) -> None:
        for request_id, field in ((ID_GET_EXTRA, "x"), (ID_REFUND_EXTRA, "unexpected")):
            with self.subTest(request_id=request_id):
                frame = self.assert_invalid_params(request_id)
                errors = frame["error"]["data"]["errors"]
                self.assertEqual(errors[0]["type"], "extra_forbidden")
                self.assertEqual(errors[0]["loc"], [field])

    def test_missing_and_mistyped_fields_return_32602(self) -> None:
        self.assert_invalid_params(ID_REFUND_MISSING)
        self.assert_invalid_params(ID_REFUND_STRING_AMOUNT)

    def test_unknown_tool_returns_32602_and_unknown_method_32601(self) -> None:
        frame = self.assert_invalid_params(ID_UNKNOWN_TOOL)
        self.assertEqual(frame["error"]["data"]["reason"], "unknown_tool")
        self.assertEqual(
            self.session.by_id[ID_UNKNOWN_METHOD]["error"]["code"], CODE_METHOD_NOT_FOUND
        )

    def test_validation_failures_are_logged_to_stderr_only(self) -> None:
        occurrences = self.session.stderr_text.count(INVALID_PARAMS_LOG_EVENT)
        self.assertGreaterEqual(occurrences, len(INVALID_CUSTOMER_IDS) + len(INVALID_AMOUNTS))
        self.assertNotIn(INVALID_PARAMS_LOG_EVENT, self.session.stdout_text)


class StdioFramingRobustnessTests(unittest.TestCase):
    def test_malformed_line_yields_parse_error_and_session_continues(self) -> None:
        session = run_session(["{not json", "", rpc(ID_PING, "ping")])
        self.assertEqual(session.returncode, 0)
        self.assertEqual(len(session.frames), 2)
        self.assertEqual(session.frames[0]["error"]["code"], CODE_PARSE_ERROR)
        self.assertIsNone(session.frames[0]["id"])
        self.assertEqual(session.frames[1]["result"], {})
        self.assertIn("parse_error", session.stderr_text)
        self.assertNotIn("parse_error", session.stdout_text)

    def test_empty_session_exits_cleanly_with_no_stdout(self) -> None:
        session = run_session([])
        self.assertEqual(session.returncode, 0)
        self.assertEqual(session.stdout_text, "")
        self.assertIn(STARTUP_LOG_EVENT, session.stderr_text)


if __name__ == "__main__":
    unittest.main()
