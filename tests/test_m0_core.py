import io
import json
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from gateway.core.config import settings
from tests.conftest import gateway_client, isolated_sqlite_path
from gateway.core.errors import (
    JSONRPC_INVALID_PARAMS,
    MCP_UNAUTHORIZED_TOOL_CALL,
    build_gateway_error,
    build_jsonrpc_error,
)
from gateway.core.logging import configure_stderr_logging


def test_m0_substrate_and_config(isolated_sqlite_path: Path) -> None:
    settings.verify_substrate()
    assert Path(settings.sqlite_db_path) == isolated_sqlite_path
    assert settings.rate_limit_tokens_per_minute == 50000
    assert settings.upstream_timeout_ms == 3000
    assert "tok_admin_live_9x8a7b" in settings.token_registry


def test_m0_stderr_logging_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    test_logger = configure_stderr_logging(level="INFO", logger_name="test.isolation")
    test_logger.info("diagnostic_probe")

    assert fake_stdout.getvalue() == ""
    stderr_out = fake_stderr.getvalue().strip()
    parsed = json.loads(stderr_out)
    assert parsed["msg"] == "diagnostic_probe"
    assert parsed["level"] == "INFO"


def test_m0_error_envelopes() -> None:
    rpc_err = build_jsonrpc_error(
        request_id=42,
        code=MCP_UNAUTHORIZED_TOOL_CALL,
        message="Unauthorized Tool Call",
    )
    assert rpc_err["jsonrpc"] == "2.0"
    assert rpc_err["id"] == 42
    assert rpc_err["error"]["code"] == -32001

    param_err = build_jsonrpc_error(
        request_id="req-1",
        code=JSONRPC_INVALID_PARAMS,
        message="Invalid params",
    )
    assert param_err["error"]["code"] == -32602

    gw_err = build_gateway_error(
        code="upstream_timeout",
        message="Primary and secondary providers failed.",
        http_status=504,
    )
    assert gw_err["error"]["status"] == 504
    assert "stack" not in json.dumps(gw_err).lower()


def test_m0_fastapi_health_gate(gateway_client: TestClient) -> None:
    response = gateway_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["plane_mcp"] == "ready"
    assert data["plane_model"] == "ready"