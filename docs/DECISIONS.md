# Architectural Decision Records (ADRs)

**Status Legend:** `Accepted` = Implemented, verified under `-W error`, and enforced by automated tests in `tests/`.  
Each record documents the architectural context, the design decision, the invariant it protects, and the verification tests that prove compliance.

---

## ADR-001: Dual-Plane Separation and Stdio JSON-RPC Bridge Isolation

* **Status:** Accepted
* **Scope:** `gateway/mcp_plane/mcp_router.py`, `gateway/mcp_plane/stdio_bridge.py`, `servers/customer_service_mcp.py`, `tests/test_m1_mcp_server.py`, `tests/test_m2_mcp_gateway.py`

### Context

An enterprise AI security gateway governs two fundamentally different traffic classes:
1. **MCP Context & Tool Traffic (`POST /mcp`)**: Discrete, state-mutating JSON-RPC 2.0 calls where a single authorization bypass (such as an unprivileged caller invoking `admin_reset_key`) or schema defect causes immediate operational damage.
2. **Model Inference Traffic (`POST /v1/chat/completions`)**: Long-lived Server-Sent Events (SSE) token streams where buffering destroys Time To First Token (TTFT) and upstream provider instability requires sub-second failover.

Forcing both workloads through an artificial single-pipe conveyor belt couples incompatible failure modes: slow streaming completions can starve synchronous tool authorization checks, and streaming retry logic can accidentally replay non-idempotent tool mutations. Furthermore, the downstream MCP server communicates over `stdio`, where a single stray `print()` or unsynchronized concurrent write corrupts the JSON-RPC wire framing.

### Decision

1. **Dual-Plane Router Isolation with Flattened Namespaces:** The gateway separates traffic into `gateway/mcp_plane/mcp_router.py` (`POST /mcp`) and `gateway/model_plane/model_router.py` (`POST /v1/chat/completions`). The two planes share zero mutable state, relying only on `gateway/core/` for configuration, error envelopes, signal lifecycle management, and `stderr` structured logging.
2. **Strict Stdio Wire Isolation & Daemon Queue Pump:** `servers/customer_service_mcp.py` reserves `sys.stdout` exclusively for newline-delimited JSON-RPC 2.0 response frames. All operational telemetry is emitted as structured JSON to `sys.stderr`. To prevent blocking `sys.stdin.readline()` calls from hanging process shutdown on `SIGTERM`, the server reads `stdin` on a dedicated daemon pump thread that feeds an `asyncio.Queue`, allowing the main event loop to race queue reads against the shutdown event cleanly.
3. **Lock-Serialized Stdio Bridge with Request-ID Pairing:** `McpStdioBridge` manages the child subprocess behind an event-loop-bound `asyncio.Lock`. Every request-response exchange is guarded by `asyncio.wait_for` (`UPSTREAM_TIMEOUT_MS`) and verified by matching JSON-RPC `id` fields so a delayed frame from a timed-out request can never bleed into a subsequent caller. On timeout or broken pipe, the child process is terminated, reaped, and transparently respawned on the next request.
4. **Perimeter `admin_*` Interception & Unadvertised Downstream Admin Registry:** `servers/customer_service_mcp.py` maintains two distinct registries: a public registry (`get_customer_record` and `trigger_refund`) returned by `tools/list`, and an internal administrative registry (`admin_reset_key`). At the gateway perimeter, `mcp_router.py` inspects `tools/call` payloads; if `params.name` (whitespace-trimmed and case-folded) starts with `admin_` and the caller's Bearer token resolves to a non-admin role, the gateway immediately returns JSON-RPC error `-32001` (`Unauthorized Tool Call`) without touching the stdio bridge.
5. **Protocol-Level `-32602` Schema Rejection vs. Business Logic Errors:** Because the high-level `mcp` SDK decorator wraps tool exceptions into normal `CallToolResult(isError=True)` payloads, `customer_service_mcp.py` validates inputs directly against strict Pydantic v2 models (`GetCustomerRecordArgs` and `TriggerRefundArgs` with `extra="forbid"`, explicit whitespace rejection on `customer_id`, and `math.isfinite` bounds on `amount`) and maps `ValidationError` directly to JSON-RPC error `-32602` (`Invalid params`). Error payloads expose only sanitized `loc`, `type`, and `msg` fields—never echoing sensitive or malformed raw input values back to the caller.

### Consequences & Proof

