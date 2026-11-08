"""Zero-trust deployment diagnostics shared by GET /health/diagnostics and
scripts/gateway_doctor.py.

Interface: run_zero_trust_diagnostics() -> dict. Executes five non-destructive
checks (substrate, zero_trust_env, sqlite_wal, mcp_stdio_bridge,
model_plane_config) and returns a sanitized report: booleans, counts, modes
and latencies only, never environment values, URLs or exception text.
Top-level "status" is "ok" when every sub-check is "ok", else "degraded".

Dependencies: gateway.core.config, gateway.mcp_plane.stdio_bridge,
gateway.model_plane.rate_limiter.
"""

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from gateway.core.config import settings
from gateway.mcp_plane.stdio_bridge import BridgeError, stdio_bridge
from gateway.model_plane.rate_limiter import SqliteTokenRateLimiter

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DEGRADED = "degraded"
VOLATILE_PATH_PREFIX = str(Path("/dev") / "shm")
EXPECTED_JOURNAL_MODE = "wal"
DIAGNOSTIC_RPC_ID_PREFIX = "diag-"

PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
CA_BUNDLE_ENV_KEYS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")

CHECK_SUBSTRATE = "substrate"
CHECK_ZERO_TRUST_ENV = "zero_trust_env"
CHECK_SQLITE_WAL = "sqlite_wal"
CHECK_MCP_BRIDGE = "mcp_stdio_bridge"
CHECK_MODEL_CONFIG = "model_plane_config"
ALL_CHECKS = (CHECK_SUBSTRATE, CHECK_ZERO_TRUST_ENV, CHECK_SQLITE_WAL, CHECK_MCP_BRIDGE, CHECK_MODEL_CONFIG)

logger = logging.getLogger(__name__)


def _env_present(keys: tuple[str, ...]) -> bool:
    return any(os.environ.get(key, "").strip() for key in keys)


def check_substrate() -> dict[str, Any]:
    """Validate configuration and the SQLite directory without touching values."""
    try:
        settings.verify_substrate()
        db_path = Path(settings.sqlite_db_path).expanduser().resolve()
        writable = os.access(db_path.parent, os.W_OK)
        status = STATUS_OK if writable else STATUS_ERROR
    except (RuntimeError, ValueError, OSError) as exc:
        logger.warning("event=diagnostics_substrate_failed error=%s", type(exc).__name__)
        return {"status": STATUS_ERROR, "sqlite_dir_writable": False, "durable_storage": False}
    durable = not str(db_path).startswith(VOLATILE_PATH_PREFIX)
    return {
        "status": status if durable else STATUS_ERROR,
        "sqlite_dir_writable": writable,
        "durable_storage": durable,
    }


def check_zero_trust_env() -> dict[str, Any]:
    """Report presence of egress-control variables, never their contents."""
    return {
        "status": STATUS_OK,
        "proxy_configured": _env_present(PROXY_ENV_KEYS),
        "custom_ca_bundle_configured": _env_present(CA_BUNDLE_ENV_KEYS),
        "no_proxy_configured": _env_present(NO_PROXY_ENV_KEYS),
    }


async def check_sqlite_wal() -> dict[str, Any]:
    """Open the configured rate limiter database and confirm WAL journaling."""
    limiter = SqliteTokenRateLimiter(db_path=settings.sqlite_db_path)
    try:
        await limiter.open()
        mode = limiter.journal_mode
    except Exception as exc:
        logger.warning("event=diagnostics_sqlite_failed error=%s", type(exc).__name__)
        return {"status": STATUS_ERROR, "journal_mode": ""}
    finally:
        await limiter.close()
    return {
        "status": STATUS_OK if mode == EXPECTED_JOURNAL_MODE else STATUS_ERROR,
        "journal_mode": mode,
    }


async def check_mcp_stdio_bridge() -> dict[str, Any]:
    """Live tools/list round-trip through the stdio bridge."""
    request = {
        "jsonrpc": "2.0",
        "id": f"{DIAGNOSTIC_RPC_ID_PREFIX}{uuid.uuid4().hex[:12]}",
        "method": "tools/list",
    }
    started = time.perf_counter()
    try:
        response = await stdio_bridge.exchange(request)
    except (BridgeError, OSError, ValueError) as exc:
        logger.warning("event=diagnostics_bridge_failed error=%s", type(exc).__name__)
        return {"status": STATUS_ERROR, "public_tools_count": 0, "latency_ms": 0.0}
    latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
    tools = response.get("result", {}).get("tools") if isinstance(response, dict) else None
    count = len(tools) if isinstance(tools, list) else 0
    return {
        "status": STATUS_OK if count > 0 else STATUS_ERROR,
        "public_tools_count": count,
        "latency_ms": latency_ms,
    }


def check_model_plane_config() -> dict[str, Any]:
    return {
        "status": STATUS_OK,
        "timeout_ms": settings.upstream_timeout_ms,
        "rate_limit_tokens_per_minute": settings.rate_limit_tokens_per_minute,
        "holdback_chars": settings.stream_guardrail_max_holdback_chars,
    }


async def run_zero_trust_diagnostics() -> dict[str, Any]:
    """Run every check and aggregate the top-level status."""
    report: dict[str, Any] = {
        CHECK_SUBSTRATE: check_substrate(),
        CHECK_ZERO_TRUST_ENV: check_zero_trust_env(),
        CHECK_SQLITE_WAL: await check_sqlite_wal(),
        CHECK_MCP_BRIDGE: await check_mcp_stdio_bridge(),
        CHECK_MODEL_CONFIG: check_model_plane_config(),
    }
    all_ok = all(report[name]["status"] == STATUS_OK for name in ALL_CHECKS)
    report["status"] = STATUS_OK if all_ok else STATUS_DEGRADED
    logger.info("event=diagnostics_complete status=%s", report["status"])
    return report
