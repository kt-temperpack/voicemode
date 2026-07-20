"""Stable types shared by the local conversation broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class BrokerPhase(str, Enum):
    ASLEEP = "asleep"
    ENGAGED = "engaged"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class BrokerEvent(str, Enum):
    OPEN = "open"
    ACTIVATE = "activate"
    LISTEN_STARTED = "listen_started"
    UTTERANCE_ENQUEUED = "utterance_enqueued"
    UTTERANCE_DELIVERED = "utterance_delivered"
    SUMMARY_ACCEPTED = "summary_accepted"
    PLAYBACK_FINISHED = "playback_finished"
    BARGE_IN = "barge_in"
    FOLLOWUP_EXPIRED = "followup_expired"
    CLOSE = "close"
    FAULT = "fault"
    RESET = "reset"


class TurnState(str, Enum):
    """Host-independent lifecycle of one accepted utterance."""

    IDLE = "idle"
    CAPTURING = "capturing"
    ACCEPTED = "accepted"
    DISPATCH_REQUESTED = "dispatch_requested"
    DISPATCHED = "dispatched"
    HOST_COMPLETED = "host_completed"
    CANCELLED = "cancelled"
    RECOVERY_UNCERTAIN = "recovery_uncertain"


class PresentationState(str, Enum):
    """At-most-once presentation lifecycle for a canonical response."""

    NONE = "none"
    READY = "ready"
    VISIBLE = "visible"
    TTS_STARTED = "tts_started"
    COMPLETE = "complete"
    TTS_FAILED = "tts_failed"


class TurnEventKind(str, Enum):
    CAPTURE_STARTED = "capture_started"
    TRANSCRIPT_ACCEPTED = "transcript_accepted"
    DISPATCH_REQUESTED = "dispatch_requested"
    DISPATCH_CONFIRMED = "dispatch_confirmed"
    HOST_COMPLETED = "host_completed"
    VISIBLE_PRESENTED = "visible_presented"
    TTS_STARTED = "tts_started"
    TTS_COMPLETED = "tts_completed"
    TTS_FAILED = "tts_failed"
    CANCELLED = "cancelled"
    RECOVERY_UNCERTAIN = "recovery_uncertain"


class TurnIntent(str, Enum):
    """Named I/O which the reducer authorizes but never performs."""

    DISPATCH_HOST = "dispatch_host"
    HANDLE_CONTROL = "handle_control"
    PRESENT_VISIBLE = "present_visible"
    START_TTS = "start_tts"


class ResultKind(str, Enum):
    STATUS = "status"
    SESSION = "session"
    UTTERANCE = "utterance"
    IDLE = "idle"
    CLOSED = "closed"
    STOPPING = "stopping"


class BrokerErrorCode(str, Enum):
    INVALID_JSON = "invalid_json"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN_OPERATION = "unknown_operation"
    SESSION_BUSY = "session_busy"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_MISMATCH = "session_mismatch"
    QUEUE_FULL = "queue_full"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"
    SHUTTING_DOWN = "shutting_down"


class BrokerError(Exception):
    """A failure safe to map to the closed broker protocol error set."""

    def __init__(
        self,
        code: BrokerErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.public_message = message[:500]
        self.retryable = retryable
        super().__init__(self.public_message)


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    codex_session_id: str
    repo_root: str
    opened_at: datetime
    opened_monotonic: float


@dataclass(frozen=True)
class PendingUtterance:
    utterance_id: str
    text: str
    captured_at: datetime


@dataclass(frozen=True)
class TurnEnvelope:
    schema_version: int
    utterance_id: str
    request_id: str | None
    broker_session_id: str
    repo_root: str
    host_adapter: str
    host_thread_id: str | None
    state: TurnState
    transcript: str | None
    control_intent: str | None
    accepted_at: datetime | None


@dataclass(frozen=True)
class CanonicalResponse:
    schema_version: int
    request_id: str
    thread_id: str
    display_text: str
    spoken_text: str
    host_turn_id: str
    completed_at: datetime


@dataclass(frozen=True)
class TurnEvent:
    kind: TurnEventKind
    envelope: TurnEnvelope | None = None
    response: CanonicalResponse | None = None


@dataclass(frozen=True)
class TurnProjection:
    """Complete deterministic kernel state for the current turn."""

    envelope: TurnEnvelope | None = None
    response: CanonicalResponse | None = None
    presentation: PresentationState = PresentationState.NONE

    @property
    def state(self) -> TurnState:
        return self.envelope.state if self.envelope is not None else TurnState.IDLE


@dataclass(frozen=True)
class TurnReduction:
    projection: TurnProjection
    intents: tuple[TurnIntent, ...] = ()


@dataclass(frozen=True)
class BrokerSnapshot:
    phase: BrokerPhase
    session: SessionInfo | None
    pending_turns: int
    uptime_seconds: float
    shutting_down: bool
    session_age_seconds: float | None


@dataclass(frozen=True)
class BrokerCapabilities:
    protocol_version: int = 1
    pending_turn_limit: int = 1
    audio_enabled: bool = False
