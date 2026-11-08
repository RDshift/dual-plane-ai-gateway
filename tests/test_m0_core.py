"""Milestone 0 verification: configuration substrate, error envelopes, stderr
logging isolation, health gates and zero-trust diagnostics.

Interface: pytest module. Uses conftest fixtures isolated_sqlite_path and
gateway_client.
Dependencies: pytest, fastapi.testclient, gateway.core.*.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi.testclient import TestClient

from gateway.core.config import DEFAULT_SQLITE_RELATIVE_PATH, GatewaySettings, _resolve_default_sqlite_path, settings
from gateway.core.diagnostics import ALL_CHECKS, VOLATILE_PATH_PREFIX
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


def test_m0_default_sqlite_path_is_durable_and_project_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    monkeypatch.delenv("STREAM_GUARDRAIL_MAX_HOLDBACK_CHARS", raising=False)
    default_path = Path(_resolve_default_sqlite_path())
    assert default_path == (PROJECT_ROOT / DEFAULT_SQLITE_RELATIVE_PATH).resolve()
    assert default_path.name == "gateway_ratelimit.db"
    assert default_path.parent.name == "data"
    assert not str(default_path).startswith(VOLATILE_PATH_PREFIX)
    fresh = GatewaySettings(_env_file=None)
    assert Path(fresh.sqlite_db_path) == default_path
    assert fresh.stream_guardrail_max_holdback_chars == 256


def test_m0_verify_substrate_creates_missing_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    nested = tmp_path / "nested" / "deeper" / "ratelimit.db"
    assert not nested.parent.exists()
    cfg = GatewaySettings(_env_file=None, SQLITE_DB_PATH=str(nested))
    cfg.verify_substrate()
    assert nested.parent.is_dir()
    assert os.access(nested.parent, os.W_OK)
    blank = GatewaySettings(_env_file=None, SQLITE_DB_PATH="   ")
    blank.verify_substrate()
    assert Path(blank.sqlite_db_path) == Path(_resolve_default_sqlite_path())


def test_m0_verify_substrate_rejects_non_positive_bounds(tmp_path: Path) -> None:
    cfg = GatewaySettings(
        _env_file=None,
        SQLITE_DB_PATH=str(tmp_path / "x.db"),
        STREAM_GUARDRAIL_MAX_HOLDBACK_CHARS=0,
    )
    with pytest.raises(ValueError):
        cfg.verify_substrate()


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


def test_m0_diagnostics_requires_bearer_token(gateway_client: TestClient) -> None:
    response = gateway_client.get("/health/diagnostics")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == "authentication_required"


def test_m0_diagnostics_rejects_viewer_role(gateway_client: TestClient) -> None:
    response = gateway_client.get(
        "/health/diagnostics", headers={"Authorization": "Bearer tok_viewer_live_3c4d5e"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_m0_diagnostics_admin_reports_all_sections_ok(
    gateway_client: TestClient, isolated_sqlite_path: Path
) -> None:
    response = gateway_client.get(
        "/health/diagnostics", headers={"Authorization": "Bearer tok_admin_live_9x8a7b"}
    )
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "ok"
    assert set(ALL_CHECKS) <= set(report)
    for section in ALL_CHECKS:
        assert report[section]["status"] == "ok", section
    assert report["substrate"]["durable_storage"] is True
    assert report["sqlite_wal"]["journal_mode"] == "wal"
    assert report["mcp_stdio_bridge"]["public_tools_count"] == 2
    assert report["mcp_stdio_bridge"]["latency_ms"] >= 0.0
    assert report["model_plane_config"]["holdback_chars"] == 256
    serialized = json.dumps(report)
    for key in ("HTTPS_PROXY", "http://", "https://"):
        assert key not in serialized
    assert isolated_sqlite_path.exists()


def test_m0_gateway_doctor_json_exit_zero(tmp_path: Path) -> None:
    env = {**os.environ, "SQLITE_DB_PATH": str(tmp_path / "doctor" / "ratelimit.db")}
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gateway_doctor.py"), "--json"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout)
    assert report["status"] == "ok"
    assert report["sqlite_wal"]["journal_mode"] == "wal"
    assert (tmp_path / "doctor" / "ratelimit.db").exists()


if __name__ == "__main__":
    from tests.conftest import run_module_tests

    raise SystemExit(run_module_tests(__file__))
