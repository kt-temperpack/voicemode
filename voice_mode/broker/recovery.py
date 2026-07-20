"""Bounded, evidence-driven host reconnection without automatic redispatch."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .hosts.app_server import AppServerHostAdapter
from .journal import JournalEvent, TurnJournal
from .runtime import BrokerRuntime
from .types import HostCompletion, HostDisposition


class RecoveryAction(str, Enum):
    PRESENT = "present"
    RETRY_ALLOWED = "retry_allowed"
    WAIT = "wait"
    CANCELLED = "cancelled"
    MANUAL = "manual"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True)
class RecoveryDecision:
    request_id: str
    action: RecoveryAction
    rationale: str
    attempt: int


@dataclass(frozen=True)
class RecoveryStatus:
    reconnecting: bool
    circuit_open: bool
    attempts: int
    rationale: str | None


AdapterFactory = Callable[[], AppServerHostAdapter]
Sleeper = Callable[[float], None]
Jitter = Callable[[float], float]


class RecoveryCoordinator:
    """Reconnect an app-server adapter and classify one correlated request."""

    def __init__(
        self,
        runtime: BrokerRuntime,
        journal: TurnJournal,
        adapter_factory: AdapterFactory,
        *,
        max_attempts: int = 4,
        base_delay: float = 0.25,
        max_delay: float = 4.0,
        sleeper: Sleeper = time.sleep,
        jitter: Jitter | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.runtime = runtime
        self.journal = journal
        self.adapter_factory = adapter_factory
        self.max_attempts = max_attempts
        self.base_delay = max(0.0, base_delay)
        self.max_delay = max(self.base_delay, max_delay)
        self.sleeper = sleeper
        self.jitter = jitter or (lambda delay: random.uniform(0.0, delay * 0.2))
        self.adapter: AppServerHostAdapter | None = None
        self._lock = threading.Lock()
        self._reconnecting = False
        self._circuit_open = False
        self._attempts = 0
        self._rationale: str | None = None

    def status(self) -> RecoveryStatus:
        with self._lock:
            return RecoveryStatus(
                self._reconnecting,
                self._circuit_open,
                self._attempts,
                self._rationale,
            )

    def recover(
        self,
        *,
        request_id: str,
        thread_id: str,
        dispatch_confirmed: bool,
    ) -> RecoveryDecision:
        with self._lock:
            if self._circuit_open:
                return RecoveryDecision(
                    request_id,
                    RecoveryAction.CIRCUIT_OPEN,
                    self._rationale or "recovery circuit is open",
                    self._attempts,
                )
            if self._reconnecting:
                return RecoveryDecision(
                    request_id,
                    RecoveryAction.WAIT,
                    "host recovery is already in progress",
                    self._attempts,
                )
            self._reconnecting = True
        self.runtime.freeze_dispatch("Codex app-server connection was lost")
        previous = self.adapter
        self.adapter = None
        if previous is not None:
            previous.close()
        try:
            for attempt in range(1, self.max_attempts + 1):
                with self._lock:
                    self._attempts = attempt
                self._record("reconnect_attempt", request_id, attempt=attempt)
                adapter = None
                try:
                    adapter = self.adapter_factory()
                    adapter.attach_thread(thread_id)
                    evidence = adapter.recover_request(
                        request_id=request_id,
                        thread_id=thread_id,
                    )
                except Exception as error:
                    if adapter is not None:
                        adapter.close()
                    rationale = str(error)[:500]
                    self._record(
                        "reconnect_failed",
                        request_id,
                        reason=rationale,
                        attempt=attempt,
                    )
                    if attempt < self.max_attempts:
                        delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
                        self.sleeper(
                            min(
                                self.max_delay,
                                delay + max(0.0, self.jitter(delay)),
                            )
                        )
                        continue
                    return self._open_circuit(request_id, rationale, attempt)
                self.adapter = adapter
                decision = self._decide(
                    request_id,
                    evidence.disposition,
                    evidence.rationale,
                    evidence.completion,
                    dispatch_confirmed,
                    attempt,
                )
                self.runtime.resume_dispatch()
                return decision
            raise AssertionError("bounded recovery loop did not return")
        finally:
            with self._lock:
                self._reconnecting = False

    def _decide(
        self,
        request_id: str,
        disposition: HostDisposition,
        rationale: str,
        completion: HostCompletion | None,
        dispatch_confirmed: bool,
        attempt: int,
    ) -> RecoveryDecision:
        if disposition is HostDisposition.COMPLETED and completion is not None:
            restored = self.runtime.restore_host_completion(
                completion.canonical_response()
            )
            action = RecoveryAction.PRESENT if restored else RecoveryAction.MANUAL
            if not restored:
                rationale = f"{rationale}; local turn identity could not be restored"
        elif disposition is HostDisposition.ABSENT and not dispatch_confirmed:
            action = RecoveryAction.RETRY_ALLOWED
        elif disposition is HostDisposition.CANCELLED:
            action = RecoveryAction.CANCELLED
        elif disposition is HostDisposition.IN_PROGRESS:
            action = RecoveryAction.WAIT
        else:
            action = RecoveryAction.MANUAL
            self.runtime.mark_dispatch_uncertain(request_id)
        with self._lock:
            self._rationale = rationale
        self._record(
            "recovery_decision",
            request_id,
            decision=action.value,
            reason=rationale,
            attempt=attempt,
        )
        return RecoveryDecision(request_id, action, rationale, attempt)

    def _open_circuit(
        self,
        request_id: str,
        rationale: str,
        attempt: int,
    ) -> RecoveryDecision:
        rationale = f"recovery circuit opened after {attempt} failed attempts: {rationale}"
        with self._lock:
            self._circuit_open = True
            self._rationale = rationale
        self._record(
            "recovery_circuit_open",
            request_id,
            decision=RecoveryAction.CIRCUIT_OPEN.value,
            reason=rationale,
            attempt=attempt,
        )
        return RecoveryDecision(
            request_id,
            RecoveryAction.CIRCUIT_OPEN,
            rationale,
            attempt,
        )

    def _record(
        self,
        event: str,
        request_id: str,
        *,
        decision: str | None = None,
        reason: str | None = None,
        attempt: int | None = None,
    ) -> None:
        self.journal.append(
            JournalEvent(
                event=event,
                request_id=request_id,
                decision=decision,
                reason=reason,
                attempt=str(attempt) if attempt is not None else None,
            )
        )
