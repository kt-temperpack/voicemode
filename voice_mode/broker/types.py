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
