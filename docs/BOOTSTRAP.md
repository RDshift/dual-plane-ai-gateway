# Environment Bootstrap & Engineering Runbook

This runbook defines the repeatable environment setup, verification gates, and operational commands for the Dual-Plane AI Gateway.

---

## 1. Prerequisites

* **Python Runtime:** Python 3.11 or Python 3.12 (CPython recommended).
* **Operating Systems:** macOS (Apple Silicon / Intel), Linux (POSIX), and Windows.

---

## 2. Fast-Path Bootstrap & Verification

From the root of the cloned repository:

~~~bash
# 1. Initialize local configuration from template
cp .env.example .env

# 2. Activate virtual environment (creates .venv and syncs dependencies)
source ./scripts/start-venv.sh

# 3. Execute the full 113-test verification suite (warnings treated as errors)
python3 tests/verify_gateway_suite.py -W error

# 4. Execute the zero-trust deployment preflight doctor
python3 scripts/gateway_doctor.py
~~~

For headless CI pipelines or single-command bootstrap without shell sourcing:

~~~bash
python3 scripts/bootstrap_and_verify.py
~~~

---

## 3. Durable On-Disk SQLite Storage (`data/gateway_ratelimit.db`)

* **Default Location:** By default, `SQLITE_DB_PATH` resolves to `<project_root>/data/gateway_ratelimit.db`.
* **Startup Verification:** `settings.verify_substrate()` automatically creates the parent directory (`data/`) if absent, verifies write permissions (`os.W_OK`), and configures SQLite with `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, and `PRAGMA busy_timeout=5000`.
* **Custom Path Override:** Set `SQLITE_DB_PATH=/var/lib/gateway/ratelimit.db` in `.env` or container environment variables to point at a persistent volume mount in production.

---

## 4. Verification Gates & Test Execution

All tests in `tests/` run under `-W error` and emit structured reports to `artifacts/test_reports/`:

* `artifacts/test_reports/latest_junit.xml`: JUnit XML report for CI ingestion.
* `artifacts/test_reports/latest_summary.json`: JSON summary containing pass/fail counts, duration, Python runtime, and OS platform metadata.

Individual milestone modules can also be executed directly:

~~~bash
python3 tests/test_m0_core.py                 # Milestone 0: Substrate, logging, diagnostics (11 tests)
python3 tests/test_m1_mcp_server.py           # Milestone 1: Stdio MCP server & schemas (38 tests, 54 subtests)
python3 tests/test_m2_mcp_gateway.py          # Milestone 2: MCP gateway auth & -32001 RBAC (16 tests)
python3 tests/test_m3_streaming_guardrail.py  # Milestone 3: Streaming PII guardrail & 256-char cap (25 tests)
python3 tests/test_m4_model_router.py         # Milestone 4: SQLite WAL rate limiter & failover (23 tests)
~~~