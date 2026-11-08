"""Resilient primary/secondary model router with sanitized failure envelopes.

Interface: ModelFailoverRouter(primary, secondary, timeout_ms).complete(request)
returns a CompletionStream (async iterator of text deltas, plus .provider).
Failover to the secondary provider happens when the primary times out, raises
a network error, or answers HTTP 429 or 5xx before streaming begins. If both
providers fail, GatewayUpstreamFailure is raised; its to_payload() maps to
build_gateway_error with HTTP 502, 503 or 504 and never carries upstream
URLs, stack traces or exception reprs.

Providers implement the UpstreamProvider protocol. CallableUpstream wraps an
injectable async handler for deterministic tests. HttpUpstream speaks the
OpenAI chat completions SSE protocol over an httpx.AsyncClient.

Dependencies: httpx, gateway.core.config, gateway.core.errors.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from gateway.core.config import settings
from gateway.core.errors import build_gateway_error

HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_FLOOR = 500
HTTP_BAD_GATEWAY = 502
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_GATEWAY_TIMEOUT = 504

KIND_TIMEOUT = "timeout"
KIND_RATE_LIMITED = "rate_limited"
KIND_SERVER_ERROR = "server_error"
KIND_NETWORK = "network"

PROVIDER_PRIMARY = "primary"
PROVIDER_SECONDARY = "secondary"

SSE_DATA_PREFIX = "data:"
SSE_DONE_TOKEN = "[DONE]"
CHAT_COMPLETIONS_PATH = "/chat/completions"

MSG_UPSTREAM_TIMEOUT = "All model providers timed out."
MSG_UPSTREAM_RATE_LIMITED = "All model providers are rate limited."
MSG_UPSTREAM_UNAVAILABLE = "All model providers are unavailable."

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Raised by a provider when it cannot begin streaming a completion."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class UpstreamFailure:
    """Sanitized record of one provider failure (no payloads, no URLs)."""

    provider: str
    kind: str
    status: int | None = None


class GatewayUpstreamFailure(Exception):
    """Both providers failed. Carries only sanitized classification data."""

    def __init__(self, failures: Iterable[UpstreamFailure]) -> None:
        self.failures = tuple(failures)
        self.http_status, self.code, self.message = self._classify(self.failures)
        super().__init__(self.message)

    @staticmethod
    def _classify(failures: tuple[UpstreamFailure, ...]) -> tuple[int, str, str]:
        kinds = {failure.kind for failure in failures}
        if kinds == {KIND_TIMEOUT}:
            return HTTP_GATEWAY_TIMEOUT, "upstream_timeout", MSG_UPSTREAM_TIMEOUT
        if kinds == {KIND_RATE_LIMITED}:
            return HTTP_SERVICE_UNAVAILABLE, "upstream_rate_limited", MSG_UPSTREAM_RATE_LIMITED
        return HTTP_BAD_GATEWAY, "upstream_unavailable", MSG_UPSTREAM_UNAVAILABLE

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return build_gateway_error(
            code=self.code,
            message=self.message,
            http_status=self.http_status,
            request_id=request_id,
        )


@dataclass
class CompletionStream:
    """Async iterator of text deltas tagged with the provider that produced them."""

    provider: str
    deltas: AsyncIterator[str]

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        return await self.deltas.__anext__()

    async def collect(self) -> str:
        return "".join([delta async for delta in self.deltas])


class UpstreamProvider(Protocol):
    """A provider either returns a delta iterator or raises UpstreamError."""

    name: str

    async def start(self, request: dict[str, Any]) -> AsyncIterator[str]: ...


Handler = Callable[[dict[str, Any]], Awaitable[Any]]


async def _iterate_handler_result(result: Any) -> AsyncIterator[str]:
    if isinstance(result, str):
        yield result
        return
    if hasattr(result, "__aiter__"):
        async for delta in result:
            yield str(delta)
        return
    for delta in result:
        yield str(delta)


@dataclass
class CallableUpstream:
    """Provider backed by an injectable async handler.

    The handler receives the request dict and returns a str, an iterable or an
    async iterable of deltas. It signals failure by raising UpstreamError,
    httpx exceptions, or asyncio.TimeoutError.
    """

    name: str
    handler: Handler
    calls: int = field(default=0)

    async def start(self, request: dict[str, Any]) -> AsyncIterator[str]:
        self.calls += 1
        result = await self.handler(request)
        return _iterate_handler_result(result)


def _extract_delta_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


async def _iterate_sse(response: httpx.Response) -> AsyncIterator[str]:
    try:
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith(SSE_DATA_PREFIX):
                continue
            data = line[len(SSE_DATA_PREFIX):].strip()
            if data == SSE_DONE_TOKEN:
                break
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            content = _extract_delta_content(payload) if isinstance(payload, dict) else ""
            if content:
                yield content
    finally:
        await response.aclose()


@dataclass
class HttpUpstream:
    """OpenAI-compatible chat completions provider over httpx."""

    name: str
    base_url: str
    api_key: str
    client: httpx.AsyncClient
    default_model: str = settings.default_model_name

    async def start(self, request: dict[str, Any]) -> AsyncIterator[str]:
        body = dict(request)
        body["stream"] = True
        body.setdefault("model", self.default_model)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH
        req = self.client.build_request("POST", url, json=body, headers=headers)
        try:
            response = await self.client.send(req, stream=True)
        except httpx.TimeoutException as exc:
            raise UpstreamError(KIND_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(KIND_NETWORK) from exc
        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            await response.aclose()
            raise UpstreamError(KIND_RATE_LIMITED, response.status_code)
        if response.status_code >= HTTP_SERVER_ERROR_FLOOR:
            await response.aclose()
            raise UpstreamError(KIND_SERVER_ERROR, response.status_code)
        if response.status_code >= 400:
            await response.aclose()
            raise UpstreamError(KIND_NETWORK, response.status_code)
        return _iterate_sse(response)


def _classify_exception(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, UpstreamError):
        return exc.kind, exc.status
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return KIND_TIMEOUT, None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == HTTP_TOO_MANY_REQUESTS:
            return KIND_RATE_LIMITED, status
        if status >= HTTP_SERVER_ERROR_FLOOR:
            return KIND_SERVER_ERROR, status
        return KIND_NETWORK, status
    return KIND_NETWORK, None


class ModelFailoverRouter:
    """Try the primary provider, then the secondary, under a shared timeout."""

    def __init__(
        self,
        primary: UpstreamProvider,
        secondary: UpstreamProvider,
        timeout_ms: int | None = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.timeout_ms = int(settings.upstream_timeout_ms if timeout_ms is None else timeout_ms)
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")

    @property
    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0

    async def complete(self, request: dict[str, Any]) -> CompletionStream:
        """Return the first provider stream that starts successfully."""
        failures: list[UpstreamFailure] = []
        for label, provider in ((PROVIDER_PRIMARY, self.primary), (PROVIDER_SECONDARY, self.secondary)):
            try:
                deltas = await asyncio.wait_for(provider.start(request), timeout=self.timeout_s)
            except Exception as exc:
                kind, status = _classify_exception(exc)
                failures.append(UpstreamFailure(provider=label, kind=kind, status=status))
                logger.warning(
                    "event=upstream_failover provider=%s kind=%s status=%s", label, kind, status
                )
                continue
            return CompletionStream(provider=label, deltas=deltas)
        raise GatewayUpstreamFailure(failures)


def build_default_router(client: httpx.AsyncClient | None = None) -> ModelFailoverRouter:
    """Construct the production router from externalized settings."""
    http_client = client or httpx.AsyncClient(timeout=settings.upstream_timeout_ms / 1000.0)
    primary = HttpUpstream(
        name=PROVIDER_PRIMARY,
        base_url=settings.primary_model_base_url,
        api_key=settings.primary_model_api_key,
        client=http_client,
    )
    secondary = HttpUpstream(
        name=PROVIDER_SECONDARY,
        base_url=settings.secondary_model_base_url,
        api_key=settings.secondary_model_api_key,
        client=http_client,
    )
    return ModelFailoverRouter(primary, secondary)
