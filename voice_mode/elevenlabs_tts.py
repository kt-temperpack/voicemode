"""ElevenLabs TTS provider for voice-mode.

Provides two entry points against the ElevenLabs HTTP API:

* :func:`synthesize` — POST to ``/v1/text-to-speech/{voice_id}`` and return
  the full raw PCM bytes for the buffered playback path.
* :func:`stream` — async generator over
  ``/v1/text-to-speech/{voice_id}/stream`` that yields raw PCM ``int16``
  chunks as ElevenLabs produces them, for low-latency playback.

Both paths request ``pcm_24000`` (raw s16le 24 kHz mono) and try
``config.ELEVENLABS_MODEL`` first, falling back to
``config.ELEVENLABS_FALLBACK_MODEL`` if ElevenLabs rejects the model id.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, List, Optional, Tuple

import httpx

from . import config

logger = logging.getLogger("voicemode")

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"


class ElevenLabsError(RuntimeError):
    """Raised when ElevenLabs returns a non-2xx response we cannot recover from."""


def _normalize_base_url(base_url: Optional[str]) -> str:
    """Return the API root, tolerating a trailing ``/`` or ``/v1`` suffix."""
    root = (base_url or ELEVENLABS_BASE_URL).rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def _resolve_request(
    voice_id: Optional[str],
    model: Optional[str],
) -> Tuple[str, str, str, List[str]]:
    """Return ``(api_key, voice_id, primary_model, models_to_try)``.

    Raises :class:`ElevenLabsError` if the API key or voice id is missing.
    """
    api_key = config.ELEVENLABS_API_KEY
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is not set")

    voice = voice_id or config.ELEVENLABS_VOICE_ID
    if not voice:
        raise ElevenLabsError(
            "VOICEMODE_ELEVENLABS_VOICE_ID is not set. "
            "Pick a voice id from https://elevenlabs.io/app/voice-library and export it."
        )

    primary = model or config.ELEVENLABS_MODEL
    fallback = config.ELEVENLABS_FALLBACK_MODEL
    models_to_try = [primary, fallback] if primary != fallback else [primary]
    return api_key, voice, primary, models_to_try


def _build_body(
    model_id: str,
    text: str,
    speed: Optional[float],
) -> dict:
    body = {
        "text": text,
        "model_id": model_id,
    }
    if speed is not None:
        body["voice_settings"] = {"speed": speed}
    return body


def _is_model_error(status_code: int, body_text: str) -> bool:
    return status_code in (400, 404) and "model" in body_text.lower()


async def synthesize(
    text: str,
    voice_id: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    sample_rate: int = 24000,
    speed: Optional[float] = None,
) -> bytes:
    """Synthesize ``text`` via ElevenLabs and return raw PCM s16le bytes.

    Tries ``config.ELEVENLABS_MODEL`` first; on a 4xx that mentions the model,
    retries with ``config.ELEVENLABS_FALLBACK_MODEL``.
    """
    api_key, voice, _, models_to_try = _resolve_request(voice_id, model)
    url = f"{_normalize_base_url(base_url)}/v1/text-to-speech/{voice}"

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    params = {"output_format": f"pcm_{sample_rate}"}
    fallback = models_to_try[-1]

    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt_model in models_to_try:
            logger.debug(f"ElevenLabs TTS request: model={attempt_model} voice={voice}")
            resp = await client.post(
                url,
                headers=headers,
                params=params,
                json=_build_body(attempt_model, text, speed),
            )
            if resp.status_code == 200:
                return resp.content

            body_text = resp.text[:500]
            if (
                _is_model_error(resp.status_code, body_text)
                and attempt_model != fallback
            ):
                logger.warning(
                    f"ElevenLabs rejected model {attempt_model} "
                    f"({resp.status_code}); retrying with {fallback}"
                )
                continue
            raise ElevenLabsError(f"ElevenLabs TTS failed: {resp.status_code} {body_text}")

    raise ElevenLabsError("ElevenLabs TTS exhausted both primary and fallback models")


async def stream(
    text: str,
    voice_id: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    sample_rate: int = 24000,
    speed: Optional[float] = None,
) -> AsyncIterator[bytes]:
    """Stream raw PCM s16le bytes from ElevenLabs' stream endpoint.

    Yields chunks as they arrive so the caller can play them progressively.
    On a model-related 4xx, retries the request with the fallback model.
    """
    api_key, voice, _, models_to_try = _resolve_request(voice_id, model)
    url = f"{_normalize_base_url(base_url)}/v1/text-to-speech/{voice}/stream"

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    params = {"output_format": f"pcm_{sample_rate}"}
    fallback = models_to_try[-1]

    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt_model in models_to_try:
            logger.debug(f"ElevenLabs stream request: model={attempt_model} voice={voice}")
            try:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    params=params,
                    json=_build_body(attempt_model, text, speed),
                ) as resp:
                    if resp.status_code != 200:
                        body_text = (await resp.aread()).decode(
                            "utf-8", errors="replace"
                        )[:500]
                        if (
                            _is_model_error(resp.status_code, body_text)
                            and attempt_model != fallback
                        ):
                            logger.warning(
                                f"ElevenLabs rejected model {attempt_model} "
                                f"({resp.status_code}); retrying with {fallback}"
                            )
                            continue
                        raise ElevenLabsError(
                            f"ElevenLabs stream failed: {resp.status_code} {body_text}"
                        )

                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
                    return
            except ElevenLabsError:
                raise
            except httpx.HTTPError as e:
                raise ElevenLabsError(f"ElevenLabs stream transport error: {e}") from e

    raise ElevenLabsError("ElevenLabs stream exhausted both primary and fallback models")
