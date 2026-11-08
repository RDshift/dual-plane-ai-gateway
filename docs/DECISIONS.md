# Architectural Decision Records (ADRs)

**Status Legend:** `Accepted` = Implemented, verified under `pytest -W error` (113 passing tests), and enforced by automated verification gates in `tests/`.

Each record includes a **Plain-Language Summary** for executive and architectural review, an **Alternatives Considered** trade-off matrix, the **Technical Decision & Invariants**, and the **Verification Proof**.

---

## ADR-001: Dual-Plane Separation, Stdio JSON-RPC Bridge Isolation, and Two-Tier MCP Auth Taxonomy

* **Status:** Accepted
* **Scope:** `gateway/mcp_plane/mcp_router.py`, `gateway/mcp_plane/stdio_bridge.py`, `servers/customer_service_mcp.py`, `tests/test_m1_mcp_server.py`, `tests/test_m2_mcp_gateway.py`

### Plain-Language Summary

Enterprise AI gateways handle two fundamentally different types of traffic: **tool calls** (`POST /mcp`), which are short, state-mutating commands that require strict role-based security, and **model completions** (`POST /v1/chat/completions`), which are long-lived text streams that require low latency and provider failover. We separate these into two independent routers within the gateway so streaming load never interferes with tool security. Furthermore, we distinguish between **unauthenticated requests** (missing or invalid token $\rightarrow$ `HTTP 401` with JSON-RPC `-32000`) and **unauthorized tool calls** (valid `viewer` token attempting an `admin_*` tool $\rightarrow$ `HTTP 200` with JSON-RPC `-32001`), blocking both at the gateway perimeter before the downstream MCP subprocess is ever contacted.

### Context

Coupling MCP tool governance and LLM streaming inference in a single pipeline creates cross-plane failure modes: slow upstream model streams can starve synchronous tool authorization checks, and generic retry middleware can accidentally replay non-idempotent tool calls (such as `trigger_refund`). Additionally, the downstream MCP server communicates over `stdio`, where any stray `print()` statement or concurrent interleaved write corrupts the JSON-RPC 2.0 wire protocol.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
| :--- | :--- | :--- | :--- |
| **1. Single Unified Middleware Pipeline** | Fewer files; single auth/logging interceptor. | Couples SSE streaming backpressure with synchronous JSON-RPC tool governance; conflates HTTP REST errors with JSON-RPC 2.0 envelopes. | **Rejected** |
| **2. Spawn a New MCP Subprocess per HTTP Request** | No lock contention between concurrent `/mcp` calls. | High cold-start latency (~100–200ms per tool call) and heavy OS process churn under load. | **Rejected** |
| **3. Dual-Plane Routers + Persistent Lock-Serialized `stdio` Bridge (Selected)** | Clean failure-domain isolation; sub-millisecond warm IPC; trivial proof of frame ordering; clean seam (`exchange()`) to upgrade to a process pool later. | Serializes concurrent MCP requests per gateway worker behind an `asyncio.Lock`. | **Accepted** |

### Technical Decision & Invariants

1. **Dual-Plane Router Isolation:** `mcp_router` owns `POST /mcp`; `model_router` owns `POST /v1/chat/completions`. They share zero mutable state, relying only on `gateway/core/` for configuration, error builders, lifecycle management, and `stderr` structured logging.
2. **Two-Tier MCP Authentication vs. Authorization Contract:**
   * **Unauthenticated (`HTTP 401` / `-32000`):** Missing `Authorization` header, non-`Bearer` scheme, empty token, or unrecognized token is rejected with `HTTP 401 Unauthorized`, `WWW-Authenticate: Bearer realm="mcp-plane"`, `X-Request-Id`, and JSON-RPC error `-32000` (`Authentication required: missing, malformed, or invalid Bearer token.`). Token matching uses `hmac.compare_digest` to prevent timing attacks.
   * **Unauthorized Role (`HTTP 200` / `-32001`):** Authenticated non-admin (`viewer`) callers invoking `tools/call` where `params.name` (trimmed, case-folded) starts with `admin_` are intercepted at the gateway with `HTTP 200 OK` and JSON-RPC error `-32001` (`Unauthorized Tool Call`).
   * **Zero Downstream Touch:** Neither `-32000` nor `-32001` ever writes to the child MCP process.
