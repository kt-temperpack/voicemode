import pytest

from voice_mode.broker.audio import VoiceAudio, _voice_text


def test_voice_text_extracts_minimal_and_summary_results():
    assert _voice_text("Voice response: hello") == "hello"
    assert _voice_text("Voice response: hello | Timing: total 1.0s") == "hello"
    assert _voice_text("No speech detected") is None


@pytest.mark.asyncio
async def test_audio_modes_reuse_converse_contract():
    calls = []

    async def converse(**kwargs):
        calls.append(kwargs)
        return "Voice response: captured"

    audio = VoiceAudio(voice="am_michael", listen_duration=20, min_duration=1, converse_callable=converse)
    assert await audio.listen() == "captured"
    await audio.speak("answer")
    assert await audio.exchange("answer") == "captured"
    assert calls[0]["skip_tts"] is True and calls[0]["wait_for_response"] is True
    assert calls[1]["wait_for_response"] is False
    assert calls[2]["wait_for_response"] is True
    assert all(call["voice"] == "am_michael" and call["skip_conch"] is True for call in calls)
