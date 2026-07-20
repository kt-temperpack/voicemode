"""Persistent local audio for the foreground broker loop."""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import time
from collections import deque
from typing import Awaitable, Callable

import numpy as np
from openai import AsyncOpenAI
from scipy import signal
from scipy.io.wavfile import write

from voice_mode.config import (
    AUDIO_FEEDBACK_ENABLED,
    CHANNELS,
    OPENAI_API_KEY,
    SAMPLE_RATE,
    STT_BASE_URLS,
    VAD_AGGRESSIVENESS,
    VAD_CHUNK_DURATION_MS,
)
from voice_mode.provider_discovery import is_local_provider

from .endpointing import EndpointDecision, EndpointDetector, FrameMetadata


SpeakCallable = Callable[[str, str, float], Awaitable[None]]
TranscribeCallable = Callable[[np.ndarray], Awaitable[str | None]]
CueCallable = Callable[[], Awaitable[bool]]
_EMPTY_TRANSCRIPTS = {"[blank_audio]", "[blank audio]", "[silence]", "[no speech]"}


def _speech_tail_is_silent(frames: deque[bool], required_frames: int) -> bool:
    """Treat a mostly-silent tail as an endpoint despite isolated VAD noise."""
    if len(frames) < required_frames:
        return False
    allowed_speech_frames = max(1, required_frames // 10)
    return sum(frames) <= allowed_speech_frames


def _clean_transcript(text: str) -> str | None:
    cleaned = text.strip()
    if not cleaned or cleaned.casefold() in _EMPTY_TRANSCRIPTS:
        return None
    return cleaned


async def _speak_local(message: str, voice: str, speed: float = 1.25) -> None:
    from voice_mode.audio_player import NonBlockingAudioPlayer
    from voice_mode.tools.converse import synthesize_turn_with_failover

    success, samples, sample_rate, _metrics, _config = await synthesize_turn_with_failover(
        message,
        voice=voice,
        speed=speed,
    )
    if not success or samples is None or sample_rate is None:
        raise RuntimeError("local text-to-speech failed")
    player = NonBlockingAudioPlayer()
    await asyncio.to_thread(player.play, samples, sample_rate, blocking=True)


async def _transcribe_local(audio: np.ndarray) -> str | None:
    """Transcribe only through a configured local endpoint.

    Wake-listener audio must never fall through to a cloud provider. Normal
    one-shot VoiceMode calls retain their configured failover behavior.
    """
    endpoint = next((url for url in STT_BASE_URLS if is_local_provider(url)), None)
    if endpoint is None:
        raise RuntimeError("hands-free mode requires a local STT endpoint")
    wav = io.BytesIO()
    write(wav, SAMPLE_RATE, audio.astype(np.int16, copy=False))
    wav.seek(0)
    client = AsyncOpenAI(
        api_key=OPENAI_API_KEY or "dummy-key-for-local",
        base_url=endpoint,
        timeout=60.0,
        max_retries=0,
    )
    transcription = await client.audio.transcriptions.create(
        model="whisper-1",
        file=("speech.wav", wav.getvalue(), "audio/wav"),
        response_format="text",
        language="auto",
    )
    text = transcription if isinstance(transcription, str) else transcription.text
    return _clean_transcript(text)


async def _play_submitted_cue() -> bool:
    from voice_mode.core import play_chime_end

    return await play_chime_end(leading_silence=0, trailing_silence=0)


async def _play_listening_cue() -> bool:
    from voice_mode.core import play_chime_start

    return await play_chime_start(leading_silence=0, trailing_silence=0)


class PersistentVoiceAudio:
    """Keep one CoreAudio input stream open and segment utterances with VAD."""

    def __init__(
        self,
        *,
        voice: str,
        listen_duration: float,
        min_duration: float,
        speed: float = 1.25,
        stream_factory=None,
        vad_factory=None,
        speak_callable: SpeakCallable = _speak_local,
        transcribe_callable: TranscribeCallable = _transcribe_local,
        listening_cue_callable: CueCallable = _play_listening_cue,
        submitted_cue_callable: CueCallable = _play_submitted_cue,
        cues_enabled: bool = AUDIO_FEEDBACK_ENABLED,
        silence_threshold_ms: int = 900,
        endpoint_sink: Callable[[EndpointDecision], None] | None = None,
    ) -> None:
        self.voice = voice
        self.listen_duration = listen_duration
        self.min_duration = min_duration
        self.speed = speed
        self._stream_factory = stream_factory
        self._vad_factory = vad_factory
        self._speak_callable = speak_callable
        self._transcribe_callable = transcribe_callable
        self._listening_cue_callable = listening_cue_callable
        self._submitted_cue_callable = submitted_cue_callable
        self._cues_enabled = cues_enabled
        self._silence_threshold_ms = silence_threshold_ms
        self._endpoint_sink = endpoint_sink
        self.last_endpoint: EndpointDecision | None = None
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._muted = threading.Event()
        self._muted.set()
        self._stream = None
        self._chunk_samples = int(SAMPLE_RATE * VAD_CHUNK_DURATION_MS / 1000)

    def _callback(self, indata, _frames, _time_info, _status) -> None:
        if not self._muted.is_set():
            self._queue.put(indata.copy())

    def start(self) -> None:
        if self._stream is not None:
            return
        if self._stream_factory is None:
            import sounddevice as sd

            self._stream_factory = sd.InputStream
        self._stream = self._stream_factory(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=np.int16,
            callback=self._callback,
            blocksize=self._chunk_samples,
        )
        self._stream.start()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self._muted.set()
        if stream is not None:
            stream.stop()
            stream.close()
        self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _vad(self):
        if self._vad_factory is None:
            import webrtcvad

            self._vad_factory = webrtcvad.Vad
        return self._vad_factory(VAD_AGGRESSIVENESS)

    def _capture_utterance(self) -> np.ndarray | None:
        self.start()
        self._drain()
        vad = self._vad()
        pre_roll = deque(maxlen=max(1, int(500 / VAD_CHUNK_DURATION_MS)))
        chunks: list[np.ndarray] = []
        speech_started = False
        detector = EndpointDetector(
            silence_ms=self._silence_threshold_ms,
            min_utterance_ms=round(self.min_duration * 1000),
            max_utterance_ms=round(self.listen_duration * 1000),
        )
        self.last_endpoint = None
        deadline = time.monotonic() + self.listen_duration
        self._muted.clear()
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = self._queue.get(timeout=0.25).flatten()
                except queue.Empty:
                    continue
                vad_samples = int(16000 * VAD_CHUNK_DURATION_MS / 1000)
                resampled = signal.resample(chunk, vad_samples).astype(np.int16)
                try:
                    is_speech = vad.is_speech(resampled.tobytes(), 16000)
                except Exception:
                    is_speech = None
                rms = float(np.sqrt(np.mean(np.square(chunk.astype(np.float64)))))
                decision = detector.feed(
                    FrameMetadata(
                        duration_ms=VAD_CHUNK_DURATION_MS,
                        rms=rms,
                        vad_speech=is_speech,
                    )
                )

                if not speech_started:
                    pre_roll.append(chunk)
                    if not detector.speech_started:
                        continue
                    speech_started = True
                    chunks.extend(pre_roll)
                    continue

                chunks.append(chunk)
                if decision.ended:
                    self.last_endpoint = decision
                    if self._endpoint_sink is not None:
                        self._endpoint_sink(decision)
                    break
        finally:
            self._muted.set()
            self._drain()
        if not speech_started or not chunks:
            return None
        return np.concatenate(chunks)

    async def listen(self) -> str | None:
        self.start()
        self._muted.set()
        self._drain()
        audio = await asyncio.to_thread(self._capture_utterance)
        if audio is None:
            return None
        transcript = await self._transcribe_callable(audio)
        if not transcript:
            return None
        return transcript

    async def cue_listening(self) -> None:
        if self._cues_enabled:
            self._muted.set()
            self._drain()
            await self._listening_cue_callable()
            self._drain()

    async def cue_submitted(self) -> None:
        if self._cues_enabled:
            self._muted.set()
            self._drain()
            await self._submitted_cue_callable()
            self._drain()

    async def speak(self, message: str) -> None:
        self.start()
        self._muted.set()
        self._drain()
        await self._speak_callable(message, self.voice, self.speed)
        self._drain()

    async def exchange(self, message: str) -> str | None:
        await self.speak(message)
        return await self.listen()
