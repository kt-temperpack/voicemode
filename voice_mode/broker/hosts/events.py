"""Normalize Codex app-server lifecycle messages into stable host events."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from ..types import (
    HostApprovalRequest,
    HostCompletion,
    HostDisposition,
    HostEvent,
    HostEventKind,
)
from .base import HostEventSink, Unsubscribe


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}
_MAX_PENDING_TURNS = 256
_MAX_PENDING_PER_TURN = 16
_MAX_APPROVAL_HISTORY = 1024


class AppServerEventMapper:
    """Correlate, deduplicate, and normalize app-server lifecycle messages."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._sinks: list[HostEventSink] = []
        self._turn_requests: dict[str, str] = {}
        self._request_turns: dict[tuple[str, str], str] = {}
        self._dispositions: dict[str, HostDisposition] = {}
        self._pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._terminal_turns: set[str] = set()
        self._approval_ids: set[str] = set()
        self._approval_order: deque[str] = deque()

    def subscribe(self, sink: HostEventSink) -> Unsubscribe:
        with self._lock:
            self._sinks.append(sink)

        def unsubscribe() -> None:
            with self._lock:
                if sink in self._sinks:
                    self._sinks.remove(sink)

        return unsubscribe

    def register_turn(self, request_id: str, thread_id: str, turn_id: str) -> None:
        with self._lock:
            self._turn_requests.setdefault(turn_id, request_id)
            self._request_turns[(request_id, thread_id)] = turn_id
            self._dispositions.setdefault(turn_id, HostDisposition.IN_PROGRESS)
            pending = tuple(self._pending.pop(turn_id, ()))
        for message in pending:
            self.consume(message)

    def disposition(self, request_id: str, thread_id: str) -> HostDisposition:
        with self._lock:
            turn_id = self._request_turns.get((request_id, thread_id))
            if turn_id is None:
                return HostDisposition.ABSENT
            return self._dispositions.get(turn_id, HostDisposition.UNCERTAIN)

    def wait_for_terminal(self, turn_id: str, timeout: float) -> HostDisposition:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._dispositions.get(turn_id)
                in {HostDisposition.COMPLETED, HostDisposition.CANCELLED},
                timeout=timeout,
            )
            if not ready:
                return HostDisposition.UNCERTAIN
            return self._dispositions[turn_id]

    def consume(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method == "voicemode/transportLost":
            reason = params.get("reason")
            self._publish(
                HostEvent(
                    HostEventKind.TRANSPORT_LOST,
                    None,
                    None,
                    error=str(reason)[:500] if reason else "app-server transport lost",
                )
            )
            return
        turn_id = self._turn_id(params)
        if method in _APPROVAL_METHODS:
            self._consume_approval(message, params, turn_id)
            return
        if method not in {"turn/started", "turn/completed"} or turn_id is None:
            return
        with self._lock:
            if turn_id not in self._turn_requests:
                self._buffer(turn_id, message)
                return
        if method == "turn/started":
            self._emit_turn_started(params, turn_id)
        else:
            self._emit_turn_completed(params, turn_id)

    @staticmethod
    def _turn_id(params: dict[str, Any]) -> str | None:
        direct = params.get("turnId")
        if isinstance(direct, str):
            return direct
        turn = params.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return turn["id"]
        return None

    def _identity(self, params: dict[str, Any], turn_id: str) -> tuple[str, str] | None:
        thread_id = params.get("threadId")
        with self._lock:
            request_id = self._turn_requests.get(turn_id)
        if not isinstance(thread_id, str) or request_id is None:
            return None
        return request_id, thread_id

    def _emit_turn_started(self, params: dict[str, Any], turn_id: str) -> None:
        identity = self._identity(params, turn_id)
        if identity is None:
            return
        request_id, thread_id = identity
        with self._lock:
            if turn_id in self._terminal_turns:
                return
            self._dispositions[turn_id] = HostDisposition.IN_PROGRESS
        self._publish(
            HostEvent(HostEventKind.TURN_STARTED, request_id, thread_id, turn_id)
        )

    def _emit_turn_completed(self, params: dict[str, Any], turn_id: str) -> None:
        identity = self._identity(params, turn_id)
        turn = params.get("turn")
        if identity is None or not isinstance(turn, dict):
            return
        request_id, thread_id = identity
        status = turn.get("status")
        with self._condition:
            if turn_id in self._terminal_turns:
                return
            self._terminal_turns.add(turn_id)
            if status == "interrupted":
                self._dispositions[turn_id] = HostDisposition.CANCELLED
            elif status in {"completed", "failed"}:
                self._dispositions[turn_id] = HostDisposition.COMPLETED
            else:
                self._dispositions[turn_id] = HostDisposition.UNCERTAIN
            self._condition.notify_all()

        if status == "interrupted":
            event = HostEvent(
                HostEventKind.TURN_CANCELLED, request_id, thread_id, turn_id
            )
        elif status == "completed":
            text = self.agent_text(turn.get("items"))
            completed_at = turn.get("completedAt")
            completed = (
                datetime.fromtimestamp(completed_at, timezone.utc)
                if isinstance(completed_at, (int, float))
                else datetime.now(timezone.utc)
            )
            completion = HostCompletion(
                request_id,
                thread_id,
                turn_id,
                text,
                text,
                completed,
            )
            event = HostEvent(
                HostEventKind.TURN_COMPLETED,
                request_id,
                thread_id,
                turn_id,
                completion=completion,
            )
        else:
            error = turn.get("error")
            event = HostEvent(
                HostEventKind.TURN_COMPLETED,
                request_id,
                thread_id,
                turn_id,
                error=self._failure_text(error),
            )
        self._publish(event)

    def _consume_approval(
        self,
        message: dict[str, Any],
        params: dict[str, Any],
        turn_id: str | None,
    ) -> None:
        if turn_id is None:
            return
        identity = self._identity(params, turn_id)
        if identity is None:
            with self._lock:
                self._buffer(turn_id, message)
            return
        request_id, thread_id = identity
        approval_id = params.get("approvalId") or message.get("id") or params.get("itemId")
        if not isinstance(approval_id, (str, int)):
            return
        approval_key = str(approval_id)
        with self._lock:
            if approval_key in self._approval_ids:
                return
            if len(self._approval_order) >= _MAX_APPROVAL_HISTORY:
                expired = self._approval_order.popleft()
                self._approval_ids.discard(expired)
            self._approval_ids.add(approval_key)
            self._approval_order.append(approval_key)
        reason = params.get("reason")
        approval = HostApprovalRequest(
            request_id,
            thread_id,
            turn_id,
            approval_key,
            str(reason) if reason else "Codex requires user approval",
        )
        self._publish(
            HostEvent(
                HostEventKind.APPROVAL_REQUIRED,
                request_id,
                thread_id,
                turn_id,
                approval=approval,
            )
        )

    def _buffer(self, turn_id: str, message: dict[str, Any]) -> None:
        if turn_id not in self._pending and len(self._pending) >= _MAX_PENDING_TURNS:
            oldest = next(iter(self._pending))
            del self._pending[oldest]
        messages = self._pending[turn_id]
        if len(messages) < _MAX_PENDING_PER_TURN:
            messages.append(message)

    @staticmethod
    def agent_text(items: Any) -> str:
        if not isinstance(items, list):
            return ""
        messages = [
            item.get("text")
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
            and item.get("text")
        ]
        return messages[-1] if messages else ""

    @staticmethod
    def _failure_text(error: Any) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:1000]
        return "Codex turn failed"

    def _publish(self, event: HostEvent) -> None:
        with self._lock:
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink(event)
            except Exception:
                continue
