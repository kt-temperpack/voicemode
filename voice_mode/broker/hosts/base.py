"""Stable contract between the broker kernel and a conversational host."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ..types import (
    HostCapability,
    HostDisposition,
    HostErrorKind,
    HostEvent,
    HostProbe,
    HostRecoveryEvidence,
    HostThreadSummary,
    HostTurn,
)

HostEventSink = Callable[[HostEvent], None]
Unsubscribe = Callable[[], None]


class HostAdapterError(RuntimeError):
    """Bounded, classified failure from a host adapter."""

    def __init__(
        self,
        kind: HostErrorKind,
        operation: str,
        message: str,
        *,
        retryable: bool | None = None,
    ) -> None:
        self.kind = kind
        self.operation = operation
        self.public_message = message[:1000]
        self.retryable = (
            kind is HostErrorKind.RETRYABLE_TRANSPORT if retryable is None else retryable
        )
        super().__init__(self.public_message)


def require_capability(probe: HostProbe, capability: HostCapability) -> None:
    """Fail before I/O when an adapter cannot perform an operation."""

    if not probe.available:
        raise HostAdapterError(
            HostErrorKind.UNAVAILABLE,
            capability.value,
            probe.reason or f"host adapter {probe.adapter} is unavailable",
        )
    if capability not in probe.capabilities:
        raise HostAdapterError(
            HostErrorKind.UNSUPPORTED,
            capability.value,
            f"host adapter {probe.adapter} does not support {capability.value}",
        )


class HostAdapter(ABC):
    """Host-independent thread and turn API used by the broker."""

    @abstractmethod
    def probe(self) -> HostProbe:
        """Report live availability and supported operations."""

    @abstractmethod
    def list_threads(self, repo_root: str | None = None) -> tuple[HostThreadSummary, ...]:
        """Return stable thread summaries, optionally scoped to a repository."""

    @abstractmethod
    def read_thread(self, thread_id: str) -> HostThreadSummary:
        """Read one thread without attaching it."""

    @abstractmethod
    def attach_thread(self, thread_id: str) -> HostThreadSummary:
        """Select an existing thread as the destination for later turns."""

    @abstractmethod
    def create_thread(self, repo_root: str, label: str) -> HostThreadSummary:
        """Create and attach a broker-owned thread."""

    @abstractmethod
    def start_turn(self, *, request_id: str, thread_id: str, prompt: str) -> HostTurn:
        """Start one host turn correlated to the broker request ID."""

    @abstractmethod
    def steer_turn(
        self,
        *,
        request_id: str,
        thread_id: str,
        host_turn_id: str,
        prompt: str,
    ) -> HostTurn:
        """Steer an active turn while preserving request correlation."""

    @abstractmethod
    def interrupt_turn(
        self,
        *,
        request_id: str,
        thread_id: str,
        host_turn_id: str,
    ) -> HostTurn:
        """Interrupt one correlated active turn."""

    @abstractmethod
    def subscribe(self, sink: HostEventSink) -> Unsubscribe:
        """Subscribe to normalized host lifecycle events."""

    @abstractmethod
    def query_disposition(self, *, request_id: str, thread_id: str) -> HostDisposition:
        """Classify host evidence for recovery without redispatching."""

    def recover_request(
        self,
        *,
        request_id: str,
        thread_id: str,
    ) -> HostRecoveryEvidence:
        """Map bounded disposition evidence into a typed recovery result."""

        disposition = self.query_disposition(request_id=request_id, thread_id=thread_id)
        rationale = {
            HostDisposition.ABSENT: "the host has no evidence for the broker request ID",
            HostDisposition.IN_PROGRESS: "the host still shows the broker request in progress",
            HostDisposition.COMPLETED: (
                "the host reports the broker request completed without a canonical response payload"
            ),
            HostDisposition.CANCELLED: "the host has terminal cancellation evidence",
            HostDisposition.UNCERTAIN: "the host evidence is present but not safe to classify",
        }[disposition]
        return HostRecoveryEvidence(disposition, rationale)

    @abstractmethod
    def close(self) -> None:
        """Release adapter resources idempotently."""
