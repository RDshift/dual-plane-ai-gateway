# Environment Bootstrap & Engineering Runbook

**Project:** `dual-plane-ai-gateway`

Repeatable environment setup, operational commands, and verification gates for the Dual-Plane AI Gateway.

---

## 1. Prerequisites

* **Python Runtime:** Python >= 3.11 (3.11 and 3.12 supported; CPython recommended).
* **Git:** Revision control configured with local keychain integration.
* **Operating Systems:** macOS (Apple Silicon / Intel), Linux (systemd / POSIX), Windows.
* **Shell:** `scripts/start-venv.sh` targets Zsh/POSIX shells. Headless and Windows environments use the Python entry point (Section 3).

---

## 2. Fast-Path Bootstrap (Interactive)

From the repository root:

```zsh
# 1. Initialize local configuration
cp .env.example .env

# 2. Create/activate the virtual environment (must be sourced, not executed)
source ./scripts/start-venv.sh

# 3. Execute the full verification suite
python3 tests/verify_gateway_suite.py -W error
```

`start-venv.sh` behavior:

* Creates `.venv` if absent and activates it in the current shell.
* Installs `requirements.txt` and `requirements-dev.txt`.
* Idempotent: re-sourcing skips installs unless the requirements files changed.

Override the minimum Python version (default `3.11`):

```zsh
DUAL_MIN_PY=3.12 source ./scripts/start-venv.sh
```

---

## 3. Headless / CI Bootstrap

No shell sourcing required:

```bash
python3 scripts/bootstrap_and_verify.py
```

This ensures `.venv` exists, installs dev requirements and the project in editable mode, then runs `tests/verify_gateway_suite.py`. Exit code `0` indicates a healthy environment.

> If the package is nested under `gateway/`, use `python3 gateway/scripts/bootstrap_and_verify.py`.

---

## 4. Storage Hierarchy & Tier 1 Volatile Detection

In accordance with Tiered Storage governance:

* **Linux Hosts:** Automatically detects `/dev/shm` (RAM-backed tmpfs) to place the SQLite WAL rate-limiting database (`dual_plane_gateway_ratelimit.db`), eliminating flash media endurance wear during high-frequency token transactions.
* **macOS / Windows Hosts:** Falls back to `tempfile.gettempdir()` if `/dev/shm` is unavailable.
* **Custom Paths:** Override at runtime with `SQLITE_DB_PATH=/path/to/custom.db` in `.env`.

---

## 5. Verification Gates & Test Discovery

All tests in `tests/` enforce zero-warning discipline (`-W error`) and write structured artifacts to `artifacts/test_reports/`:

* `latest_junit.xml`: Machine-parsable JUnit XML report.
* `latest_summary.json`: Execution timestamp, platform metadata, test counts, and exit status.

Individual test modules (run inside the activated `.venv`):

```bash
python3 tests/test_m0_core.py                 # Core primitives, config, logging, lifecycle
python3 tests/test_m1_mcp_server.py           # Task 1: Stdio MCP server & Pydantic validation
python3 tests/test_m2_mcp_gateway.py          # Task 2: MCP proxy & -32001 tool interception
python3 tests/test_m3_streaming_guardrail.py  # Task 3: Streaming PII redaction & TTFT
python3 tests/test_m4_model_router.py         # Task 4: SQLite WAL rate limiter & failover
```