3. **Strict Stdio Wire Isolation & Daemon Queue Pump:** `servers/customer_service_mcp.py` reserves `sys.stdout` strictly for newline-delimited JSON-RPC 2.0 frames and writes all logs as structured JSON to `sys.stderr`. A daemon thread pumps `sys.stdin.readline()` into an `asyncio.Queue` so `SIGTERM` shutdown is never blocked by a pending synchronous read.
4. **Unadvertised Downstream Admin Tool Registry:** `customer_service_mcp.py` advertises only `get_customer_record` and `trigger_refund` in `tools/list`, while keeping `admin_reset_key` in an internal registry reachable only when an authorized `admin` call is forwarded by the gateway.
5. **Protocol-Level `-32602` Schema Rejection:** Pydantic v2 validation errors (`GetCustomerRecordArgs`, `TriggerRefundArgs`) map directly to JSON-RPC `-32602` (`Invalid params`) with sanitized `loc/type/msg` triples (never echoing raw input values).

### Consequences & Proof

* **Zero Downstream Exposure:** Proven by `test_missing_bearer_header_returns_401`, `test_invalid_bearer_header_returns_401`, and `test_viewer_blocked_from_admin_tool_without_downstream_call` (`stdio_bridge.downstream_call_count` remains unchanged).
* **Stdout Wire Purity:** Proven by `StdioEndToEndTests.test_stdout_contains_only_jsonrpc_frames`.

---

## ADR-002: Zero-Buffering Rolling-Tail Holdback for Streaming PII Redaction

* **Status:** Accepted (Amended in CR-05: default holdback cap raised from `128` to `256` chars for full RFC 5321 compliance)
* **Scope:** `gateway/model_plane/streaming_guardrail.py`, `tests/test_m3_streaming_guardrail.py`

### Plain-Language Summary

When an LLM streams text token-by-token, sensitive data like an SSN (`123-45-` then `6789`) or an email address often arrives split across two or three separate chunks. If we only scan each chunk by itself, split PII slips through unredacted. If we buffer the entire response before scanning, the user waits seconds for the first word to appear. Instead, our guardrail immediately forwards all safe text ending in spaces or punctuation, and holds back only the trailing fragment of an unfinished word or number (capped at `256` characters). Once the next chunk completes that word or number, it is scanned, redacted if necessary, and flushed immediately.

### Context

Task 3 requires real-time redaction of emails, US SSNs, and 16-digit credit card numbers over streaming deltas while minimizing Time To First Token (TTFT) and avoiding full-response memory accumulation. Under RFC 5321, valid email addresses can reach up to 254 characters in total length.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
| :--- | :--- | :--- | :--- |
| **1. Stateless Per-Chunk Regex** | Zero latency; zero state. | Fails completely whenever an email, SSN, or credit card splits across SSE chunk boundaries. | **Rejected** |
| **2. Full-Response Buffering** | 100% regex context. | Destroys streaming UX (TTFT equals total generation time); memory grows $O(N)$ with response size. | **Rejected** |
| **3. Sentence / Line Buffering** | Simple delimiter logic. | Holds back entire paragraphs when the model generates long lists or code blocks without newlines; still stalls TTFT. | **Rejected** |
| **4. Maximal Token-Suffix Holdback Capped at 256 Chars (Selected)** | Immediate TTFT on safe text; catches all split emails (up to RFC 5321 254-char max), SSNs, and cards; $O(1)$ memory bound. | Holds the final unfinished word of a chunk until the next delta arrives (1 token of latency on trailing words). | **Accepted** |

### Technical Decision & Qualified Mathematical Invariant

Define the **token alphabet** $T = \text{\texttt{[A-Za-z0-9.\_\%+@-]}}$ and the **card-separator rule** where a single ASCII space (`' '`) is eligible only when immediately preceded by an ASCII digit (`0-9`). On each delta:
1. Concatenate `window = pending + delta`.
2. Compute `tail` as the longest suffix of `window` whose characters satisfy $T$ or the card-separator rule, capped at `max_holdback_chars` (default `256`, configurable via `STREAM_GUARDRAIL_MAX_HOLDBACK_CHARS`).
3. Emit `redact(window[:-len(tail)])` immediately and retain `tail`.

