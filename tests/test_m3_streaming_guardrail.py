"""Milestone 3 verification for gateway/model_plane/streaming_guardrail.py.

Interface: pytest module. Exercises single-chunk redaction, adversarial
split-chunk boundaries, SSE framing, and the bounded low-latency holdback
invariants of the streaming PII guardrail.
Dependencies: pytest, pytest-asyncio (asyncio_mode=auto).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import json
from collections.abc import AsyncIterator, Iterable

import pytest

from gateway.core.config import settings
from gateway.model_plane.streaming_guardrail import (
    DEFAULT_MAX_HOLDBACK_CHARS,
    REDACTION_TOKEN,
    resolve_max_holdback_chars,
    SSE_DONE_FRAME,
    StreamingPiiGuardrail,
    guard_stream,
    guard_text_stream,
    redact,
    sse_frame,
)

EMAIL = "jane.doe+billing@example-corp.co.uk"
SSN = "123-45-6789"
CARD_SPACES = "4111 1111 1111 1111"
CARD_HYPHENS = "4111-1111-1111-1111"
CARD_PLAIN = "4111111111111111"
SAFE_PREFIX = "Hello, your account summary is ready. "
LARGE_CHUNK_CHARS = 4096
LARGE_CHUNK_COUNT = 8
RFC5321_MAX_EMAIL_CHARS = 254
LONG_LOCAL_PART = "a" * 60
LONG_SUBDOMAIN = "s" * 135
LONG_EMAIL = f"{LONG_LOCAL_PART}@{LONG_SUBDOMAIN}.example.com"


async def _aiter(chunks: Iterable[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


async def _collect_text(chunks: Iterable[str]) -> str:
    return "".join([piece async for piece in guard_text_stream(_aiter(chunks))])


def _drain(chunks: Iterable[str]) -> list[str]:
    guard = StreamingPiiGuardrail()
    emitted = [guard.feed(chunk) for chunk in chunks]
    emitted.append(guard.flush())
    return emitted


@pytest.mark.parametrize("pii", [EMAIL, SSN, CARD_SPACES, CARD_HYPHENS, CARD_PLAIN])
def test_single_chunk_redaction(pii: str) -> None:
    text = f"Record: {pii}. End."
    assert redact(text) == f"Record: {REDACTION_TOKEN}. End."
    assert "".join(_drain([text])) == f"Record: {REDACTION_TOKEN}. End."


def test_single_chunk_multiple_pii_types() -> None:
    text = f"email {EMAIL} ssn {SSN} card {CARD_SPACES} done"
    expected = f"email {REDACTION_TOKEN} ssn {REDACTION_TOKEN} card {REDACTION_TOKEN} done"
    assert "".join(_drain([text])) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Order 12345 ships in 3-5 days.",
        "Call 555-1234 or visit shop.example today.",
        "Reference 1234-5678 is a short code, not a card.",
        "Version 1.2.3 released",
    ],
)
def test_non_pii_passes_through_unchanged(text: str) -> None:
    assert "".join(_drain([text])) == text


def test_split_ssn_across_two_chunks() -> None:
    emitted = _drain(["SSN is 123-45-", "6789 on file."])
    assert "".join(emitted) == f"SSN is {REDACTION_TOKEN} on file."
    assert emitted[0] == "SSN is "


def test_split_email_across_three_chunks() -> None:
    emitted = _drain(["Contact jane.doe", "+billing@example", "-corp.co.uk for help."])
    assert "".join(emitted) == f"Contact {REDACTION_TOKEN} for help."
    assert emitted[0] == "Contact "
    assert emitted[1] == ""


def test_split_card_mid_digits_with_spaces() -> None:
    emitted = _drain(["Card: 4111 11", "11 1111 ", "1111 expires soon."])
    assert "".join(emitted) == f"Card: {REDACTION_TOKEN} expires soon."
    assert emitted[0] == "Card: "


def test_split_card_mid_digits_with_hyphens() -> None:
    emitted = _drain(["4111-1111-11", "11-1", "111\n"])
    assert "".join(emitted) == f"{REDACTION_TOKEN}\n"


def test_pii_split_one_character_per_chunk() -> None:
    text = f"x {SSN} y {EMAIL} z"
    emitted = _drain(list(text))
    assert "".join(emitted) == f"x {REDACTION_TOKEN} y {REDACTION_TOKEN} z"


def test_pii_at_stream_end_is_redacted_on_flush() -> None:
    guard = StreamingPiiGuardrail()
    assert guard.feed("Final card ") == "Final card "
    assert guard.feed(CARD_PLAIN) == ""
    assert guard.flush() == REDACTION_TOKEN
    assert guard.holdback_len == 0


def test_safe_prefix_flushes_before_stream_completes() -> None:
    guard = StreamingPiiGuardrail()
    first = guard.feed(SAFE_PREFIX)
    assert first == SAFE_PREFIX
    assert guard.holdback_len == 0
    second = guard.feed("Your SSN 123-45-")
    assert second == "Your SSN "
    assert guard.holdback_len == len("123-45-")
    third = guard.feed("6789 is confirmed")
    assert third == f"{REDACTION_TOKEN} is "
    assert guard.flush() == "confirmed"


def test_default_cap_is_256_from_settings() -> None:
    assert DEFAULT_MAX_HOLDBACK_CHARS == 256
    assert resolve_max_holdback_chars() == settings.stream_guardrail_max_holdback_chars == 256
    assert resolve_max_holdback_chars(7) == 7
    assert StreamingPiiGuardrail().max_holdback_chars == 256
    assert DEFAULT_MAX_HOLDBACK_CHARS > RFC5321_MAX_EMAIL_CHARS


def test_long_rfc5321_email_over_128_chars_redacted_across_chunks() -> None:
    assert 128 < len(LONG_EMAIL) <= RFC5321_MAX_EMAIL_CHARS
    text = f"Contact: {LONG_EMAIL} for details."
    chunks = [text[:40], text[40:150], text[150:]]
    assert "".join(chunks) == text
    emitted = _drain(chunks)
    output = "".join(emitted)
    assert output == f"Contact: {REDACTION_TOKEN} for details."
    assert "@" not in output
    assert LONG_LOCAL_PART[:16] not in output
    assert "example.com" not in output
    assert emitted[0] == "Contact: "


def test_holdback_is_bounded_on_long_token_runs() -> None:
    guard = StreamingPiiGuardrail()
    total_in = 0
    total_out = 0
    for _ in range(LARGE_CHUNK_COUNT):
        chunk = "a" * LARGE_CHUNK_CHARS
        total_in += len(chunk)
        total_out += len(guard.feed(chunk))
        assert guard.holdback_len <= DEFAULT_MAX_HOLDBACK_CHARS
    total_out += len(guard.flush())
    assert total_out == total_in
    assert total_in > DEFAULT_MAX_HOLDBACK_CHARS * 4


def test_custom_holdback_limit_and_validation() -> None:
    guard = StreamingPiiGuardrail(max_holdback_chars=4)
    guard.feed("abcdefgh")
    assert guard.holdback_len <= 4
    with pytest.raises(ValueError):
        StreamingPiiGuardrail(max_holdback_chars=0)


async def test_guard_text_stream_async_redaction() -> None:
    result = await _collect_text(["Hi ", "jane@exa", "mple.com and ", SSN[:6], SSN[6:], "."])
    assert result == f"Hi {REDACTION_TOKEN} and {REDACTION_TOKEN}."


async def test_guard_stream_emits_sse_frames_and_done() -> None:
    frames = [frame async for frame in guard_stream(_aiter(["Hello ", "world. ", "4111 1111 ", "1111 1111", " bye"]))]
    assert frames[-1] == SSE_DONE_FRAME
    assert frames[0] == sse_frame("Hello ")
    contents = []
    for frame in frames[:-1]:
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        payload = json.loads(frame[len("data: "):])
        contents.append(payload["choices"][0]["delta"]["content"])
    assert "".join(contents) == f"Hello world. {REDACTION_TOKEN} bye"
    assert all(content for content in contents)


async def test_guard_stream_empty_input_yields_only_done() -> None:
    frames = [frame async for frame in guard_stream(_aiter([]))]
    assert frames == [SSE_DONE_FRAME]


async def test_first_frame_latency_on_safe_text() -> None:
    stream = guard_stream(_aiter([SAFE_PREFIX, "pending", " tail"]))
    first = await stream.__anext__()
    assert first == sse_frame(SAFE_PREFIX)
    rest = [frame async for frame in stream]
    assert rest == [sse_frame("pending "), sse_frame("tail"), SSE_DONE_FRAME]


if __name__ == "__main__":
    from tests.conftest import run_module_tests

    raise SystemExit(run_module_tests(__file__))