* **Zero Downstream Exposure on Unauthorized Calls:** Verified by `test_viewer_blocked_from_admin_tool_without_downstream_call`, which proves `stdio_bridge.downstream_call_count` remains strictly unchanged when a `viewer` invokes `admin_reset_key`.
* **Wire Purity:** Verified by `StdioEndToEndTests.test_stdout_contains_only_jsonrpc_frames`, confirming zero non-JSON bytes on `stdout` across initialization, tool listing, valid executions, and schema rejections.
* **Lifecycle Re-Arming:** `LifecycleManager` resets its shutdown latch in a `finally` block and registers the bridge teardown hook once per process, guaranteeing zero orphaned MCP subprocesses across repeated `TestClient` lifespans.

---

## ADR-002: Zero-Buffering Rolling-Tail Holdback for Streaming PII Redaction

* **Status:** Accepted
* **Scope:** `gateway/model_plane/streaming_guardrail.py`, `tests/test_m3_streaming_guardrail.py`

### Context

Upstream LLMs emit SSE token deltas with arbitrary chunk boundaries—for example, `"123-45-"` in chunk 1 and `"6789"` in chunk 2, or an email address split across three chunks. Applying regex matching naively per chunk fails to redact any split PII pattern. Conversely, buffering the entire completion before running redaction guarantees accuracy at the cost of destroying streaming responsiveness (making TTFT equal to total generation time and memory consumption $O(N)$ with response length).

### Decision

Implement a stateful `StreamingPiiGuardrail` that holds back only the **maximal trailing suffix** of each chunk window that could plausibly form the prefix of an incomplete email address, US Social Security Number (`XXX-XX-XXXX`), or 16-digit credit card number.

Define the **token alphabet** $T = \text{\texttt{[A-Za-z0-9.\_\%+@-]}}$ and the **card-separator rule** where a single ASCII space (`' '`) is eligible only when immediately preceded by an ASCII digit (`0-9`). On each arriving text delta:
1. Concatenate `window = pending_tail + delta`.
2. Compute `tail` as the longest contiguous suffix of `window` whose characters belong to $T$ or satisfy the digit-preceded space rule, bounded by `max_holdback_chars` (default `128`).
3. Immediately apply regex redaction (`[REDACTED]`) to the safe prefix `window[:-len(tail)]` and yield it as an SSE frame (`data: {"choices":[{"delta":{"content":"..."}}]}\n\n`).
4. Retain `tail` as `pending_tail` for the next delta, and redact + flush any remaining tail upon stream completion prior to emitting `data: [DONE]\n\n`.

### Mathematical Invariant (No-Straddle Property)

Every character of every target PII pattern (email, SSN, and 16-digit credit card with space or hyphen separators) is either in $T$ or is a digit-preceded space. Consequently, any complete PII match forms a contiguous run of tail-eligible characters. Suppose for contradiction that a complete match straddled the boundary between `safe_prefix` and `tail`: then the first character of the match inside `safe_prefix` would be tail-eligible and contiguous with `tail`, contradicting the construction of `tail` as the *maximal* tail-eligible suffix. Therefore, every complete PII pattern lies either 100% inside `safe_prefix` (where it is immediately redacted) or 100% inside `tail` (where it is held until subsequent deltas complete or terminate the token).

### Consequences & Proof

* **Immediate TTFT on Safe Text:** Punctuation or trailing whitespace breaks the tail-eligibility chain, causing `safe_prefix` to flush on the very first delta with `holdback_len == 0` (`test_safe_prefix_flushes_before_stream_completes`).
* **Strict $O(1)$ Memory Bound:** Holdback memory never exceeds `max_holdback_chars` (`128` bytes, over 3x the length of SSNs, credit cards, and standard emails), preventing adversarial memory exhaustion on unbroken 32 KB base64 or URL strings (`test_holdback_is_bounded_on_long_token_runs`).
* **Adversarial Split Resilience:** Proven across mid-hyphen SSN splits, 3-way email splits, mid-digit credit card splits, and 1-character-per-chunk streams (`test_m3_streaming_guardrail.py`).

---

## ADR-003: On-Disk SQLite WAL Sliding-Window Token Rate Limiter

* **Status:** Accepted
* **Scope:** `gateway/core/config.py`, `gateway/model_plane/rate_limiter.py`, `tests/test_m4_model_router.py`

### Context

The Model Inference Plane must enforce a strict 50,000 tokens/minute quota per tenant API key backed by on-disk SQLite. Fixed-window counters permit 2x burst abuse across minute boundaries; naive `SELECT SUM(...)` followed by `INSERT` creates Time-of-Check to Time-of-Use (TOCTOU) race conditions under async concurrency; and writing high-frequency rate-limit rows to boot flash media causes unnecessary storage wear and lock contention.

### Decision