**Qualified Invariant (No Straddling up to `max_holdback_chars`):** Every character of every target email, SSN, and 16-digit credit card pattern is drawn from $T$ or is a digit-preceded space. Therefore, any match of length $L \le \text{\texttt{max\_holdback\_chars}}$ ($256$) is a contiguous run of tail-eligible characters that cannot straddle the boundary between the emitted prefix and the held tail (if it did, its first character in the emitted region would be tail-eligible and adjacent to `tail`, contradicting maximality). Because RFC 5321 bounds valid emails at 254 characters (and SSNs/cards are $\le 19$ characters), setting `max_holdback_chars = 256` guarantees zero straddling across all standard target patterns while releasing unbroken non-whitespace token runs longer than 256 characters (such as base64 blobs) FIFO to preserve $O(1)$ memory.

### Consequences & Proof

* **RFC 5321 Long Email Split Redaction:** Proven by `test_long_rfc5321_email_over_128_chars_redacted_across_chunks` (a 196-character email split across three chunks is completely redacted).
* **Immediate TTFT & Bounded Memory:** Proven by `test_safe_prefix_flushes_before_stream_completes` (`holdback_len == 0`) and `test_holdback_is_bounded_on_long_token_runs` (`holdback_len <= 256` over 32 KB of unbroken token characters).

---

## ADR-003: Durable On-Disk SQLite WAL Sliding-Window Token Rate Limiter

* **Status:** Accepted (Amended in CR-01: replaced volatile tmpfs auto-detection with durable project-relative on-disk default `data/gateway_ratelimit.db`)
* **Scope:** `gateway/core/config.py`, `gateway/model_plane/rate_limiter.py`, `tests/test_m0_core.py`, `tests/test_m4_model_router.py`

### Plain-Language Summary

To prevent any single tenant from exhausting model capacity or billing budgets, the gateway enforces a strict sliding-window limit of `50,000 tokens per 60 seconds` per API key. Usage is stored in a durable, on-disk SQLite database (`data/gateway_ratelimit.db`) running in Write-Ahead Logging (`WAL`) mode so token quotas survive process restarts and work safely across concurrent requests. Raw API keys are hashed with SHA-256 before being written to disk so database files never expose tenant secrets.

### Context

Task 4 requires a token-aware sliding-window rate limiter backed by on-disk SQLite. Fixed-window counters allow 2x burst abuse at minute boundaries, and naive read-then-write queries suffer from Time-of-Check to Time-of-Use (TOCTOU) race conditions when concurrent async requests arrive simultaneously. Additionally, an enterprise rate limiter must persist state on durable storage rather than volatile RAM filesystems so restarts do not reset exhausted tenant quotas.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
| :--- | :--- | :--- | :--- |
| **1. In-Memory Token Bucket / Volatile RAM DB** | Fast; zero disk I/O. | Violates durable on-disk requirement; resets all quotas on process restart; cannot share state across worker processes. | **Rejected** |
| **2. SQLite Fixed-Window Counter** | Single row per tenant. | Allows tenants to spend 100,000 tokens in 2 seconds straddling the minute boundary. | **Rejected** |
| **3. External Redis Cluster** | Ideal for multi-node clusters. | Requires external daemon infrastructure outside a self-contained assessment repository. | **Deferred (Seam Provided)** |
| **4. Durable On-Disk SQLite WAL + `BEGIN IMMEDIATE` Sliding Window (Selected)** | Survives restarts; true 60s sliding window; zero TOCTOU over-allocation; zero external daemon dependencies. | Single-host storage scope (requires shared volume or Redis swap for multi-node clusters). | **Accepted** |

### Technical Decision & Invariants

