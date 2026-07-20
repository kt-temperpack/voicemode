"""Pure, deterministic end-of-speech decisions for streaming microphone audio."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum


class EndpointReason(str, Enum):
    SILENCE = "silence"
    LINGUISTIC_SILENCE = "linguistic_silence"
    MAX_DURATION = "max_duration"
    PUSH_TO_TALK_RELEASE = "push_to_talk_release"


@dataclass(frozen=True)
class FrameMetadata:
    duration_ms: int
    rms: float
    vad_speech: bool | None
    linguistic_complete: bool = False
    push_to_talk_released: bool = False


@dataclass(frozen=True)
class EndpointDecision:
    ended: bool
    reason: EndpointReason | None
    elapsed_ms: int
    utterance_ms: int
    trailing_silence_ms: int
    noise_floor: float
    voiced_ratio: float


class EndpointDetector:
    """Combine VAD votes, energy, duration bounds, and explicit release."""

    def __init__(
        self,
        *,
        silence_ms: int = 900,
        min_utterance_ms: int = 1000,
        max_utterance_ms: int = 30_000,
        start_vote_frames: int = 3,
        start_votes_required: int = 2,
        allowed_tail_speech_ratio: float = 0.1,
        linguistic_silence_ratio: float = 0.65,
        initial_noise_floor: float = 64.0,
        noise_alpha: float = 0.08,
        energy_multiplier: float = 2.0,
    ) -> None:
        if silence_ms <= 0 or max_utterance_ms <= 0:
            raise ValueError("endpoint durations must be positive")
        if min_utterance_ms < 0 or min_utterance_ms > max_utterance_ms:
            raise ValueError("minimum utterance duration is invalid")
        if not 0 <= allowed_tail_speech_ratio < 1:
            raise ValueError("tail speech ratio must be in [0, 1)")
        self.silence_ms = silence_ms
        self.min_utterance_ms = min_utterance_ms
        self.max_utterance_ms = max_utterance_ms
        self.start_votes_required = start_votes_required
        self.allowed_tail_speech_ratio = allowed_tail_speech_ratio
        self.linguistic_silence_ratio = linguistic_silence_ratio
        self.noise_alpha = noise_alpha
        self.energy_multiplier = energy_multiplier
        self.noise_floor = max(1.0, initial_noise_floor)
        self.elapsed_ms = 0
        self.utterance_ms = 0
        self.speech_started = False
        self._start_votes: deque[bool] = deque(maxlen=max(1, start_vote_frames))
        self._tail: deque[tuple[int, bool]] = deque()
        self._linguistic_complete = False
        self._decision: EndpointDecision | None = None

    def feed(self, frame: FrameMetadata) -> EndpointDecision:
        if self._decision is not None:
            return self._decision
        if frame.duration_ms <= 0 or frame.rms < 0:
            raise ValueError("frame duration and RMS must be valid")

        self.elapsed_ms += frame.duration_ms
        energy_speech = frame.rms >= self.noise_floor * self.energy_multiplier
        voiced = frame.vad_speech if frame.vad_speech is not None else energy_speech

        if frame.vad_speech is not True:
            self.noise_floor += self.noise_alpha * (frame.rms - self.noise_floor)
            self.noise_floor = max(1.0, self.noise_floor)

        if not self.speech_started:
            self._start_votes.append(bool(voiced))
            if sum(self._start_votes) >= self.start_votes_required:
                self.speech_started = True
            if frame.push_to_talk_released:
                return self._finish(EndpointReason.PUSH_TO_TALK_RELEASE)
            return self._snapshot()

        self.utterance_ms += frame.duration_ms
        self._linguistic_complete = (
            self._linguistic_complete or frame.linguistic_complete
        )
        self._tail.append((frame.duration_ms, bool(voiced)))
        self._trim_tail(self.silence_ms)

        if frame.push_to_talk_released:
            return self._finish(EndpointReason.PUSH_TO_TALK_RELEASE)
        if self.utterance_ms >= self.max_utterance_ms:
            return self._finish(EndpointReason.MAX_DURATION)
        if self.utterance_ms < self.min_utterance_ms:
            return self._snapshot()
        if self._mostly_silent(self.silence_ms):
            return self._finish(EndpointReason.SILENCE)
        linguistic_ms = max(300, round(self.silence_ms * self.linguistic_silence_ratio))
        if self._linguistic_complete and self._mostly_silent(linguistic_ms):
            return self._finish(EndpointReason.LINGUISTIC_SILENCE)
        return self._snapshot()

    def _trim_tail(self, retain_ms: int) -> None:
        total = sum(duration for duration, _voiced in self._tail)
        while self._tail and total - self._tail[0][0] >= retain_ms:
            duration, _voiced = self._tail.popleft()
            total -= duration

    def _mostly_silent(self, required_ms: int) -> bool:
        window: list[tuple[int, bool]] = []
        total = 0
        for duration, is_voiced in reversed(self._tail):
            window.append((duration, is_voiced))
            total += duration
            if total >= required_ms:
                break
        if total < required_ms:
            return False
        voiced = sum(duration for duration, is_voiced in window if is_voiced)
        return voiced / total <= self.allowed_tail_speech_ratio

    def _snapshot(self) -> EndpointDecision:
        total = sum(duration for duration, _voiced in self._tail)
        voiced = sum(duration for duration, is_voiced in self._tail if is_voiced)
        return EndpointDecision(
            False,
            None,
            self.elapsed_ms,
            self.utterance_ms,
            total - voiced,
            self.noise_floor,
            voiced / total if total else 0.0,
        )

    def _finish(self, reason: EndpointReason) -> EndpointDecision:
        snapshot = self._snapshot()
        self._decision = EndpointDecision(
            True,
            reason,
            snapshot.elapsed_ms,
            snapshot.utterance_ms,
            snapshot.trailing_silence_ms,
            snapshot.noise_floor,
            snapshot.voiced_ratio,
        )
        return self._decision
