"""Host-independent activation events and their deterministic reducer."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable


class ActivationKind(str, Enum):
    WAKE = "wake"
    PUSH_TO_TALK_PRESS = "push_to_talk_press"
    PUSH_TO_TALK_RELEASE = "push_to_talk_release"
    TOGGLE = "toggle"
    SLEEP = "sleep"
    INTERRUPT = "interrupt"


@dataclass(frozen=True)
class ActivationEvent:
    kind: ActivationKind
    source: str
    timestamp: float

    @classmethod
    def now(cls, kind: ActivationKind, source: str) -> "ActivationEvent":
        return cls(kind, source, time.monotonic())


@dataclass(frozen=True)
class ActivationState:
    direct_capture: bool = False
    push_to_talk_held: bool = False
    toggle_active: bool = False
    endpoint_requested: bool = False
    sleep_requested: bool = False
    interrupt_requested: bool = False


def reduce_activation(
    state: ActivationState, event: ActivationEvent
) -> ActivationState:
    """Apply one adapter event without touching audio, Codex, or presentation."""
    if event.kind is ActivationKind.WAKE:
        return replace(state, direct_capture=True)
    if event.kind is ActivationKind.PUSH_TO_TALK_PRESS:
        return replace(
            state,
            direct_capture=True,
            push_to_talk_held=True,
            endpoint_requested=False,
        )
    if event.kind is ActivationKind.PUSH_TO_TALK_RELEASE:
        return replace(
            state,
            push_to_talk_held=False,
            endpoint_requested=True,
        )
    if event.kind is ActivationKind.TOGGLE:
        active = not state.toggle_active
        return replace(
            state,
            direct_capture=state.direct_capture or active,
            toggle_active=active,
            endpoint_requested=not active,
        )
    if event.kind is ActivationKind.SLEEP:
        return replace(state, sleep_requested=True, endpoint_requested=True)
    if event.kind is ActivationKind.INTERRUPT:
        return replace(state, interrupt_requested=True)
    raise AssertionError(f"unsupported activation event: {event.kind}")


class ActivationBus:
    """Thread-safe fanout that keeps adapters outside conversation ownership."""

    def __init__(self) -> None:
        self._queue: queue.Queue[ActivationEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[ActivationEvent], None]] = []

    def publish(self, event: ActivationEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        if not subscribers:
            self._queue.put(event)
        for subscriber in subscribers:
            subscriber(event)

    def subscribe(
        self, subscriber: Callable[[ActivationEvent], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def drain(self) -> list[ActivationEvent]:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events
