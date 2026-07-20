import numpy as np
import pytest

from voice_mode.broker import audio as audio_module
from voice_mode.broker.audio import PersistentVoiceAudio, _clean_transcript


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.starts = 0
        self.stops = 0
        self.closes = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


def test_blank_audio_markers_are_not_turns():
    assert _clean_transcript("[BLANK_AUDIO]") is None
    assert _clean_transcript("  [silence]  ") is None
    assert _clean_transcript("actual request") == "actual request"


@pytest.mark.asyncio
async def test_stream_stays_open_across_speak_and_listen(monkeypatch):
    streams = []
    spoken = []
    transcribed = []

    async def cue():
        return True

    def stream_factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    async def speak(message, voice):
        spoken.append((message, voice))

    async def transcribe(audio):
        transcribed.append(audio)
        return "captured"

    audio = PersistentVoiceAudio(
        voice="am_michael",
        listen_duration=20,
        min_duration=1,
        stream_factory=stream_factory,
        speak_callable=speak,
        transcribe_callable=transcribe,
        listening_cue_callable=cue,
        submitted_cue_callable=cue,
    )
    monkeypatch.setattr(audio, "_capture_utterance", lambda: np.array([1, 2], dtype=np.int16))

    audio.start()
    await audio.speak("answer")
    assert await audio.listen() == "captured"
    assert await audio.exchange("follow up") == "captured"
    audio.close()

    assert len(streams) == 1
    assert streams[0].starts == streams[0].stops == streams[0].closes == 1
    assert spoken == [("answer", "am_michael"), ("follow up", "am_michael")]
    assert len(transcribed) == 2


@pytest.mark.asyncio
async def test_listening_and_submitted_cues_bracket_capture(monkeypatch):
    events = []

    async def listening_cue():
        events.append("listening")
        return True

    async def submitted_cue():
        events.append("submitted")
        return True

    async def transcribe(_audio):
        events.append("transcribed")
        return "captured"

    audio = PersistentVoiceAudio(
        voice="am_michael",
        listen_duration=20,
        min_duration=1,
        stream_factory=lambda **_kwargs: FakeStream(),
        listening_cue_callable=listening_cue,
        submitted_cue_callable=submitted_cue,
        transcribe_callable=transcribe,
    )

    def capture():
        assert audio._muted.is_set()
        events.append("captured")
        return np.array([1, 2], dtype=np.int16)

    monkeypatch.setattr(audio, "_capture_utterance", capture)

    assert await audio.listen() == "captured"
    assert events == ["listening", "captured", "transcribed", "submitted"]
    audio.close()


@pytest.mark.asyncio
async def test_no_submitted_cue_when_nothing_was_heard(monkeypatch):
    events = []

    async def listening_cue():
        events.append("listening")
        return True

    async def submitted_cue():
        events.append("submitted")
        return True

    audio = PersistentVoiceAudio(
        voice="am_michael",
        listen_duration=20,
        min_duration=1,
        stream_factory=lambda **_kwargs: FakeStream(),
        listening_cue_callable=listening_cue,
        submitted_cue_callable=submitted_cue,
    )
    monkeypatch.setattr(audio, "_capture_utterance", lambda: None)

    assert await audio.listen() is None
    assert events == ["listening"]
    assert await audio.listen() is None
    assert events == ["listening"]
    audio.close()


@pytest.mark.asyncio
async def test_blank_transcript_does_not_rearm_or_play_submitted_cue(monkeypatch):
    events = []

    async def listening_cue():
        events.append("listening")
        return True

    async def submitted_cue():
        events.append("submitted")
        return True

    async def transcribe(_audio):
        events.append("blank")
        return None

    audio = PersistentVoiceAudio(
        voice="am_michael",
        listen_duration=20,
        min_duration=1,
        stream_factory=lambda **_kwargs: FakeStream(),
        listening_cue_callable=listening_cue,
        submitted_cue_callable=submitted_cue,
        transcribe_callable=transcribe,
    )
    monkeypatch.setattr(audio, "_capture_utterance", lambda: np.array([1, 2], dtype=np.int16))

    assert await audio.listen() is None
    assert await audio.listen() is None
    assert events == ["listening", "blank", "blank"]
    audio.close()


def test_callback_discards_audio_while_muted():
    audio = PersistentVoiceAudio(voice="am_michael", listen_duration=20, min_duration=1)
    chunk = np.ones((10, 1), dtype=np.int16)
    audio._callback(chunk, 10, None, None)
    assert audio._queue.empty()
    audio._muted.clear()
    audio._callback(chunk, 10, None, None)
    assert np.array_equal(audio._queue.get_nowait(), chunk)


@pytest.mark.asyncio
async def test_handsfree_transcription_refuses_cloud_only_configuration(monkeypatch):
    monkeypatch.setattr(audio_module, "STT_BASE_URLS", ["https://api.openai.com/v1"])
    with pytest.raises(RuntimeError, match="local STT endpoint"):
        await audio_module._transcribe_local(np.ones(2400, dtype=np.int16))
