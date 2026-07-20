"""Thread-safe single-session authority for the conversation broker."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .state import transition
from .types import (
    BrokerError,
    BrokerErrorCode,
    BrokerEvent,
    BrokerPhase,
    BrokerSnapshot,
    PendingUtterance,
    SessionInfo,
)

EventSink = Callable[[str, dict], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BrokerRuntime:
    """Own all mutable broker state behind one condition variable."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
        uuid_factory: Callable[[], object] = uuid.uuid4,
        event_sink: EventSink | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._uuid_factory = uuid_factory
        self._event_sink = event_sink
        self._started_at = monotonic()
        self._phase_changed_at = self._started_at
        self._phase = BrokerPhase.ASLEEP
        self._session: SessionInfo | None = None
        self._last_closed_session_id: str | None = None
        self._pending: PendingUtterance | None = None
        self._shutting_down = False

    def _emit(self, name: str, **data) -> None:
        if self._event_sink is not None:
            self._event_sink(name, data)

    def _apply(self, event: BrokerEvent) -> tuple[BrokerPhase, BrokerPhase, float]:
        old = self._phase
        now = self._monotonic()
        new = transition(old, event)
        duration = max(0.0, now - self._phase_changed_at)
        self._phase = new
        self._phase_changed_at = now
        return old, new, duration

    def _emit_transition(self, old: BrokerPhase, new: BrokerPhase, duration: float) -> None:
        self._emit(
            "BROKER_PHASE_CHANGE",
            old_phase=old.value,
            new_phase=new.value,
            duration_seconds=duration,
        )

    def _require_session(self, session_id: str) -> SessionInfo:
        if self._session is None:
            raise BrokerError(BrokerErrorCode.SESSION_NOT_FOUND, "broker session is not open")
        if self._session.session_id != session_id:
            raise BrokerError(BrokerErrorCode.SESSION_MISMATCH, "broker session does not match")
        return self._session

    def open_session(self, codex_session_id: str, repo_root: str) -> SessionInfo:
        canonical_root = str(Path(repo_root).resolve(strict=False))
        with self._condition:
            if self._shutting_down:
                raise BrokerError(BrokerErrorCode.SHUTTING_DOWN, "broker is shutting down")
            if self._session is not None:
                if (
                    self._session.codex_session_id == codex_session_id
                    and self._session.repo_root == canonical_root
                ):
                    return self._session
                raise BrokerError(BrokerErrorCode.SESSION_BUSY, "another broker session is active", retryable=True)
            old, new, duration = self._apply(BrokerEvent.OPEN)
            session = SessionInfo(
                session_id=str(self._uuid_factory()),
                codex_session_id=codex_session_id,
                repo_root=canonical_root,
                opened_at=self._utc_now(),
                opened_monotonic=self._monotonic(),
            )
            self._session = session
            self._last_closed_session_id = None
        self._emit_transition(old, new, duration)
        self._emit(
            "BROKER_SESSION_OPEN",
            session_id=session.session_id,
            codex_session_prefix=session.codex_session_id[:8],
            repo_root=session.repo_root,
        )
        return session

    def _session_event(self, session_id: str, event: BrokerEvent) -> None:
        with self._condition:
            self._require_session(session_id)
            old, new, duration = self._apply(event)
            self._condition.notify_all()
        self._emit_transition(old, new, duration)

    def activate(self, session_id: str) -> None:
        self._session_event(session_id, BrokerEvent.ACTIVATE)

    def attach_codex_thread(self, session_id: str, codex_session_id: str) -> None:
        """Replace the provisional hands-free label with Codex's real thread ID."""
        with self._condition:
            session = self._require_session(session_id)
            self._session = replace(session, codex_session_id=codex_session_id)
        self._emit(
            "BROKER_CODEX_THREAD_ATTACHED",
            session_id=session_id,
            codex_session_prefix=codex_session_id[:8],
        )

    def start_listening(self, session_id: str) -> None:
        self._session_event(session_id, BrokerEvent.LISTEN_STARTED)

    def enqueue_utterance(self, session_id: str, text: str) -> PendingUtterance:
        with self._condition:
            session = self._require_session(session_id)
            if self._pending is not None:
                self._emit("BROKER_QUEUE_FULL", session_id=session_id, queue_count=1)
                raise BrokerError(BrokerErrorCode.QUEUE_FULL, "pending turn slot is full", retryable=True)
            old, new, duration = self._apply(BrokerEvent.UTTERANCE_ENQUEUED)
            utterance = PendingUtterance(str(self._uuid_factory()), text, self._utc_now())
            self._pending = utterance
            self._condition.notify_all()
        self._emit_transition(old, new, duration)
        self._emit(
            "BROKER_TURN_ENQUEUED",
            session_id=session.session_id,
            repo_root=session.repo_root,
            queue_count=1,
        )
        return utterance

    def wait_for_turn(
        self,
        session_id: str,
        wait_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> PendingUtterance | None:
        deadline = self._monotonic() + max(0.0, wait_seconds)
        with self._condition:
            while self._pending is None:
                if self._shutting_down:
                    raise BrokerError(BrokerErrorCode.SHUTTING_DOWN, "broker is shutting down")
                self._require_session(session_id)
                if cancel_event is not None and cancel_event.is_set():
                    raise BrokerError(BrokerErrorCode.TIMEOUT, "turn wait was cancelled", retryable=True)
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    self._emit("BROKER_TURN_IDLE", session_id=session_id, queue_count=0)
                    return None
                self._condition.wait(min(remaining, 0.1 if cancel_event is not None else remaining))
            self._require_session(session_id)
            utterance = self._pending
            self._pending = None
            old, new, duration = self._apply(BrokerEvent.UTTERANCE_DELIVERED)
        self._emit_transition(old, new, duration)
        self._emit("BROKER_TURN_DELIVERED", session_id=session_id, queue_count=0)
        return utterance

    def accept_summary(self, session_id: str, summary: str) -> None:
        # Content is deliberately not retained; the length is sufficient for
        # diagnostics and does not become a transcript side channel.
        self._session_event(session_id, BrokerEvent.SUMMARY_ACCEPTED)

    def finish_playback(self, session_id: str) -> None:
        self._session_event(session_id, BrokerEvent.PLAYBACK_FINISHED)

    def barge_in(self, session_id: str) -> None:
        self._session_event(session_id, BrokerEvent.BARGE_IN)

    def expire_followup(self, session_id: str) -> None:
        with self._condition:
            session = self._require_session(session_id)
            old, new, duration = self._apply(BrokerEvent.FOLLOWUP_EXPIRED)
            self._session = None
            self._last_closed_session_id = session_id
            self._pending = None
            self._condition.notify_all()
        self._emit_transition(old, new, duration)
        self._emit("BROKER_SESSION_CLOSE", session_id=session_id, repo_root=session.repo_root)

    def close_session(self, session_id: str) -> None:
        with self._condition:
            if self._session is None:
                if session_id == self._last_closed_session_id:
                    return
                raise BrokerError(BrokerErrorCode.SESSION_NOT_FOUND, "broker session is not open")
            session = self._require_session(session_id)
            old, new, duration = self._apply(BrokerEvent.CLOSE)
            self._session = None
            self._last_closed_session_id = session_id
            self._pending = None
            self._condition.notify_all()
        self._emit_transition(old, new, duration)
        self._emit("BROKER_SESSION_CLOSE", session_id=session_id, repo_root=session.repo_root)

    def begin_shutdown(self) -> None:
        with self._condition:
            if self._shutting_down:
                return
            self._shutting_down = True
            if self._phase is not BrokerPhase.ASLEEP:
                old, new, duration = self._apply(BrokerEvent.FAULT)
                self._session = None
                self._pending = None
            else:
                old = new = self._phase
                duration = 0.0
            self._condition.notify_all()
        if old is not new:
            self._emit_transition(old, new, duration)

    def snapshot(self) -> BrokerSnapshot:
        with self._condition:
            return BrokerSnapshot(
                phase=self._phase,
                session=self._session,
                pending_turns=int(self._pending is not None),
                uptime_seconds=max(0.0, self._monotonic() - self._started_at),
                shutting_down=self._shutting_down,
                session_age_seconds=(
                    max(0.0, self._monotonic() - self._session.opened_monotonic)
                    if self._session is not None
                    else None
                ),
            )
