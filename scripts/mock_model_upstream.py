"""Mock OpenAI-compatible upstream for local demos of the Model Inference Plane.

Interface: uvicorn scripts.mock_model_upstream:app --port <port>
  POST /v1/chat/completions streams a canned reply containing PII in SSE
  frames split mid-token, or returns a configured failure status.
Environment:
  MOCK_UPSTREAM_STATUS  HTTP status to return instead of streaming (e.g. 429, 503).
  MOCK_UPSTREAM_DELAY_S Seconds to sleep before answering (simulates a slow provider).
Dependencies: fastapi, uvicorn.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse

FAILURE_STATUS = int(os.environ.get("MOCK_UPSTREAM_STATUS", "0") or 0)
DELAY_S = float(os.environ.get("MOCK_UPSTREAM_DELAY_S", "0") or 0.0)
CHUNK_DELAY_S = 0.05
CANNED_DELTAS = (
    "Sure. ",
    "The customer on file is jane.",
    "doe@example",
    ".com, SSN 123-",
    "45-6789, card 4111 1111 ",
    "1111 1111. ",
    "Anything else?",
)

app = FastAPI(title="Mock Model Upstream")


async def _stream() -> AsyncIterator[str]:
    for delta in CANNED_DELTAS:
        payload = {"choices": [{"delta": {"content": delta}}]}
        yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(CHUNK_DELAY_S)
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions() -> Response:
    if DELAY_S > 0:
        await asyncio.sleep(DELAY_S)
    if FAILURE_STATUS:
        return JSONResponse(status_code=FAILURE_STATUS, content={"error": "mock upstream failure"})
    return StreamingResponse(_stream(), media_type="text/event-stream")