1. **Durable Default Path with Auto-Created Parent Directory:** `settings.sqlite_db_path` defaults to `<project_root>/data/gateway_ratelimit.db` (overridable via `SQLITE_DB_PATH`). At startup, `verify_substrate()` automatically creates missing parent directories (`mkdir(parents=True, exist_ok=True)`) and verifies write permissions.
2. **WAL Mode + `BEGIN IMMEDIATE` Atomic Transactions:** `SqliteTokenRateLimiter.open()` sets `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, and `PRAGMA busy_timeout=5000;`. Each `consume()` call runs on a worker thread behind an `asyncio.Lock` and executes `BEGIN IMMEDIATE` (with 6-attempt exponential backoff + jitter on `SQLITE_BUSY`) so expired-row eviction (`recorded_at <= now - window`), active sum calculation, and row insertion occur as a single serializable transaction.
3. **SHA-256 Credential Hashing:** Tenant API keys are stored as 32-character SHA-256 hex digests, never in plaintext.
4. **Exact Oldest-Row `Retry-After` Derivation:** When a request is denied, the limiter walks the tenant's live rows oldest-first to compute the exact number of seconds until enough tokens expire to admit the request.

### Consequences & Proof

* **Cross-Restart Durability:** Proven by `test_limiter_state_persists_across_restarts` (20,000 tokens consumed before `.close()` remain accounted for when a new limiter opens the same file and consumes 10,000 more, leaving exactly 20,000 remaining).
* **Concurrency Safety:** Proven by `test_limiter_concurrent_consumers_never_over_allocate` (80 concurrent 1,000-token requests against a 50,000 budget admit exactly 50 and reject 30).

---

## ADR-004: Pre-Stream Failover Race and Sanitized Error Taxonomy

* **Status:** Accepted
* **Scope:** `gateway/model_plane/failover_router.py`, `gateway/model_plane/model_router.py`, `tests/test_m4_model_router.py`

### Plain-Language Summary

In HTTP streaming (SSE), once the gateway sends `HTTP 200 OK` headers to the client, it can no longer switch providers cleanly or return an HTTP error status code if the model endpoint is down. Therefore, our router verifies that the primary provider connects and commits to streaming within `3000ms` *before* sending HTTP headers to the caller. If the primary provider times out (`>3000ms`), returns `HTTP 429` (rate-limited), or returns `HTTP 5xx`, the gateway transparently switches to the secondary backup provider within the same request. If both providers fail, the client receives a clean, standardized JSON error envelope (`502`, `503`, or `504`) with a correlation `request_id` and zero leaked stack traces or internal URLs.

### Context

Task 4 requires automatic failover to a secondary backup model provider when the primary endpoint returns `HTTP 429` or times out after `3000ms`, along with standardized error payloads that never leak upstream stack traces or internal implementation details.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
| :--- | :--- | :--- | :--- |
| **1. Lazy Connection Inside `StreamingResponse` Generator** | Simple FastAPI generator pattern. | Commits `HTTP 200 OK` before contacting upstream; if both providers fail, the client gets a broken 200 stream instead of `502`/`503`/`504`. | **Rejected** |
| **2. Mid-Stream Replay on Provider Drop** | Attempts recovery even if a provider dies halfway through generation. | Duplicates already-rendered tokens on the client screen or requires full buffering (violating Task 3 TTFT). | **Rejected** |
| **3. Two-Phase `start()` / `stream()` Pre-Header Failover (Selected)** | Resolves timeouts, 429s, and 5xx errors before HTTP headers are sent; preserves clean `502`/`503`/`504` status codes on dual failure and immediate TTFT once streaming begins. | Mid-stream disconnects after the first chunk terminate the stream rather than replaying tokens. | **Accepted** |

### Technical Decision & Invariants

1. **Two-Phase Provider Protocol:** `UpstreamProvider.start()` performs connection, authentication, and HTTP status verification inside `asyncio.wait_for(..., timeout=3.0)`, returning a `CompletionStream` iterator only after the upstream provider commits to a valid stream.
2. **Deterministic Sanitized Error Taxonomy:** Dual-provider failures raise `GatewayUpstreamFailure`, which classifies failure tuples `(provider, kind, status)` without retaining raw exception strings:
   * All failures `timeout` $\rightarrow$ `HTTP 504` (`upstream_timeout`)
   * All failures `rate_limited` (`429`) $\rightarrow$ `HTTP 503` (`upstream_rate_limited`)
   * Any other combination (`5xx`, network error, mixed) $\rightarrow$ `HTTP 502` (`upstream_unavailable`)

### Consequences & Proof

* **3000ms Boundary & 429 Failover:** Proven by `test_failover_exact_3000ms_timeout_boundary`, `test_failover_on_429`, and `test_http_e2e_failover_streaming_redaction`.
* **Zero Upstream Leakage:** Proven by `test_dual_failure_is_sanitized` and `test_http_dual_upstream_failure_is_sanitized`.

---

## ADR-005: Zero-Trust Network Deployment Diagnostics and Observability Hygiene

* **Status:** Accepted (Added in CR-04)
* **Scope:** `gateway/core/diagnostics.py`, `gateway/main.py`, `scripts/gateway_doctor.py`, `docs/TROUBLESHOOTING.md`, `tests/test_m0_core.py`

### Plain-Language Summary

Deploying an AI gateway inside an enterprise zero-trust environment introduces infrastructure failure modes outside application logic: corporate TLS-inspecting egress proxies (`HTTPS_PROXY`, custom CA bundles), read-only container root filesystems, and sandboxed subprocess policies. However, security rules forbid exposing internal URLs, environment variable values, or stack traces in error responses. To make the gateway operable in zero-trust networks without weakening error sanitization, we provide an admin-only diagnostics endpoint (`GET /health/diagnostics`), a preflight CLI tool (`scripts/gateway_doctor.py`), and `X-Request-Id` log correlation across both planes.

### Context

The assessment overview highlights *"troubleshooting zero-trust network deployments"* alongside strict error sanitization (ADR-004). Operators need a deterministic way to verify whether a deployment failure is caused by a non-writable SQLite mount, a missing corporate CA bundle, an egress proxy misconfiguration, or a broken MCP `stdio` subprocess pipe—without leaking sensitive configuration values.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
| :--- | :--- | :--- | :--- |
| **1. Include Detailed Error Reasons in Client `502`/`504` Payloads** | Easy debugging from client logs. | Violates Task 4 rubric and ADR-004 by leaking internal hostnames, proxy URLs, and exception details to tenants. | **Rejected** |
| **2. Unauthenticated `/health` Dumping Full Config** | Zero friction for monitoring tools. | Exposes internal topology and configuration to unauthenticated network scanners. | **Rejected** |
| **3. Public Shallow `/health` + Admin-Gated `/health/diagnostics` + CLI Doctor (Selected)** | Keeps `/health` safe for load balancers; gives authorized admins and CI/CD preflight scripts deep, boolean-sanitized visibility; correlates client errors via `X-Request-Id`. | Requires an `admin` Bearer token or local shell access to view deep diagnostics. | **Accepted** |

### Technical Decision & Invariants

1. **Five Non-Destructive Diagnostic Probes (`run_zero_trust_diagnostics`):**
   * `substrate`: Verifies `SQLITE_DB_PATH` parent directory writability (`sqlite_dir_writable`) and confirms the database is not placed on volatile shared memory (`durable_storage`).
   * `zero_trust_env`: Reports boolean presence only (`proxy_configured`, `custom_ca_bundle_configured`, `no_proxy_configured`) for `HTTPS_PROXY`/`HTTP_PROXY`, `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`, and `NO_PROXY`—never echoing URLs or file paths.
   * `sqlite_wal`: Opens the rate-limiter database and verifies `PRAGMA journal_mode` equals `"wal"`.
   * `mcp_stdio_bridge`: Executes a live `tools/list` round-trip over the `stdio` bridge and records `public_tools_count` and `latency_ms`.
   * `model_plane_config`: Reports active `timeout_ms`, `rate_limit_tokens_per_minute`, and `holdback_chars`.
2. **RBAC-Protected Runtime Endpoint (`GET /health/diagnostics`):** Unauthenticated callers receive `HTTP 401`; `viewer` tokens receive `HTTP 403` (`insufficient_role`); `admin` tokens receive `HTTP 200` (`status == "ok"`) or `HTTP 503` (`status == "degraded"`).
3. **Standalone CLI Doctor (`scripts/gateway_doctor.py`):** Runs the same diagnostic suite from the command line (`--json` or human-readable table) and executes `lifecycle_manager.execute_shutdown()` in a `finally` block so zero orphaned MCP processes remain after the check.

### Consequences & Proof

* **RBAC & Sanitization Enforcement:** Proven by `test_m0_diagnostics_requires_bearer_token`, `test_m0_diagnostics_rejects_viewer_role`, and `test_m0_diagnostics_admin_reports_all_sections_ok`.
* **CLI Preflight Execution:** Proven by `test_m0_gateway_doctor_json_exit_zero`.