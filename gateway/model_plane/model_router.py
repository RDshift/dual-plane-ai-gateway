"""Model Inference Plane router exposing POST /v1/chat/completions.

Interface: authenticates the tenant (Bearer or X-API-Key), reserves tokens in
SqliteTokenRateLimiter, routes the completion through ModelFailoverRouter and
pipes every delta through the Milestone 3 streaming guardrail. stream=true
yields text/event-stream SSE frames; stream=false yields a redacted JSON
completion. Response headers X-Model-Provider and X-RateLimit-Remaining
expose routing and quota state.

Dependency injection: configure_model_plane(rate_limiter=..., failover_router=...)
replaces the lazily built production components; reset_model_plane() closes
and discards them. Tests use both through the conftest fixture.

Dependencies: fastapi, pydantic, gateway.core.config, gateway.core.errors,
gateway.model_plane.rate_limiter, gateway.model_plane.failover_router,
gateway.model_plane.streaming_guardrail.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gateway.core.config import settings
from gateway.core.errors import build_gateway_error
from gateway.model_plane.failover_router import (
    GatewayUpstreamFailure,
    ModelFailoverRouter,
    build_default_router,
)
from gateway.model_plane.rate_limiter import RateLimitDecision, SqliteTokenRateLimiter
from gateway.model_plane.streaming_guardrail import guard_stream, redact

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429

CHARS_PER_TOKEN = 4
PER_MESSAGE_TOKEN_OVERHEAD = 4
DEFAULT_MAX_TOKENS = 256
HEADER_PROVIDER = "X-Model-Provider"
HEADER_REMAINING = "X-RateLimit-Remaining"
HEADER_LIMIT = "X-RateLimit-Limit"
HEADER_RETRY_AFTER = "Retry-After"
HEADER_REQUEST_ID = "X-Request-Id"
SSE_MEDIA_TYPE = "text/event-stream"

HEADER_WWW_AUTHENTICATE = "WWW-Authenticate"
WWW_AUTHENTICATE_VALUE = 'Bearer realm="model-plane"'
MSG_UNAUTHORIZED = "Missing or invalid API key."
MSG_INVALID_REQUEST = "Invalid request body."
MSG_RATE_LIMITED = "Rate limit exceeded for this API key."

logger = logging.getLogger(__name__)

model_router = APIRouter(tags=["model_plane"])


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = Field(..., min_length=1)
    content: str | list[Any] | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible request envelope; unknown fields are forwarded as-is."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(..., min_length=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False
    tokens: int | None = Field(default=None, gt=0)


@dataclass
class ModelPlaneState:
    rate_limiter: SqliteTokenRateLimiter | None = None
    failover_router: ModelFailoverRouter | None = None


_state = ModelPlaneState()


def configure_model_plane(
    rate_limiter: SqliteTokenRateLimiter | None = None,
    failover_router: ModelFailoverRouter | None = None,
) -> None:
    """Inject components (tests) or leave None to build from settings lazily."""
    if rate_limiter is not None:
        _state.rate_limiter = rate_limiter
    if failover_router is not None:
        _state.failover_router = failover_router


async def reset_model_plane() -> None:
    """Close and discard injected or lazily built components."""
    limiter, _state.rate_limiter = _state.rate_limiter, None
    _state.failover_router = None
    if limiter is not None:
        await limiter.close()


async def get_rate_limiter() -> SqliteTokenRateLimiter:
    if _state.rate_limiter is None:
        _state.rate_limiter = SqliteTokenRateLimiter()
    if not _state.rate_limiter.is_open:
        await _state.rate_limiter.open()
    return _state.rate_limiter


def get_failover_router() -> ModelFailoverRouter:
    if _state.failover_router is None:
        _state.failover_router = build_default_router()
    return _state.failover_router


def estimate_tokens(req: ChatCompletionRequest) -> int:
    """Explicit tokens field wins; otherwise prompt estimate plus completion budget."""
    if req.tokens is not None:
        return req.tokens
    prompt_chars = 0
    for message in req.messages:
        if isinstance(message.content, str):
            prompt_chars += len(message.content)
        elif isinstance(message.content, list):
            for part in message.content:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str):
                    prompt_chars += len(text)
    prompt_tokens = math.ceil(prompt_chars / CHARS_PER_TOKEN) + PER_MESSAGE_TOKEN_OVERHEAD * len(req.messages)
    completion_tokens = req.max_tokens if req.max_tokens is not None else DEFAULT_MAX_TOKENS
    return max(1, prompt_tokens + completion_tokens)


def _resolve_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    return None


def _error(code: str, message: str, status: int, request_id: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=build_gateway_error(code=code, message=message, http_status=status, request_id=request_id),
        headers={HEADER_REQUEST_ID: request_id, **(headers or {})},
    )


def _quota_headers(decision: RateLimitDecision, limiter: SqliteTokenRateLimiter) -> dict[str, str]:
    return {
        HEADER_REMAINING: str(max(0, decision.remaining)),
        HEADER_LIMIT: str(limiter.tokens_per_window),
    }


@model_router.post(CHAT_COMPLETIONS_PATH)
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Response:
    """Authenticate, rate limit, fail over, and redact one chat completion."""
    request_id = uuid.uuid4().hex
    api_key = _resolve_api_key(authorization, x_api_key)
    if api_key is None:
        logger.warning("event=model_auth_failed request_id=%s", request_id)
        return _error(
            "authentication_required",
            MSG_UNAUTHORIZED,
            HTTP_UNAUTHORIZED,
            request_id,
            headers={HEADER_WWW_AUTHENTICATE: WWW_AUTHENTICATE_VALUE},
        )

    try:
        body = await request.json()
        chat_request = ChatCompletionRequest.model_validate(body)
    except (ValueError, ValidationError):
        return _error("invalid_request", MSG_INVALID_REQUEST, HTTP_BAD_REQUEST, request_id)

    limiter = await get_rate_limiter()
    requested_tokens = estimate_tokens(chat_request)
    decision = await limiter.consume(api_key, requested_tokens)
    quota_headers = _quota_headers(decision, limiter)
    if not decision.allowed:
        logger.warning("event=rate_limited request_id=%s tokens=%s", request_id, requested_tokens)
        retry_after = str(max(1, math.ceil(decision.retry_after_s)))
        return _error(
            "rate_limit_exceeded",
            MSG_RATE_LIMITED,
            HTTP_TOO_MANY_REQUESTS,
            request_id,
            headers={HEADER_RETRY_AFTER: retry_after, **quota_headers},
        )

    upstream_request = chat_request.model_dump(exclude_none=True, exclude={"tokens"})
    upstream_request.setdefault("model", settings.default_model_name)
    try:
        completion = await get_failover_router().complete(upstream_request)
    except GatewayUpstreamFailure as failure:
        logger.error(
            "event=upstream_exhausted request_id=%s failures=%s",
            request_id,
            [(f.provider, f.kind, f.status) for f in failure.failures],
        )
        return JSONResponse(
            status_code=failure.http_status,
            content=failure.to_payload(request_id),
            headers={HEADER_REQUEST_ID: request_id, **quota_headers},
        )

    headers = {HEADER_PROVIDER: completion.provider, HEADER_REQUEST_ID: request_id, **quota_headers}
    if chat_request.stream:
        return StreamingResponse(guard_stream(completion), media_type=SSE_MEDIA_TYPE, headers=headers)

    content = redact(await completion.collect())
    payload = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "model": upstream_request["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    return JSONResponse(status_code=HTTP_OK, content=payload, headers=headers)
