# Dual-Plane AI Gateway

An enterprise-grade AI and Model Context Protocol (MCP) security gateway engineered around a strict **Dual-Plane Architecture** within a unified FastAPI process.

Rather than treating tool execution and model inference as disconnected scripts or forcing them through an artificial single-pipe conveyor belt, this gateway separates traffic by operational failure mode, latency profile, and security boundary:

1. **The MCP Context & Tool Plane (`POST /mcp`)**: Governs synchronous, state-mutating JSON-RPC 2.0 tool invocations. Enforces Bearer token authentication, role-based access control (intercepting unauthorized `admin_*` calls at the perimeter with JSON-RPC error `-32001`), and serialized forwarding over an isolated `stdio` subprocess bridge to a Pydantic-validated MCP server.
2. **The Model Inference Plane (`POST /v1/chat/completions`)**: Governs long-lived, latency-sensitive LLM generation streams. Enforces per-tenant sliding-window token rate limiting backed by on-disk SQLite in Write-Ahead Logging (WAL) mode, executes automated 3000ms timeout and HTTP 429/5xx primary-to-secondary model failover before committing HTTP 200 SSE headers, and applies zero-buffering rolling-tail PII redaction in real time.

Detailed architectural documentation and operational runbooks live in the [`docs/`](docs/) directory:
* **Architectural Decision Records (ADRs 001–004):** [`docs/DECISIONS.md`](docs/DECISIONS.md)
* **Environment Bootstrap & Engineering Runbook:** [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md)

---

## System Architecture

```
                         +-------------------------------------------------------+
   Bearer tok_admin_*    |                 Dual-Plane AI Gateway                 |
   Bearer tok_viewer_*   |              (FastAPI / Uvicorn Process)              |
          |              |                                                       |
          v              |  +-------------------------------------------------+  |
  POST /mcp ------------>|  | 1. MCP CONTEXT & TOOL PLANE (mcp_router.py)     |  |
  (JSON-RPC 2.0)         |  |  - Bearer token & role resolution               |  |     Newline-Delimited
                         |  |  - Perimeter admin_* gate (-32001 on viewer,    |  |     JSON-RPC (stdin/stdout)
                         |  |    zero downstream writes)                      |  |   +-------------------------+
                         |  |  - stdio_bridge.py (asyncio.Lock serialization, +--+-->| servers/                |
                         |  |    request-ID pairing, timeout auto-respawn)    |  |   | customer_service_mcp.py |
                         |  +-------------------------------------------------+  |   | - get_customer_record   |
                         |                                                       |   | - trigger_refund        |
   Bearer <tenant-key>   |  +-------------------------------------------------+  |   | - admin_reset_key       |
   or X-API-Key          |  | 2. MODEL INFERENCE PLANE (model_router.py)      |  |   +-------------------------+
          |              |  |  - Tenant auth & prompt/completion token tally  |  |     stderr: JSON telemetry
          v              |  |  - rate_limiter.py (On-disk SQLite WAL,         |  |
  POST /v1/chat/ ------->|  |    BEGIN IMMEDIATE sliding window, SHA-256 key) |  |   +-------------------------+
       completions       |  |  - failover_router.py (Primary -> Secondary     |--+-->| Primary LLM Provider    |
                         |  |    on 3000ms timeout / 429 / 5xx pre-stream)    |  |   +-------------------------+
                         |  |  - streaming_guardrail.py (128-char rolling     |--+-->| Secondary LLM Provider  |
  text/event-stream <----|  |    tail holdback, split-chunk [REDACTED] SSE)   |  |   +-------------------------+
  X-Model-Provider       |  +-------------------------------------------------+  |
  X-RateLimit-Remaining  |                                                       |
                         |  SHARED CORE (gateway/core/):                         |
                         |  - config.py (Pydantic Settings, Tier 1 /dev/shm)     |
                         |  - errors.py (JSON-RPC 2.0 & sanitized HTTP envelopes)|
                         |  - lifecycle.py (POSIX signal traps & child reaping)  |
                         |  - logging.py (Unbuffered structured JSON on stderr)  |
                         +-------------------------------------------------------+
```

---

## Core Capabilities & Verification Mapping