1. **Tier 1 Volatile Storage Auto-Detection:** `gateway/core/config.py` dynamically inspects `/dev/shm` on POSIX hosts (Linux RAM-backed tmpfs) to place the on-disk SQLite database on zero-wear Tier 1 volatile storage by default, falling back cleanly to `tempfile.gettempdir()` on macOS and Windows when `SQLITE_DB_PATH` is unset.
2. **WAL Mode + Atomic `BEGIN IMMEDIATE` Transactions:** `SqliteTokenRateLimiter.open()` configures `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, and `PRAGMA busy_timeout=5000;`. Every `consume()` operation executes on a worker thread serialized by an in-process `asyncio.Lock` and wrapped in a `BEGIN IMMEDIATE` transaction with a 6-attempt exponential backoff + random jitter loop for cross-process `SQLITE_BUSY` resilience.
3. **True Sliding-Window Eviction & Atomic Deduction:** Inside the `BEGIN IMMEDIATE` transaction:
   * Rows with `recorded_at <= now - window_seconds` are deleted.
   * Active window usage for the tenant is summed (`SELECT COALESCE(SUM(tokens), 0)`).
   * If `current_tokens + requested_tokens > tokens_per_window`, the transaction rolls back/completes without inserting a row and computes the exact `retry_after_s` by walking the tenant's oldest active rows until enough tokens expire to admit the request.
   * Otherwise, the new token record is inserted and committed atomically.
4. **Cryptographic Credential Hygiene:** Raw tenant API keys are never written to SQLite; keys are hashed with SHA-256 (truncated to 32 hex chars) before indexing or storage.

### Consequences & Proof

* **Zero Over-Allocation Under High Concurrency:** Verified by `test_limiter_concurrent_consumers_never_over_allocate`, where 80 concurrent coroutines requesting 1,000 tokens each against a 50,000-token budget result in exactly 50 admissions and 30 rejections.
* **Deterministic Window Recovery & Tenant Isolation:** Verified via injectable clock fixtures (`test_limiter_sliding_window_eviction_recovers_quota` and `test_http_tenant_isolation_and_429`) without slow wall-clock sleeps.

---

## ADR-004: Pre-Stream Failover Race and Error Sanitization Taxonomy

* **Status:** Accepted
* **Scope:** `gateway/model_plane/failover_router.py`, `gateway/model_plane/model_router.py`, `tests/test_m4_model_router.py`

### Context

In an SSE streaming gateway, once `HTTP 200 OK` and `Content-Type: text/event-stream` headers are flushed to the client, the HTTP status code is immutable. If upstream timeout detection or HTTP 429 handling is deferred until inside the `StreamingResponse` generator body, failover becomes fragile and dual-provider failures cannot return proper HTTP 502/503/504 status codes. Additionally, raw upstream exceptions (`httpx.ConnectError`, provider URLs, or Python tracebacks) must never leak to external tenants.

### Decision

1. **Two-Phase `start()` vs. `stream()` Provider Protocol:** `UpstreamProvider.start()` executes connection establishment, authentication, and upstream status validation inside `asyncio.wait_for(..., timeout=settings.upstream_timeout_ms / 1000.0)` (default `3.0` seconds), returning a `CompletionStream` iterator only after the provider commits to a valid response.
2. **Pre-Header Failover Resolution:** `ModelFailoverRouter.complete()` invokes `primary.start()` first. If the primary raises a timeout (>3000ms), network error, HTTP 429 (`Too Many Requests`), or HTTP 5xx, the router catches the failure and immediately invokes `secondary.start()` within the same client request before `model_router.py` constructs the FastAPI `StreamingResponse`.
3. **Deterministic Sanitized Error Taxonomy:** If both primary and secondary providers fail, `GatewayUpstreamFailure` classifies the recorded failure tuples `(provider, kind, status)` into a strict, leak-free HTTP response via `build_gateway_error()`:
   * All failures `timeout` $\rightarrow$ `HTTP 504` (`upstream_timeout`)
   * All failures `rate_limited` (HTTP 429) $\rightarrow$ `HTTP 503` (`upstream_rate_limited`)
   * Any other combination (HTTP 5xx, network errors, mixed) $\rightarrow$ `HTTP 502` (`upstream_unavailable`)

### Consequences & Proof

* **Seamless Single-Request Failover:** When the primary times out at 3000ms or returns HTTP 429, the client receives a single continuous stream from the secondary provider tagged with `X-Model-Provider: secondary` (`test_http_e2e_failover_streaming_redaction` and `test_failover_exact_3000ms_timeout_boundary`).
* **Zero Information Leakage:** Verified by `test_dual_failure_is_sanitized` and `test_http_dual_upstream_failure_is_sanitized`, proving that upstream URLs, internal hostnames, file paths, and Python exception reprs are completely absent from error payloads.