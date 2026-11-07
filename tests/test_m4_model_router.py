"""Milestone 4 verification: SQLite sliding-window rate limiter, failover router,
and POST /v1/chat/completions integration with the streaming PII guardrail.

Interface: pytest module (asyncio_mode=auto). Uses conftest fixtures
isolated_sqlite_path, model_plane and gateway_client.
Dependencies: pytest, pytest-asyncio, fastapi.testclient, gateway.model_plane.*.
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.core.config import settings
from gateway.model_plane.failover_router import (
    KIND_RATE_LIMITED,
    KIND_TIMEOUT,
    PROVIDER_PRIMARY,
    PROVIDER_SECONDARY,
    CallableUpstream,
    GatewayUpstreamFailure,
    HttpUpstream,
    ModelFailoverRouter,
    UpstreamError,
)
from gateway.model_plane.model_router import (
    CHAT_COMPLETIONS_PATH,
    HEADER_PROVIDER,
    HEADER_REMAINING,
    configure_model_plane,
)
from gateway.model_plane.rate_limiter import RateLimitDecision, SqliteTokenRateLimiter
from gateway.model_plane.streaming_guardrail import REDACTION_TOKEN, SSE_DONE_FRAME

TOKENS_PER_MINUTE = 50000
WINDOW_SECONDS = 60
FAST_TIMEOUT_MS = 50
SLOW_HANDLER_DELAY_S = 0.5
EXACT_TIMEOUT_MS = 3000
TENANT_A = "tenant-a-key"
TENANT_B = "tenant-b-key"
CONCURRENT_REQUESTS = 80
CONCURRENT_TOKENS_EACH = 1000
SPLIT_SSN_CHUNKS = ("Your SSN 123-45-", "6789 and card 4111 1111 ", "1111 1111 noted.")
LEAK_MARKERS = ("Traceback", "http://", "https://", "Exception(", "Error(", ".py")


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _open_limiter(path: Path, clock: FakeClock | None = None) -> SqliteTokenRateLimiter:
    limiter = SqliteTokenRateLimiter(
        db_path=str(path),
        tokens_per_window=TOKENS_PER_MINUTE,
        window_seconds=WINDOW_SECONDS,
        clock=clock or FakeClock(),
    )
    await limiter.open()
    return limiter


def _rate_limited_handler(*_: Any) -> Any:
    async def handler(_request: dict[str, Any]) -> str:
        raise UpstreamError(KIND_RATE_LIMITED, 429)

    return handler


def _streaming_handler(chunks: tuple[str, ...]) -> Any:
    async def handler(_request: dict[str, Any]) -> list[str]:
        return list(chunks)

    return handler


def _slow_handler(delay_s: float) -> Any:
    async def handler(_request: dict[str, Any]) -> str:
        await asyncio.sleep(delay_s)
        return "late"

    return handler


def _network_error_handler(*_: Any) -> Any:
    async def handler(_request: dict[str, Any]) -> str:
        raise ConnectionError("dial tcp 10.0.0.7:443: connection refused")

    return handler


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _chat_body(tokens: int | None = None, stream: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Summarize my account."}],
        "stream": stream,
    }
    if tokens is not None:
        body["tokens"] = tokens
    return body


def _sse_contents(text: str) -> list[str]:
    contents = []
    for frame in text.split("\n\n"):
        if not frame.startswith("data: ") or frame == SSE_DONE_FRAME.strip():
            continue
        contents.append(json.loads(frame[len("data: "):])["choices"][0]["delta"]["content"])
    return contents


async def test_limiter_creates_file_and_enables_wal(isolated_sqlite_path: Path) -> None:
    assert not isolated_sqlite_path.exists()
    limiter = await _open_limiter(isolated_sqlite_path)
    try:
        assert isolated_sqlite_path.exists()
        assert limiter.journal_mode == "wal"
        probe = sqlite3.connect(str(isolated_sqlite_path))
        try:
            assert probe.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
            tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table';")}
            assert "token_usage" in tables
        finally:
            probe.close()
    finally:
        await limiter.close()


async def test_limiter_tracks_tokens_up_to_limit(isolated_sqlite_path: Path) -> None:
    limiter = await _open_limiter(isolated_sqlite_path)
    try:
        first = await limiter.consume(TENANT_A, 20000)
        assert first == RateLimitDecision(allowed=True, remaining=30000, retry_after_s=0.0)
        second = await limiter.consume(TENANT_A, 30000)
        assert second.allowed is True
        assert second.remaining == 0
        denied = await limiter.consume(TENANT_A, 1)
        assert denied.allowed is False
        assert denied.remaining == 0
        assert 0.0 < denied.retry_after_s <= WINDOW_SECONDS
        assert await limiter.window_usage(TENANT_A) == TOKENS_PER_MINUTE
    finally:
        await limiter.close()


async def test_limiter_rejects_without_inserting(isolated_sqlite_path: Path) -> None:
    limiter = await _open_limiter(isolated_sqlite_path)
    try:
        await limiter.consume(TENANT_A, 49000)
        denied = await limiter.consume(TENANT_A, 2000)
        assert denied.allowed is False
        assert denied.remaining == 1000
        assert await limiter.window_usage(TENANT_A) == 49000
        assert await limiter.row_count() == 1
    finally:
        await limiter.close()


async def test_limiter_tenant_isolation(isolated_sqlite_path: Path) -> None:
    limiter = await _open_limiter(isolated_sqlite_path)
    try:
        assert (await limiter.consume(TENANT_A, TOKENS_PER_MINUTE)).allowed is True
        assert (await limiter.consume(TENANT_A, 1)).allowed is False
        decision_b = await limiter.consume(TENANT_B, 1)
        assert decision_b.allowed is True
        assert decision_b.remaining == TOKENS_PER_MINUTE - 1
    finally:
        await limiter.close()


async def test_limiter_sliding_window_eviction_recovers_quota(isolated_sqlite_path: Path) -> None:
    clock = FakeClock()
    limiter = await _open_limiter(isolated_sqlite_path, clock)
    try:
        await limiter.consume(TENANT_A, 30000)
        clock.advance(30)
        await limiter.consume(TENANT_A, 20000)
        denied = await limiter.consume(TENANT_A, 1)
        assert denied.allowed is False
        assert denied.retry_after_s == pytest.approx(30.0)
        clock.advance(31)
        recovered = await limiter.consume(TENANT_A, 25000)
        assert recovered.allowed is True
        assert recovered.remaining == 5000
        assert await limiter.row_count() == 2
        clock.advance(WINDOW_SECONDS + 1)
        full = await limiter.consume(TENANT_A, TOKENS_PER_MINUTE)
        assert full.allowed is True
        assert full.remaining == 0
        assert await limiter.row_count() == 1
    finally:
        await limiter.close()


async def test_limiter_concurrent_consumers_never_over_allocate(isolated_sqlite_path: Path) -> None:
    limiter = await _open_limiter(isolated_sqlite_path)
    try:
        decisions = await asyncio.gather(
            *(limiter.consume(TENANT_A, CONCURRENT_TOKENS_EACH) for _ in range(CONCURRENT_REQUESTS))
        )
        allowed = [d for d in decisions if d.allowed]
        assert len(allowed) == TOKENS_PER_MINUTE // CONCURRENT_TOKENS_EACH
        assert sum(1 for d in decisions if not d.allowed) == CONCURRENT_REQUESTS - len(allowed)
        assert all(d.retry_after_s > 0 for d in decisions if not d.allowed)
        assert await limiter.window_usage(TENANT_A) == TOKENS_PER_MINUTE
        assert min(d.remaining for d in allowed) == 0
    finally:
        await limiter.close()


async def test_limiter_close_is_idempotent_and_guards_consume(isolated_sqlite_path: Path) -> None:
    limiter = await _open_limiter(isolated_sqlite_path)
    await limiter.close()
    await limiter.close()
    with pytest.raises(RuntimeError):
        await limiter.consume(TENANT_A, 1)
    with pytest.raises(ValueError):
        SqliteTokenRateLimiter(db_path=str(isolated_sqlite_path), tokens_per_window=0)


async def test_failover_on_primary_429() -> None:
    primary = CallableUpstream(PROVIDER_PRIMARY, _rate_limited_handler())
    secondary = CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("ok ", "done")))
    router = ModelFailoverRouter(primary, secondary, timeout_ms=FAST_TIMEOUT_MS)
    completion = await router.complete({"messages": []})
    assert completion.provider == PROVIDER_SECONDARY
    assert await completion.collect() == "ok done"
    assert primary.calls == 1 and secondary.calls == 1


async def test_failover_on_primary_timeout_fast() -> None:
    primary = CallableUpstream(PROVIDER_PRIMARY, _slow_handler(SLOW_HANDLER_DELAY_S))
    secondary = CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("fallback",)))
    router = ModelFailoverRouter(primary, secondary, timeout_ms=FAST_TIMEOUT_MS)
    completion = await router.complete({"messages": []})
    assert completion.provider == PROVIDER_SECONDARY
    assert await completion.collect() == "fallback"


async def test_failover_exact_3000ms_timeout_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[float] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable: Any, timeout: float | None = None) -> Any:
        observed.append(float(timeout))
        if len(observed) == 1:
            awaitable.close()
            raise asyncio.TimeoutError()
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr("gateway.model_plane.failover_router.asyncio.wait_for", recording_wait_for)
    primary = CallableUpstream(PROVIDER_PRIMARY, _streaming_handler(("never",)))
    secondary = CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("after-3000ms",)))
    router = ModelFailoverRouter(primary, secondary)
    assert settings.upstream_timeout_ms == EXACT_TIMEOUT_MS
    assert router.timeout_s == EXACT_TIMEOUT_MS / 1000.0
    completion = await router.complete({"messages": []})
    assert observed == [3.0, 3.0]
    assert completion.provider == PROVIDER_SECONDARY
    assert await completion.collect() == "after-3000ms"


async def test_primary_success_does_not_touch_secondary() -> None:
    primary = CallableUpstream(PROVIDER_PRIMARY, _streaming_handler(("primary",)))
    secondary = CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("secondary",)))
    router = ModelFailoverRouter(primary, secondary, timeout_ms=FAST_TIMEOUT_MS)
    completion = await router.complete({"messages": []})
    assert completion.provider == PROVIDER_PRIMARY
    assert secondary.calls == 0


@pytest.mark.parametrize(
    ("primary_handler", "secondary_handler", "expected_status", "expected_code"),
    [
        (_slow_handler(SLOW_HANDLER_DELAY_S), _slow_handler(SLOW_HANDLER_DELAY_S), 504, "upstream_timeout"),
        (_rate_limited_handler(), _rate_limited_handler(), 503, "upstream_rate_limited"),
        (_rate_limited_handler(), _network_error_handler(), 502, "upstream_unavailable"),
    ],
)
async def test_dual_failure_is_sanitized(
    primary_handler: Any, secondary_handler: Any, expected_status: int, expected_code: str
) -> None:
    router = ModelFailoverRouter(
        CallableUpstream(PROVIDER_PRIMARY, primary_handler),
        CallableUpstream(PROVIDER_SECONDARY, secondary_handler),
        timeout_ms=FAST_TIMEOUT_MS,
    )
    with pytest.raises(GatewayUpstreamFailure) as excinfo:
        await router.complete({"messages": []})
    failure = excinfo.value
    assert failure.http_status == expected_status
    payload = failure.to_payload("req-1")
    assert payload["error"]["code"] == expected_code
    assert payload["error"]["status"] == expected_status
    serialized = json.dumps(payload)
    for marker in LEAK_MARKERS:
        assert marker not in serialized
    assert {f.kind for f in failure.failures} <= {KIND_TIMEOUT, KIND_RATE_LIMITED, "network"}


def _sse_body(chunks: tuple[str, ...]) -> str:
    frames = [
        "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) + "\n\n" for chunk in chunks
    ]
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


async def test_http_upstream_failover_over_mock_transport() -> None:
    seen_hosts: list[str] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        assert request.headers["Authorization"].startswith("Bearer ")
        assert json.loads(request.content)["stream"] is True
        if request.url.host == "primary.internal":
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=_sse_body(("hello ", "from ", "secondary")),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler)) as client:
        router = ModelFailoverRouter(
            HttpUpstream(PROVIDER_PRIMARY, "https://primary.internal/v1", "key-a", client),
            HttpUpstream(PROVIDER_SECONDARY, "https://secondary.internal/v1", "key-b", client),
            timeout_ms=FAST_TIMEOUT_MS,
        )
        completion = await router.complete({"messages": [{"role": "user", "content": "hi"}]})
        assert completion.provider == PROVIDER_SECONDARY
        assert await completion.collect() == "hello from secondary"
    assert seen_hosts == ["primary.internal", "secondary.internal"]


def test_http_missing_api_key_returns_401(gateway_client: TestClient) -> None:
    response = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_http_invalid_body_returns_400(gateway_client: TestClient) -> None:
    response = gateway_client.post(CHAT_COMPLETIONS_PATH, json={"messages": []}, headers=_headers(TENANT_A))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_http_tenant_isolation_and_429(gateway_client: TestClient) -> None:
    router = ModelFailoverRouter(
        CallableUpstream(PROVIDER_PRIMARY, _streaming_handler(("fine",))),
        CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("fine",))),
        timeout_ms=FAST_TIMEOUT_MS,
    )
    configure_model_plane(failover_router=router)
    exhaust = gateway_client.post(
        CHAT_COMPLETIONS_PATH, json=_chat_body(tokens=TOKENS_PER_MINUTE), headers=_headers(TENANT_A)
    )
    assert exhaust.status_code == 200
    assert exhaust.headers[HEADER_REMAINING] == "0"
    assert exhaust.headers[HEADER_PROVIDER] == PROVIDER_PRIMARY

    limited = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(), headers=_headers(TENANT_A))
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    body = limited.json()["error"]
    assert body["code"] == "rate_limit_exceeded"
    assert body["status"] == 429
    assert "request_id" in body

    other = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(), headers={"X-API-Key": TENANT_B})
    assert other.status_code == 200
    assert int(other.headers[HEADER_REMAINING]) > 0


def test_http_sliding_window_recovery_via_injected_clock(gateway_client: TestClient, model_plane: Path) -> None:
    clock = FakeClock()
    limiter = SqliteTokenRateLimiter(
        db_path=str(model_plane), tokens_per_window=TOKENS_PER_MINUTE, window_seconds=WINDOW_SECONDS, clock=clock
    )
    router = ModelFailoverRouter(
        CallableUpstream(PROVIDER_PRIMARY, _streaming_handler(("fine",))),
        CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("fine",))),
        timeout_ms=FAST_TIMEOUT_MS,
    )
    configure_model_plane(rate_limiter=limiter, failover_router=router)
    first = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(tokens=TOKENS_PER_MINUTE), headers=_headers(TENANT_A))
    assert first.status_code == 200
    assert gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(), headers=_headers(TENANT_A)).status_code == 429
    clock.advance(WINDOW_SECONDS + 1)
    recovered = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(), headers=_headers(TENANT_A))
    assert recovered.status_code == 200


def test_http_e2e_failover_streaming_redaction(gateway_client: TestClient) -> None:
    primary = CallableUpstream(PROVIDER_PRIMARY, _rate_limited_handler())
    secondary = CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(SPLIT_SSN_CHUNKS))
    configure_model_plane(failover_router=ModelFailoverRouter(primary, secondary, timeout_ms=FAST_TIMEOUT_MS))
    response = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(stream=True), headers=_headers(TENANT_A))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers[HEADER_PROVIDER] == PROVIDER_SECONDARY
    assert int(response.headers[HEADER_REMAINING]) < TOKENS_PER_MINUTE
    assert response.text.endswith(SSE_DONE_FRAME)
    joined = "".join(_sse_contents(response.text))
    assert joined == f"Your SSN {REDACTION_TOKEN} and card {REDACTION_TOKEN} noted."
    assert "123-45-6789" not in response.text
    assert "4111" not in response.text
    assert primary.calls == 1 and secondary.calls == 1


def test_http_non_streaming_completion_is_redacted(gateway_client: TestClient) -> None:
    primary = CallableUpstream(PROVIDER_PRIMARY, _streaming_handler(("Reach me at ", "jane@exa", "mple.com today.")))
    secondary = CallableUpstream(PROVIDER_SECONDARY, _streaming_handler(("unused",)))
    configure_model_plane(failover_router=ModelFailoverRouter(primary, secondary, timeout_ms=FAST_TIMEOUT_MS))
    response = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(stream=False), headers=_headers(TENANT_A))
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == f"Reach me at {REDACTION_TOKEN} today."
    assert response.headers[HEADER_PROVIDER] == PROVIDER_PRIMARY


def test_http_dual_upstream_failure_is_sanitized(gateway_client: TestClient) -> None:
    configure_model_plane(
        failover_router=ModelFailoverRouter(
            CallableUpstream(PROVIDER_PRIMARY, _network_error_handler()),
            CallableUpstream(PROVIDER_SECONDARY, _rate_limited_handler()),
            timeout_ms=FAST_TIMEOUT_MS,
        )
    )
    response = gateway_client.post(CHAT_COMPLETIONS_PATH, json=_chat_body(), headers=_headers(TENANT_A))
    assert response.status_code == 502
    payload = response.json()
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "upstream_unavailable"
    assert payload["error"]["status"] == 502
    for marker in LEAK_MARKERS:
        assert marker not in response.text
    assert "10.0.0.7" not in response.text


if __name__ == "__main__":
    from tests.conftest import run_module_tests

    raise SystemExit(run_module_tests(__file__))