| Core Capability | Architectural Implementation | Source Modules | Verification Suite |
| :--- | :--- | :--- | :--- |
| **Task 1: Custom MCP Server & Strict Validation** | Standalone `stdio` MCP server exposing `get_customer_record` (`^CUST-[0-9]{5}$`) and `trigger_refund` (`amount > 0.0`, stripped `reason` >= 10 chars, `extra="forbid"`). Maps validation failures to protocol-level JSON-RPC `-32602` errors with sanitized `loc/type/msg` metadata. Reserves `stdout` strictly for JSON-RPC frames and routes structured JSON logs to `stderr`. | `servers/customer_service_mcp.py` | `tests/test_m1_mcp_server.py` |
| **Task 2: MCP Security Gateway Proxy** | Extracts `Bearer <token>` roles (`admin` vs. `viewer`). Transparently proxies `tools/list` and authorized `tools/call` frames over a lock-serialized `stdio` bridge. Intercepts unauthorized `admin_*` invocations at the gateway perimeter, returning `-32001: Unauthorized Tool Call` with provably zero downstream writes. | `gateway/mcp_plane/mcp_router.py`<br>`gateway/mcp_plane/stdio_bridge.py` | `tests/test_m2_mcp_gateway.py` |
| **Task 3: Streaming PII Guardrail** | Real-time SSE stream interceptor redacting emails, US SSNs, and 16-digit credit cards into `[REDACTED]`. Uses a maximal token-alphabet suffix holdback (capped at 128 chars) to catch patterns split across arbitrary chunk boundaries while flushing safe prefixes immediately to preserve Time To First Token (TTFT). | `gateway/model_plane/streaming_guardrail.py` | `tests/test_m3_streaming_guardrail.py` |
| **Task 4: SQLite WAL Rate Limiter & Failover Router** | Token-aware 60s sliding-window rate limiter (50,000 tokens/min per tenant) backed by on-disk SQLite (`PRAGMA journal_mode=WAL`, `BEGIN IMMEDIATE` atomic eviction/deduction, SHA-256 tenant key hashing, exact `Retry-After` calculation). Pre-stream failover from primary to secondary provider on 3000ms timeout, network error, HTTP 429, or HTTP 5xx with sanitized `502/503/504` error envelopes. | `gateway/model_plane/rate_limiter.py`<br>`gateway/model_plane/failover_router.py`<br>`gateway/model_plane/model_router.py` | `tests/test_m4_model_router.py` |
| **Substrate Safety & Process Hygiene** | Dynamic Tier 1 `/dev/shm` volatile storage resolution with cross-platform temp fallback (macOS, Linux, Windows), startup substrate validation, POSIX signal handlers (`SIGINT`, `SIGTERM`, `SIGHUP`), and deterministic child-process teardown. | `gateway/core/config.py`<br>`gateway/core/errors.py`<br>`gateway/core/lifecycle.py`<br>`gateway/core/logging.py`<br>`gateway/main.py` | `tests/test_m0_core.py` |

---

## 60-Second Quickstart & Verification

### 1. Environment Bootstrap & Full Verification Suite

For macOS / Linux (`zsh` / `bash`), source the idempotent virtual environment helper and run the unified test orchestrator:

```
git clone <repository-url> dual-plane-ai-gateway
cd dual-plane-ai-gateway
cp .env.example .env
source ./scripts/start-venv.sh
python3 tests/verify_gateway_suite.py -W error
```

For cross-platform CI or Windows/macOS/Linux one-command execution without sourcing shell scripts:

```
python3 scripts/bootstrap_and_verify.py
```

`tests/verify_gateway_suite.py` executes all 101 tests with warnings treated as errors, writes machine-readable JUnit XML and JSON reports to `artifacts/test_reports/`, and prints an executive summary banner at the bottom of `stdout`:

```
========================================================================
 DUAL-PLANE AI GATEWAY VERIFICATION SUMMARY  [PASSED]
========================================================================
 Status        : PASSED (pytest exit code 0)
 Total tests   : 101
 Passed        : 101
 Failed        : 0    Errors: 0    Skipped: 0
 Subtests      : 54 passed, 0 failed
 Duration      : 1.480s (suite)  1.815s (wall clock)
 Python        : 3.12.16 (CPython)
 Platform      : macOS-27.2-arm64-arm-64bit
 Timestamp UTC : 2026-11-07T21:42:51.633040+00:00
 JUnit report  : artifacts/test_reports/latest_junit.xml
 JSON summary  : artifacts/test_reports/latest_summary.json
========================================================================
```

### 2. Modular Test Execution

