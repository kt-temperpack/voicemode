"""Tests for the ElevenLabs TTS provider."""

import json
import os
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ELEVENLABS_API_KEY", "test-elevenlabs-key")
os.environ.setdefault("VOICEMODE_ELEVENLABS_VOICE_ID", "voice-abc")
os.environ.setdefault("VOICEMODE_ELEVENLABS_MODEL", "eleven_flash_v2_5")
os.environ.setdefault("VOICEMODE_ELEVENLABS_FALLBACK_MODEL", "eleven_multilingual_v2")

from voice_mode import elevenlabs_tts  # noqa: E402
from voice_mode import config  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "test-elevenlabs-key")
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "voice-abc")
    monkeypatch.setattr(config, "ELEVENLABS_MODEL", "eleven_flash_v2_5")
    monkeypatch.setattr(config, "ELEVENLABS_FALLBACK_MODEL", "eleven_multilingual_v2")


def _fake_client(transport):
    class FakeClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return FakeClient


@pytest.mark.asyncio
async def test_synthesize_returns_bytes_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/text-to-speech/voice-abc"
        assert request.url.params["output_format"] == "pcm_24000"
        assert request.headers["xi-api-key"] == "test-elevenlabs-key"
        body = json.loads(request.content)
        assert body["model_id"] == "eleven_flash_v2_5"
        assert body["text"] == "hello"
        return httpx.Response(200, content=b"FAKE_PCM")

    transport = httpx.MockTransport(handler)

    with patch.object(elevenlabs_tts.httpx, "AsyncClient", _fake_client(transport)):
        result = await elevenlabs_tts.synthesize("hello")

    assert result == b"FAKE_PCM"


@pytest.mark.asyncio
async def test_synthesize_tolerates_v1_suffix_in_base_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/text-to-speech/voice-abc"
        return httpx.Response(200, content=b"OK")

    transport = httpx.MockTransport(handler)

    with patch.object(elevenlabs_tts.httpx, "AsyncClient", _fake_client(transport)):
        result = await elevenlabs_tts.synthesize(
            "hello", base_url="https://api.elevenlabs.io/v1"
        )

    assert result == b"OK"


@pytest.mark.asyncio
async def test_synthesize_falls_back_on_model_error():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["model_id"])
        if body["model_id"] == "eleven_flash_v2_5":
            return httpx.Response(404, text='{"detail":"model not found"}')
        return httpx.Response(200, content=b"OK")

    transport = httpx.MockTransport(handler)

    with patch.object(elevenlabs_tts.httpx, "AsyncClient", _fake_client(transport)):
        result = await elevenlabs_tts.synthesize("hello")

    assert result == b"OK"
    assert calls == ["eleven_flash_v2_5", "eleven_multilingual_v2"]


@pytest.mark.asyncio
async def test_synthesize_raises_on_non_model_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    transport = httpx.MockTransport(handler)

    with patch.object(elevenlabs_tts.httpx, "AsyncClient", _fake_client(transport)):
        with pytest.raises(elevenlabs_tts.ElevenLabsError, match="401"):
            await elevenlabs_tts.synthesize("hello")


@pytest.mark.asyncio
async def test_synthesize_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    with pytest.raises(elevenlabs_tts.ElevenLabsError, match="ELEVENLABS_API_KEY"):
        await elevenlabs_tts.synthesize("hello")


@pytest.mark.asyncio
async def test_synthesize_requires_voice_id(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_VOICE_ID", "")
    with pytest.raises(elevenlabs_tts.ElevenLabsError, match="VOICEMODE_ELEVENLABS_VOICE_ID"):
        await elevenlabs_tts.synthesize("hello")


@pytest.mark.asyncio
async def test_stream_yields_pcm_chunks():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/text-to-speech/voice-abc/stream"
        assert request.url.params["output_format"] == "pcm_24000"
        return httpx.Response(200, content=b"\x01\x02\x03\x04\x05\x06")

    transport = httpx.MockTransport(handler)

    with patch.object(elevenlabs_tts.httpx, "AsyncClient", _fake_client(transport)):
        chunks = [c async for c in elevenlabs_tts.stream("hello")]

    assert b"".join(chunks) == b"\x01\x02\x03\x04\x05\x06"


@pytest.mark.asyncio
async def test_stream_falls_back_on_model_error():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["model_id"])
        if body["model_id"] == "eleven_flash_v2_5":
            return httpx.Response(400, text='{"detail":"unknown model"}')
        return httpx.Response(200, content=b"OK")

    transport = httpx.MockTransport(handler)

    with patch.object(elevenlabs_tts.httpx, "AsyncClient", _fake_client(transport)):
        chunks = [c async for c in elevenlabs_tts.stream("hello")]

    assert b"".join(chunks) == b"OK"
    assert calls == ["eleven_flash_v2_5", "eleven_multilingual_v2"]
