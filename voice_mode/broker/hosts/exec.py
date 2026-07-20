"""Explicitly limited host adapter for the separate-process Codex fallback."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from ..codex import CodexAdapter
from ..types import (
    HostCapability,
    HostCompletion,
    HostDisposition,
    HostErrorKind,
    HostEvent,
    HostEventKind,
    HostProbe,
    HostThreadSummary,
    HostTurn,
    HostTurnState,
)
from .base import HostAdapter, HostAdapterError, HostEventSink, Unsubscribe


_CAPABILITIES = frozenset(
    {
        HostCapability.ATTACH_THREAD,
        HostCapability.START_TURN,
        HostCapability.INTERRUPT_TURN,
        HostCapability.SUBSCRIBE_EVENTS,
        HostCapability.QUERY_DISPOSITION,
    }
)


class ExecCodexAdapter(HostAdapter):
    """Run schema-constrained Codex children with honest fallback semantics."""

    def __init__(self, codex: CodexAdapter) -> None:
        self.codex = codex
        self._lock = threading.RLock()
        self._sinks: list[HostEventSink] = []
        self._dispositions: dict[tuple[str, str], HostDisposition] = {}
        self._active: tuple[str, str, str] | None = None
        self._worker: threading.Thread | None = None
        self._closed = False

    def probe(self) -> HostProbe:
        return HostProbe(
            "exec",
            not self._closed,
            _CAPABILITIES if not self._closed else frozenset(),
            reason=(
                "fallback runs a separate Codex child; active-session attach and steering "
                "are unavailable"
            ),
        )

    def _unsupported(self, capability: HostCapability):
        raise HostAdapterError(
            HostErrorKind.UNSUPPORTED,
            capability.value,
            f"exec fallback does not support {capability.value}; use app-server for live-session control",
        )

    def list_threads(self, repo_root=None):
        self._unsupported(HostCapability.LIST_THREADS)

    def read_thread(self, thread_id):
        self._unsupported(HostCapability.READ_THREAD)

    def create_thread(self, repo_root, label):
        self._unsupported(HostCapability.CREATE_THREAD)

    def attach_thread(self, thread_id: str) -> HostThreadSummary:
        if not thread_id:
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                "attach_thread",
                "exec fallback requires a complete Codex thread ID",
            )
        self.codex.thread_id = thread_id
        return HostThreadSummary(thread_id, str(self.codex.repo_root), active=False)

    def start_turn(self, *, request_id: str, thread_id: str, prompt: str) -> HostTurn:
        if not prompt.strip():
            raise HostAdapterError(HostErrorKind.HOST_REJECTION, "start_turn", "prompt is empty")
        with self._lock:
            if self._closed:
                raise HostAdapterError(HostErrorKind.UNAVAILABLE, "start_turn", "exec fallback is closed")
            if self._active is not None:
                raise HostAdapterError(
                    HostErrorKind.HOST_REJECTION,
                    "start_turn",
                    "exec fallback already has an active Codex child",
                )
            if self.codex.thread_id is not None and self.codex.thread_id != thread_id:
                raise HostAdapterError(
                    HostErrorKind.AMBIGUOUS,
                    "start_turn",
                    "requested thread differs from the attached exec thread",
                )
            if thread_id != "exec:new-separate-thread":
                self.codex.thread_id = thread_id
            host_turn_id = f"exec-{request_id}"
            self._active = (request_id, thread_id, host_turn_id)
            self._dispositions[(request_id, thread_id)] = HostDisposition.IN_PROGRESS
            worker = threading.Thread(
                target=self._run,
                args=(request_id, thread_id, host_turn_id, prompt),
                name="voicemode-exec-codex",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        return HostTurn(request_id, thread_id, host_turn_id, HostTurnState.STARTED)

    def _run(self, request_id, thread_id, host_turn_id, prompt):
        try:
            turn = self.codex.run_turn(prompt, request_id=request_id)
        except Exception as error:
            event = HostEvent(
                HostEventKind.TURN_COMPLETED,
                request_id,
                thread_id,
                host_turn_id,
                error=str(error)[:1000],
            )
            disposition = HostDisposition.UNCERTAIN
        else:
            completion = HostCompletion(
                request_id,
                turn.thread_id,
                host_turn_id,
                turn.display_text,
                turn.spoken_summary,
                turn.completed_at or datetime.now(timezone.utc),
            )
            event = HostEvent(
                HostEventKind.TURN_COMPLETED,
                request_id,
                turn.thread_id,
                host_turn_id,
                completion=completion,
            )
            disposition = HostDisposition.COMPLETED
        with self._lock:
            interrupted = (
                self._dispositions.get((request_id, thread_id))
                is HostDisposition.CANCELLED
            )
            self._active = None
            if not interrupted:
                self._dispositions[(request_id, thread_id)] = disposition
        if interrupted:
            event = HostEvent(
                HostEventKind.TURN_CANCELLED,
                request_id,
                thread_id,
                host_turn_id,
            )
        self._publish(event)

    def steer_turn(self, **_kwargs):
        self._unsupported(HostCapability.STEER_TURN)

    def interrupt_turn(self, *, request_id, thread_id, host_turn_id):
        with self._lock:
            if self._active != (request_id, thread_id, host_turn_id):
                raise HostAdapterError(
                    HostErrorKind.HOST_REJECTION,
                    "interrupt_turn",
                    "exec fallback has no matching active turn",
                )
            self._dispositions[(request_id, thread_id)] = HostDisposition.CANCELLED
        if not self.codex.cancel_active():
            with self._lock:
                self._dispositions[(request_id, thread_id)] = (
                    HostDisposition.IN_PROGRESS
                )
            raise HostAdapterError(
                HostErrorKind.AMBIGUOUS,
                "interrupt_turn",
                "exec fallback could not confirm process-group termination",
            )
        return HostTurn(request_id, thread_id, host_turn_id, HostTurnState.CANCELLED)

    def subscribe(self, sink: HostEventSink) -> Unsubscribe:
        with self._lock:
            self._sinks.append(sink)

        def unsubscribe():
            with self._lock:
                if sink in self._sinks:
                    self._sinks.remove(sink)

        return unsubscribe

    def _publish(self, event):
        with self._lock:
            sinks = tuple(self._sinks)
        for sink in sinks:
            sink(event)

    def query_disposition(self, *, request_id, thread_id):
        with self._lock:
            return self._dispositions.get(
                (request_id, thread_id), HostDisposition.ABSENT
            )

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = self._active is not None
        if active:
            self.codex.cancel_active()