Each milestone test module includes a direct execution guard and can be run standalone with full fixture and warning-filter parity:

```
python3 tests/test_m0_core.py
python3 tests/test_m1_mcp_server.py
python3 tests/test_m2_mcp_gateway.py
python3 tests/test_m3_streaming_guardrail.py
python3 tests/test_m4_model_router.py
```

---

## Live Interactive `curl` Playbook

To exercise both planes locally without external API keys, start two instances of the bundled OpenAI-compatible mock upstream (`scripts/mock_model_upstream.py`)—one configured to return `HTTP 429` as the primary provider, and one healthy instance streaming split-chunk PII as the secondary backup provider—then start the gateway:

```
MOCK_UPSTREAM_STATUS=429 python3 -m uvicorn scripts.mock_model_upstream:app --port 9101 &
python3 -m uvicorn scripts.mock_model_upstream:app --port 9102 &
PRIMARY_MODEL_BASE_URL=http://127.0.0.1:9101/v1 \
SECONDARY_MODEL_BASE_URL=http://127.0.0.1:9102/v1 \
python3 -m uvicorn gateway.main:app --port 8000
```

### (a) Transparent `tools/list` Forwarding (`viewer` Role)

```
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer tok_viewer_live_3c4d5e" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Returns the public MCP tool registry (`get_customer_record` and `trigger_refund`) with strict JSON Schema constraints (`pattern`, `exclusiveMinimum`, `minLength`, and `additionalProperties: false`).

### (b) Perimeter Interception of Unauthorized `admin_*` Tool Calls (`-32001`)

```
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer tok_viewer_live_3c4d5e" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"target_service":"gateway_auth"}}}'
```

```
{"jsonrpc":"2.0","id":2,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

The gateway blocks the request immediately at the HTTP boundary without writing to the downstream `stdio` MCP subprocess.

### (c) Authorized Administrative Tool Execution (`admin` Role)

```
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer tok_admin_live_9x8a7b" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"target_service":"gateway_auth"}}}'
```

```
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\"status\":\"rotated\",\"target_service\":\"gateway_auth\"}"}],"structuredContent":{"status":"rotated","target_service":"gateway_auth"},"isError":false}}
```

### (d) Downstream Pydantic Validation Rejection Mapped to JSON-RPC `-32602`

```
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer tok_viewer_live_3c4d5e" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-123","amount":-5,"reason":"short"}}}'
```

```
{"jsonrpc":"2.0","id":4,"error":{"code":-32602,"message":"Invalid params","data":{"tool":"trigger_refund","errors":[
  {"loc":["customer_id"],"type":"string_pattern_mismatch","msg":"String should match pattern '^CUST-[0-9]{5}$'"},
  {"loc":["amount"],"type":"greater_than","msg":"Input should be greater than 0"},
  {"loc":["reason"],"type":"string_too_short","msg":"String should have at least 10 characters"}]}}}
```

### (e) Automatic Primary-to-Secondary Failover & Live Streaming PII Redaction

```
curl -s -i -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer tenant-demo-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"demo","messages":[{"role":"user","content":"Who is the customer on file?"}],"stream":true}'
```

```
HTTP/1.1 200 OK
x-model-provider: secondary
x-request-id: ce22d4cb48d34bbdb36c72a0c0b95a80
x-ratelimit-remaining: 49199
x-ratelimit-limit: 50000
content-type: text/event-stream; charset=utf-8

data: {"choices":[{"delta":{"content":"Sure. "}}]}

data: {"choices":[{"delta":{"content":"The customer on file is "}}]}

data: {"choices":[{"delta":{"content":"[REDACTED], SSN "}}]}

data: {"choices":[{"delta":{"content":"[REDACTED], card "}}]}

data: {"choices":[{"delta":{"content":"[REDACTED]. "}}]}

data: {"choices":[{"delta":{"content":"Anything else?"}}]}

data: [DONE]
```

### (f) Sliding-Window Quota Exhaustion & `Retry-After` Enforcement

```
curl -s -i http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer tenant-demo-key" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"x"}],"tokens":50000}'
```

```
HTTP/1.1 429 Too Many Requests
retry-after: 60
x-ratelimit-remaining: 49199
x-ratelimit-limit: 50000

{"error":{"code":"rate_limit_exceeded","message":"Rate limit exceeded for this API key.","status":429,"request_id":"..."}}
```

---

## Externalized Configuration

All operational parameters are managed via environment variables or `.env` (loaded via `pydantic-settings` in `gateway/core/config.py`):

| Environment Variable | Default Value | Description |
| :--- | :--- | :--- |
| `GATEWAY_ENV` | `development` | Runtime environment identifier |
| `LOG_LEVEL` | `INFO` | Structured JSON `stderr` logging verbosity |
| `SQLITE_DB_PATH` | Auto-detected (`/dev/shm/...` or OS temp dir) | On-disk SQLite WAL rate-limiter database path |
| `RATE_LIMIT_TOKENS_PER_MINUTE` | `50000` | Sliding-window token quota per tenant API key |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding-window duration in seconds |
| `UPSTREAM_TIMEOUT_MS` | `3000` | Per-provider failover timeout and MCP stdio bridge timeout (ms) |
| `MCP_SERVER_SCRIPT` | `servers/customer_service_mcp.py` | Path to the downstream stdio MCP server script |
| `PRIMARY_MODEL_BASE_URL` | `https://api.primary-llm.internal/v1` | Primary OpenAI-compatible model endpoint |
| `PRIMARY_MODEL_API_KEY` | `sk-primary-placeholder` | Primary upstream provider credential |
| `SECONDARY_MODEL_BASE_URL` | `https://api.secondary-llm.internal/v1` | Secondary backup OpenAI-compatible model endpoint |
| `SECONDARY_MODEL_API_KEY` | `sk-secondary-placeholder` | Secondary upstream provider credential |
| `DEFAULT_MODEL_NAME` | `dual-plane-enterprise-default` | Fallback model identifier when omitted by caller |

---

## Repository Structure

```
dual-plane-ai-gateway/
├── .env.example                        # Externalized configuration template
├── README.md                           # Root repository overview, architecture, and curl playbook
├── pyproject.toml                      # Package metadata and pytest warning/discovery config
├── requirements.txt                    # Production runtime dependencies
├── requirements-dev.txt                # Verification and test harness dependencies
├── artifacts/
│   └── test_reports/                   # Generated JUnit XML and JSON verification summaries
├── docs/
│   ├── BOOTSTRAP.md                    # Environment bootstrap and operational runbook
│   └── DECISIONS.md                    # Architectural Decision Records (ADR-001 .. ADR-004)
├── gateway/
│   ├── main.py                         # FastAPI application factory, lifespan, and /health
│   ├── core/
│   │   ├── config.py                   # Tier 1 /dev/shm detection and settings validation
│   │   ├── errors.py                   # JSON-RPC 2.0 and sanitized HTTP error builders
│   │   ├── lifecycle.py                # Signal trapping and deterministic teardown hooks
│   │   └── logging.py                  # Unbuffered stderr-only JSON formatter
│   ├── mcp_plane/
│   │   ├── mcp_router.py               # POST /mcp auth, role inspection, and -32001 gate
│   │   └── stdio_bridge.py             # Async lock-serialized child process stdio bridge
│   └── model_plane/
│       ├── failover_router.py          # 3000ms timeout / 429 / 5xx pre-stream failover
│       ├── model_router.py             # POST /v1/chat/completions orchestration
│       ├── rate_limiter.py             # SQLite WAL sliding-window rate limiter
│       └── streaming_guardrail.py      # Rolling-tail holdback PII stream redactor
├── scripts/
│   ├── bootstrap_and_verify.py         # Cross-platform venv bootstrap and test runner
│   ├── mock_model_upstream.py          # Local OpenAI-compatible SSE upstream simulator
│   └── start-venv.sh                   # Idempotent zsh/bash virtual environment activator
├── servers/
│   └── customer_service_mcp.py         # Standalone stdio MCP server with Pydantic schemas
└── tests/
    ├── conftest.py                     # Isolated SQLite and FastAPI TestClient fixtures
    ├── verify_gateway_suite.py         # Unified test orchestrator and summary banner plugin
    ├── test_m0_core.py                 # Milestone 0: Substrate, logging, and error envelopes
    ├── test_m1_mcp_server.py           # Milestone 1: MCP server schemas and stdio isolation
    ├── test_m2_mcp_gateway.py          # Milestone 2: MCP gateway auth and -32001 interception
    ├── test_m3_streaming_guardrail.py  # Milestone 3: Split-chunk PII redaction and TTFT
    └── test_m4_model_router.py         # Milestone 4: SQLite WAL concurrency and model failover
```