"""Streaming PII guardrail for the Model Inference Plane.

Interface: StreamingPiiGuardrail.feed(delta) returns text that is safe to emit
now, flush() returns whatever was held back at end of stream. guard_stream()
wraps any async iterable of text deltas and yields OpenAI-style SSE frames with
emails, US SSNs and 16-digit card numbers replaced by REDACTION_TOKEN.

Boundary handling: only the trailing run of characters that could still be the
prefix of an incomplete match is held back, so split-token PII is caught
without buffering the response and safe text is forwarded as soon as it
arrives.

Qualified invariant: no match whose length is <= max_holdback_chars can
straddle the flush boundary. The default cap is 256 characters, taken from
settings.stream_guardrail_max_holdback_chars, which covers the RFC 5321
maximum email length of 254 characters as well as every SSN and card form.
Above max_holdback_chars contiguous token characters the oldest prefix is
released FIFO, guaranteeing O(max_holdback_chars) memory per stream.

Dependencies: standard library, gateway.core.config.
"""

import json
import re
from collections.abc import AsyncIterable, AsyncIterator

from gateway.core.config import settings

REDACTION_TOKEN = "[REDACTED]"
DEFAULT_MAX_HOLDBACK_CHARS = 256
SSE_DONE_FRAME = "data: [DONE]\n\n"

EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
SSN_PATTERN = r"(?<![0-9-])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9-])"
CARD_PATTERN = r"(?<![0-9])(?:[0-9]{4}[ -]?){3}[0-9]{4}(?![0-9])"
PII_PATTERN = re.compile(f"(?:{CARD_PATTERN})|(?:{SSN_PATTERN})|(?:{EMAIL_PATTERN})")

TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+@-")
DIGITS = frozenset("0123456789")
CARD_SEPARATOR = " "


def redact(text: str) -> str:
    """Replace every complete PII match in text with REDACTION_TOKEN."""
    return PII_PATTERN.sub(REDACTION_TOKEN, text)


def sse_frame(content: str) -> str:
    """Serialize one content delta as an OpenAI-compatible SSE data frame."""
    payload = {"choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _unsafe_tail_length(text: str, limit: int) -> int:
    """Length of the maximal suffix that could be a prefix of an incomplete match.

    A character belongs to the suffix if it is a PII token character, or if it
    is a single space immediately preceded by a digit (card group separator).
    Scanning stops at limit so the holdback window stays bounded.
    """
    length = 0
    index = len(text) - 1
    while index >= 0 and length < limit:
        char = text[index]
        if char in TOKEN_CHARS:
            length += 1
        elif char == CARD_SEPARATOR and index > 0 and text[index - 1] in DIGITS:
            length += 1
        else:
            break
        index -= 1
    return length


def resolve_max_holdback_chars(explicit: int | None = None) -> int:
    """Explicit argument wins; otherwise the configured cap; otherwise the module default."""
    if explicit is not None:
        return int(explicit)
    configured = getattr(settings, "stream_guardrail_max_holdback_chars", None)
    return int(configured) if configured else DEFAULT_MAX_HOLDBACK_CHARS


class StreamingPiiGuardrail:
    """Incremental redactor with a bounded rolling-tail holdback window.

    Any PII match of length <= max_holdback_chars is redacted exactly even when
    split across chunks; longer token runs are released FIFO from the front.
    """

    def __init__(self, max_holdback_chars: int | None = None) -> None:
        resolved = resolve_max_holdback_chars(max_holdback_chars)
        if resolved <= 0:
            raise ValueError("max_holdback_chars must be positive")
        self.max_holdback_chars = resolved
        self._pending = ""

    @property
    def holdback_len(self) -> int:
        """Characters currently held back awaiting more context."""
        return len(self._pending)

    def feed(self, delta: str) -> str:
        """Absorb one delta and return the redacted text that is safe to emit now."""
        if not delta:
            return ""
        window = self._pending + delta
        tail_len = _unsafe_tail_length(window, self.max_holdback_chars)
        split = len(window) - tail_len
        safe, self._pending = window[:split], window[split:]
        return redact(safe)

    def flush(self) -> str:
        """End of stream: redact and release everything still held back."""
        remaining, self._pending = self._pending, ""
        return redact(remaining)


async def guard_text_stream(
    deltas: AsyncIterable[str],
    max_holdback_chars: int | None = None,
) -> AsyncIterator[str]:
    """Yield redacted plain-text chunks as soon as each becomes safe."""
    guard = StreamingPiiGuardrail(max_holdback_chars)
    async for delta in deltas:
        safe = guard.feed(delta)
        if safe:
            yield safe
    tail = guard.flush()
    if tail:
        yield tail


async def guard_stream(
    deltas: AsyncIterable[str],
    max_holdback_chars: int | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for redacted content, terminated by the [DONE] frame."""
    async for chunk in guard_text_stream(deltas, max_holdback_chars):
        yield sse_frame(chunk)
    yield SSE_DONE_FRAME
