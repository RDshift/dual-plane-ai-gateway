# Zero-Trust Network Deployment & Troubleshooting Runbook

This runbook provides a structured **Symptom $\rightarrow$ Root Cause $\rightarrow$ Diagnostic Check $\rightarrow$ Remediation** workflow for deploying and operating the Dual-Plane AI Gateway inside enterprise zero-trust environments (Kubernetes with read-only root filesystems, corporate TLS-inspecting egress proxies, identity-aware reverse proxies, and restricted container sandboxes).

---

## 1. Fast Triage: Running the Zero-Trust Diagnostic Suite

Before inspecting individual network hops, run the built-in diagnostic doctor via CLI (inside the container/host) or query the admin-gated diagnostics endpoint:

### CLI Preflight Check
~~~bash
python3 scripts/gateway_doctor.py
python3 scripts/gateway_doctor.py --json
~~~

### Runtime API Check (`admin` Bearer Token Required)
~~~bash
curl -s http://127.0.0.1:8000/health/diagnostics \
  -H "Authorization: Bearer tok_admin_live_9x8a7b" | python3 -m json.tool
~~~

### Correlating Client Errors with Internal `stderr` Telemetry
Because the gateway sanitizes all external error payloads (ADR-004), every HTTP response on both `/mcp` and `/v1/chat/completions` includes an `X-Request-Id` header (and `request_id` field on Model Plane JSON errors). Use that ID to locate the exact root-cause event in the gateway's `stderr` JSON logs:

~~~bash
grep "ce22d4cb48d34bbdb36c72a0c0b95a80" /var/log/gateway/stderr.log
~~~

---

## 2. Zero-Trust Troubleshooting Matrix

### Scenario A: Corporate Egress Proxy & TLS Inspection Failures (`HTTP 502` / `HTTP 504`)

* **Symptom:** Calls to `POST /v1/chat/completions` consistently return `HTTP 502` (`upstream_unavailable`) or `HTTP 504` (`upstream_timeout`), while `POST /mcp` works normally. `stderr` logs show `ConnectError` or `SSLCertVerificationError` on `HttpUpstream`.
* **Root Cause:** In zero-trust networks, outbound traffic to external LLM providers (`api.openai.com`, Azure, Bedrock) must traverse an explicit corporate forward proxy (`HTTPS_PROXY`) that performs TLS interception using an enterprise private Root CA. Alternatively, `HTTPS_PROXY` is set globally without `NO_PROXY`, causing local traffic to internal sidecars (`127.0.0.1`) to be misrouted through the corporate proxy.
* **Diagnostic Check:**
  Run `python3 scripts/gateway_doctor.py --json` and inspect `"zero_trust_env"`:
  ~~~json
  "zero_trust_env": {
    "status": "ok",
    "proxy_configured": true,
    "custom_ca_bundle_configured": false,
    "no_proxy_configured": false
  }
  ~~~
  If `proxy_configured` is `true` in a TLS-inspecting environment but `custom_ca_bundle_configured` is `false`, `httpx` will reject the proxy's self-signed certificate chain.
* **Remediation:**
  1. Mount the enterprise Root CA bundle into the container and export `SSL_CERT_FILE=/etc/ssl/certs/enterprise-ca-bundle.pem` (recognized natively by Python `ssl` and `httpx`).
  2. Export `HTTPS_PROXY=http://proxy.corp.internal:8443` and `NO_PROXY=127.0.0.1,localhost,.svc,.cluster.local` so local health checks and sidecar upstreams bypass the egress proxy.

---

### Scenario B: Read-Only Root Filesystems & Ephemeral Mounts (`RuntimeError` at Startup or Quota Reset)

* **Symptom:**
  * *Case 1:* Container crashes on startup with `RuntimeError: Substrate check failed: cannot create SQLite directory` or `SQLite directory not writable`.
  * *Case 2:* Gateway starts, but `gateway_doctor.py` reports `"substrate": {"status": "error", "durable_storage": false}` or `"sqlite_wal": {"status": "error"}`.
* **Root Cause:**
  * *Case 1:* Kubernetes `securityContext.readOnlyRootFilesystem: true` prevents creating the default `<project_root>/data/` directory unless a writable volume is mounted.
  * *Case 2:* `SQLITE_DB_PATH` was pointed at a volatile RAM mount (which wipes tenant rate-limit quotas on pod restart) or a network filesystem (NFSv3) that does not support POSIX file locking required for SQLite `WAL` `-wal` and `-shm` shared-memory sidecar files.
* **Diagnostic Check:**
  Check `"substrate"` and `"sqlite_wal"` in `GET /health/diagnostics`. Both `sqlite_dir_writable` and `durable_storage` must be `true`, and `journal_mode` must be `"wal"`.
