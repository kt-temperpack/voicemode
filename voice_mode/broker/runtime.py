"""Thread-safe single-session authority for the conversation broker."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .journal import JournalEvent, TurnJournal
from .state import transition
from .turns import reduce_turn
from .types import (
    BrokerError,
    BrokerErrorCode,
    BrokerEvent,
    BrokerPhase,
    BrokerSnapshot,
    CanonicalResponse,
    DispatchClaim,
    DispatchDisposition,
    PendingUtterance,
    PresentationState,
    SessionInfo,
    TurnEnvelope,
    TurnEvent,
    TurnEventKind,
    TurnProjection,
    TurnState,
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
        journal: TurnJournal | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._uuid_factory = uuid_factory
        self._event_sink = event_sink
        self._journal = journal
        self._started_at = monotonic()
        self._phase_changed_at = self._started_at
        self._phase = BrokerPhase.ASLEEP
        self._session: SessionInfo | None = None
        self._last_closed_session_id: str | None = None
        self._pending: PendingUtterance | None = None
        self._turn = TurnProjection()
        self._recovered_dispatches = self._recover_dispatches()
        self._dispatch_frozen_reason: str | None = None
        self._shutting_down = False

    def _recover_dispatches(self) -> dict[str, DispatchDisposition]:
        """Classify durable evidence without ever authorizing replay."""

        if self._journal is None:
            return {}
        recovered: dict[str, DispatchDisposition] = {}
        for record in self._journal.read():
            if record.request_id is None:
                continue
            if record.event == "turn_accepted":
                recovered[record.request_id] = DispatchDisposition.SAFE_TO_CANCEL
            elif record.event in {"dispatch_claimed", "dispatch_confirmed"}:
                recovered[record.request_id] = DispatchDisposition.UNCERTAIN
            elif record.event == "recovery_uncertain":
                recovered[record.request_id] = DispatchDisposition.UNCERTAIN
            elif record.event == "host_completed":
                recovered[record.request_id] = DispatchDisposition.COMPLETED
            elif record.event == "turn_cancelled":
                recovered[record.request_id] = DispatchDisposition.CANCELLED
        return recovered

    def _append_turn_event(self, event: str, envelope: TurnEnvelope) -> None:
        if self._journal is None:
            return
        self._journal.append(
            JournalEvent(
                event=event,
                request_id=envelope.request_id,
                utterance_id=envelope.utterance_id,
                broker_session_id=envelope.broker_session_id,
                repo_root=envelope.repo_root,
                adapter=envelope.host_adapter,
                codex_thread_id=envelope.host_thread_id,
                to_state=envelope.state.value,
                transcript=envelope.transcript,
            )
        )

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
            if (
                self._turn.envelope is not None
                and self._turn.envelope.broker_session_id == session_id
            ):
                self._turn = replace(
                    self._turn,
                    envelope=replace(
                        self._turn.envelope,
                        host_thread_id=codex_session_id,
                    ),
                )
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

    def accept_turn(
        self,
        session_id: str,
        text: str | None,
        *,
        host_adapter: str,
        host_thread_id: str | None,
        control_intent: str | None = None,
    ) -> TurnEnvelope:
        """Durably accept one request and assign all replay-sensitive identity."""

        transcript = text.strip() if text is not None else None
        if bool(transcript) == bool(control_intent):
            raise BrokerError(
                BrokerErrorCode.INVALID_REQUEST,
                "exactly one transcript or control intent is required",
            )
        with self._condition:
            session = self._require_session(session_id)
            if self._turn.state not in {
                TurnState.IDLE,
                TurnState.CANCELLED,
                TurnState.HOST_COMPLETED,
            }:
                raise BrokerError(
                    BrokerErrorCode.QUEUE_FULL,
                    "pending turn slot is full",
                    retryable=True,
                )
            utterance_id = str(self._uuid_factory())
            request_id = str(self._uuid_factory())
            capturing = TurnEnvelope(
                schema_version=1,
                utterance_id=utterance_id,
                request_id=None,
                broker_session_id=session_id,
                repo_root=session.repo_root,
                host_adapter=host_adapter,
                host_thread_id=host_thread_id,
                state=TurnState.CAPTURING,
                transcript=None,
                control_intent=None,
                accepted_at=None,
            )
            projection = reduce_turn(
                self._turn,
                TurnEvent(TurnEventKind.CAPTURE_STARTED, envelope=capturing),
            ).projection
            accepted = replace(
                capturing,
                request_id=request_id,
                state=TurnState.ACCEPTED,
                transcript=transcript,
                control_intent=control_intent,
                accepted_at=self._utc_now(),
            )
            projection = reduce_turn(
                projection,
                TurnEvent(TurnEventKind.TRANSCRIPT_ACCEPTED, envelope=accepted),
            ).projection
            self._append_turn_event("turn_accepted", accepted)
            self._turn = projection
            old, new, duration = self._apply(BrokerEvent.UTTERANCE_ENQUEUED)
            self._condition.notify_all()
        self._emit_transition(old, new, duration)
        self._emit(
            "BROKER_TURN_ACCEPTED",
            session_id=session_id,
            request_id=request_id,
            queue_count=1,
        )
        return accepted

    def _current_dispatch_disposition(self, request_id: str) -> DispatchDisposition:
        envelope = self._turn.envelope
        if envelope is not None and envelope.request_id == request_id:
            return {
                TurnState.ACCEPTED: DispatchDisposition.PENDING,
                TurnState.DISPATCH_REQUESTED: DispatchDisposition.CLAIMED,
                TurnState.DISPATCHED: DispatchDisposition.CONFIRMED,
                TurnState.HOST_COMPLETED: DispatchDisposition.COMPLETED,
                TurnState.CANCELLED: DispatchDisposition.CANCELLED,
                TurnState.RECOVERY_UNCERTAIN: DispatchDisposition.UNCERTAIN,
            }.get(envelope.state, DispatchDisposition.PENDING)
        try:
            return self._recovered_dispatches[request_id]
        except KeyError as error:
            raise BrokerError(
                BrokerErrorCode.INVALID_REQUEST,
                "request is not known to this broker",
            ) from error

    def dispatch_disposition(self, request_id: str) -> DispatchDisposition:
        with self._condition:
            return self._current_dispatch_disposition(request_id)

    def claim_dispatch(self, request_id: str) -> DispatchClaim:
        """Atomically grant the only host submission authorized for a request."""

        with self._condition:
            disposition = self._current_dispatch_disposition(request_id)
            envelope = self._turn.envelope
            if (
                disposition is not DispatchDisposition.PENDING
                or envelope is None
                or envelope.request_id != request_id
            ):
                return DispatchClaim(request_id, disposition, False)
            if self._dispatch_frozen_reason is not None:
                raise BrokerError(
                    BrokerErrorCode.TIMEOUT,
                    f"host dispatch is frozen: {self._dispatch_frozen_reason}",
                    retryable=True,
                )
            reduction = reduce_turn(
                self._turn,
                TurnEvent(TurnEventKind.DISPATCH_REQUESTED),
            )
            claimed = reduction.projection.envelope
            assert claimed is not None
            # This append is the dispatch boundary: it must succeed before the
            # caller receives authority to invoke the host.
            self._append_turn_event("dispatch_claimed", claimed)
            self._turn = reduction.projection
            return DispatchClaim(request_id, DispatchDisposition.CLAIMED, True)

    def freeze_dispatch(self, reason: str) -> None:
        with self._condition:
            self._dispatch_frozen_reason = reason[:500]

    def resume_dispatch(self) -> None:
        with self._condition:
            self._dispatch_frozen_reason = None

    @property
    def dispatch_frozen_reason(self) -> str | None:
        with self._condition:
            return self._dispatch_frozen_reason

    def restore_host_completion(self, response: CanonicalResponse) -> bool:
        """Restore presentable output from correlated host evidence after restart."""

        with self._condition:
            if (
                self._turn.envelope is not None
                and self._turn.envelope.request_id == response.request_id
            ):
                self.accept_host_completion(response)
                return True
            if self._journal is None:
                return False
            accepted = next(
                (
                    record
                    for record in reversed(self._journal.read())
                    if record.request_id == response.request_id
                    and record.event == "turn_accepted"
                ),
                None,
            )
            if accepted is None or not all(
                (
                    accepted.utterance_id,
                    accepted.broker_session_id,
                    accepted.repo_root,
                    accepted.adapter,
                )
            ):
                return False
            envelope = TurnEnvelope(
                schema_version=1,
                utterance_id=accepted.utterance_id,
                request_id=response.request_id,
                broker_session_id=accepted.broker_session_id,
                repo_root=accepted.repo_root,
                host_adapter=accepted.adapter,
                host_thread_id=response.thread_id,
                state=TurnState.HOST_COMPLETED,
                transcript=None,
                control_intent=None,
                accepted_at=datetime.fromisoformat(accepted.occurred_at),
            )
            self._append_turn_event("host_completed", envelope)
            self._turn = TurnProjection(
                envelope=envelope,
                response=response,
                presentation=PresentationState.READY,
            )
            self._recovered_dispatches[response.request_id] = (
                DispatchDisposition.COMPLETED
            )
            return True

    def confirm_dispatch(self, request_id: str) -> DispatchClaim:
        """Record host acceptance idempotently after a successful submission."""

        with self._condition:
            disposition = self._current_dispatch_disposition(request_id)
            if disposition is not DispatchDisposition.CLAIMED:
                return DispatchClaim(request_id, disposition, False)
            reduction = reduce_turn(
                self._turn,
                TurnEvent(TurnEventKind.DISPATCH_CONFIRMED),
            )
            confirmed = reduction.projection.envelope
            assert confirmed is not None
            self._append_turn_event("dispatch_confirmed", confirmed)
            self._turn = reduction.projection
            return DispatchClaim(request_id, DispatchDisposition.CONFIRMED, False)

    def accept_host_completion(self, response: CanonicalResponse) -> bool:
        """Register one immutable host result and reject conflicting duplicates."""

        with self._condition:
            envelope = self._turn.envelope
            if envelope is None or envelope.request_id != response.request_id:
                raise BrokerError(
                    BrokerErrorCode.INVALID_REQUEST,
                    "completion request is not the active broker turn",
                )
            if self._turn.state is TurnState.HOST_COMPLETED:
                if self._turn.response != response:
                    raise BrokerError(
                        BrokerErrorCode.INVALID_REQUEST,
                        "completion conflicts with the canonical response",
                    )
                return False
            reduction = reduce_turn(
                self._turn,
                TurnEvent(TurnEventKind.HOST_COMPLETED, response=response),
            )
            completed = reduction.projection.envelope
            assert completed is not None
            self._append_turn_event("host_completed", completed)
            self._turn = reduction.projection
            return True

    def canonical_response(self, request_id: str) -> CanonicalResponse:
        """Return the immutable response currently owned by the presenter."""

        with self._condition:
            response = self._turn.response
            if response is None or response.request_id != request_id:
                raise BrokerError(
                    BrokerErrorCode.INVALID_REQUEST,
                    "canonical response is not available for this request",
                )
            return response

    def mark_visible_presented(self, request_id: str) -> bool:
        """Claim visible output immediately before the caller writes it."""

        with self._condition:
            if self._turn.envelope is None or self._turn.envelope.request_id != request_id:
                return False
            if self._turn.presentation is not PresentationState.READY:
                return False
            reduction = reduce_turn(
                self._turn,
                TurnEvent(TurnEventKind.VISIBLE_PRESENTED),
            )
            visible = reduction.projection.envelope
            assert visible is not None
            self._append_turn_event("visible_presented", visible)
            self._turn = reduction.projection
            return True

    def mark_tts_started(self, request_id: str) -> bool:
        """Claim audio output immediately before playback begins."""

        with self._condition:
            if self._turn.envelope is None or self._turn.envelope.request_id != request_id:
                return False
            if self._turn.presentation is not PresentationState.VISIBLE:
                return False
            reduction = reduce_turn(self._turn, TurnEvent(TurnEventKind.TTS_STARTED))
            started = reduction.projection.envelope
            assert started is not None
            self._append_turn_event("tts_started", started)
            self._turn = reduction.projection
            return True

    def finish_tts(self, request_id: str, *, failed: bool = False) -> bool:
        """Record the terminal playback outcome without authorizing a retry."""

        with self._condition:
            if self._turn.envelope is None or self._turn.envelope.request_id != request_id:
                return False
            if self._turn.presentation is not PresentationState.TTS_STARTED:
                return False
            event_kind = TurnEventKind.TTS_FAILED if failed else TurnEventKind.TTS_COMPLETED
            reduction = reduce_turn(self._turn, TurnEvent(event_kind))
            finished = reduction.projection.envelope
            assert finished is not None
            self._append_turn_event(
                "tts_failed" if failed else "tts_completed",
                finished,
            )
            self._turn = reduction.projection
            return True

    def mark_dispatch_uncertain(self, request_id: str) -> bool:
        """Archive an unproven host outcome so it can never be resubmitted."""

        with self._condition:
            envelope = self._turn.envelope
            if envelope is None or envelope.request_id != request_id:
                return False
            if self._turn.state is TurnState.RECOVERY_UNCERTAIN:
                return False
            if self._turn.state not in {
                TurnState.DISPATCH_REQUESTED,
                TurnState.DISPATCHED,
            }:
                return False
            reduction = reduce_turn(
                self._turn,
                TurnEvent(TurnEventKind.RECOVERY_UNCERTAIN),
            )
            uncertain = reduction.projection.envelope
            assert uncertain is not None
            self._append_turn_event("recovery_uncertain", uncertain)
            self._recovered_dispatches[request_id] = DispatchDisposition.UNCERTAIN
            self._turn = TurnProjection()
            return True

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
