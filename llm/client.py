"""Gemini wrapper.

IMPORTANT: gemini-3.x rejects temperature / top_p / top_k — they are removed
parameters, not ignored ones. Passing them is an API error. Run-to-run stability
for the demo therefore comes from freezing v1 to disk and measuring a noise
floor (see pipelines/noise_floor.py), NOT from sampling controls.
"""
from __future__ import annotations

import time
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

import config

T = TypeVar("T", bound=BaseModel)

_client: genai.Client | None = None

# Free/low tiers throttle; a small gap between calls keeps the demo from dying
# halfway through a 3-minute run.
_MIN_INTERVAL_S = 1.0
_last_call = 0.0


def client() -> genai.Client:
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key "
                "from https://aistudio.google.com/apikey"
            )
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def _throttle() -> None:
    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - gap)
    _last_call = time.time()


def structured(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    system: str | None = None,
) -> T:
    """One call in, one parsed Pydantic object out."""
    _throttle()
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
    )
    if system:
        cfg.system_instruction = system

    resp = client().models.generate_content(
        model=model or config.GEN_MODEL,
        contents=prompt,
        config=cfg,
    )

    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    # Fall back to manual validation if the SDK didn't hydrate .parsed
    return schema.model_validate_json(resp.text)


def embed(texts: list[str], *, task_type: str) -> list[list[float]]:
    """Embed with an explicit task_type.

    gemini-embedding-001 is used rather than -2 precisely because only -001
    supports task_type. Asymmetric retrieval (RETRIEVAL_QUERY for the candidate
    rule, RETRIEVAL_DOCUMENT for stored feedback) measurably tightens the
    distance distribution, which is what makes the Chroma threshold tunable.
    """
    _throttle()
    result = client().models.embed_content(
        model=config.EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=config.EMBED_DIM,
        ),
    )
    return [e.values for e in result.embeddings]