* **Remediation:**
  1. Mount a persistent local volume (e.g., Kubernetes `PersistentVolumeClaim` or host-backed `emptyDir` on persistent node disk) at `/var/lib/dual-plane-gateway/data`.
  2. Set `SQLITE_DB_PATH=/var/lib/dual-plane-gateway/data/gateway_ratelimit.db`. Ensure the container UID has `rwx` permissions on the directory so SQLite can create `gateway_ratelimit.db`, `gateway_ratelimit.db-wal`, and `gateway_ratelimit.db-shm`.

---

### Scenario C: Restricted Container Sandboxes Blocking MCP `stdio` Subprocess (`HTTP 502` / `HTTP 504` on `/mcp`)

* **Symptom:** `POST /mcp` returns `HTTP 502` (`Downstream MCP server unavailable`) or `HTTP 504` (`Downstream MCP server timed out`), and `gateway_doctor.py` shows `"mcp_stdio_bridge": {"status": "error", "public_tools_count": 0}`.
* **Root Cause:** Seccomp/AppArmor profiles, distroless Python path mismatches, or corrupted `MCP_SERVER_SCRIPT` paths prevent `McpStdioBridge` from spawning `[sys.executable, settings.mcp_server_script]`. Alternatively, a third-party library added to the MCP server wrote banner text to `stdout`, breaking JSON-RPC line framing.
* **Diagnostic Check:**
  1. Inspect `stderr` JSON logs for `event=diagnostics_bridge_failed` or `BridgeError`.
  2. Test the MCP server binary in isolation:
     ~~~bash
     printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n' | python3 servers/customer_service_mcp.py
     ~~~
     Verify that `stdout` emits exactly one valid JSON line and all log lines appear on `stderr`.
* **Remediation:**
  Ensure `MCP_SERVER_SCRIPT` points to `servers/customer_service_mcp.py` relative to the working directory (or use an absolute path), confirm `PYTHONUNBUFFERED=1` is permitted by the sandbox, and keep `sys.stdout` strictly reserved for JSON-RPC frames.

---

### Scenario D: Identity-Aware Reverse Proxy (IAP / Envoy) Stripping Auth Headers (`HTTP 401`)

* **Symptom:** Legitimate callers receive `HTTP 401 Unauthorized` with `WWW-Authenticate: Bearer realm="mcp-plane"` (JSON-RPC `-32000`) or `WWW-Authenticate: Bearer realm="model-plane"` (`authentication_required`).
* **Root Cause:** An upstream API gateway, Envoy mesh, or Identity-Aware Proxy (IAP) consumed and stripped the incoming `Authorization: Bearer ...` header after validating its own perimeter JWT, forwarding the request to the Dual-Plane AI Gateway without credentials.
* **Diagnostic Check:**
  Inspect `stderr` logs for `event=mcp_auth_failed` or `event=model_auth_failed` correlated by the response's `X-Request-Id` header:
  * `reason=missing_header`: The upstream proxy stripped `Authorization`.
  * `reason=invalid_token`: The header arrived, but either lacked the `Bearer ` prefix or contained a token not registered in `settings.token_registry`.
* **Remediation:**
  * For the **Model Inference Plane (`/v1/chat/completions`)**: Pass the tenant credential via the `X-API-Key: <tenant-key>` header when the perimeter proxy reserves `Authorization` for OIDC/IAP tokens.
  * For the **MCP Plane (`/mcp`)**: Configure the upstream proxy to preserve the `Authorization` header (or map validated upstream role claims into the gateway's Bearer token registry).

---

### Scenario E: Aggressive Upstream Timeouts Under High-Latency Models (`HTTP 504` / Frequent Failovers)

* **Symptom:** Responses on `/v1/chat/completions` frequently carry `X-Model-Provider: secondary` or fail with `HTTP 504` (`upstream_timeout`) when invoking large reasoning prompts.
* **Root Cause:** `UPSTREAM_TIMEOUT_MS` defaults to `3000` ms (3.0 seconds) per the architectural SLA. If an upstream model takes longer than 3000ms to emit its HTTP response headers (e.g., cold-starting a local vLLM weight shard or processing a 32k-token prompt), `ModelFailoverRouter` triggers failover to the secondary provider.
* **Diagnostic Check:**
  Check `"model_plane_config"` in `GET /health/diagnostics` to confirm the active `timeout_ms` value, and check `X-Model-Provider` response headers.
* **Remediation:**
  Ensure the upstream provider flushes HTTP 200 SSE headers immediately upon accepting the request, or tune `UPSTREAM_TIMEOUT_MS` in `.env` if deploying in front of high-time-to-first-byte reasoning endpoints.