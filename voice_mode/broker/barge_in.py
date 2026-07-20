"""Capability-aware, request-correlated interruption policy."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .runtime import BrokerRuntime
from .types import BrokerPhase, HostCapability, HostProbe, TurnState


class BargeInAction(str, Enum):
    CAPTURE_ONLY = "capture_only"
    PLAYBACK_CANCELLED = "playback_cancelled"
    HOST_INTERRUPTED = "host_interrupted"
    HOST_STEERED = "host_steered"
    QUEUED_FOR_SAFE_TURN = "queued_for_safe_turn"


@dataclass(frozen=True)
class BargeInResult:
    request_id: str | None
    action: BargeInAction
    playback_cancelled: bool
    cancellation_latency_ms: float
    rationale: str


@dataclass(frozen=True)
class AcousticTopology:
    headphones: bool
    distinct_input_output: bool
    echo_score: float
    noise_floor: float


def acoustic_barge_in_supported(topology: AcousticTopology) -> bool:
    """Qualify only conservative headphone paths with low echo and noise."""
    return (
        topology.headphones
        and topology.distinct_input_output
        and topology.echo_score <= 0.15
        and topology.noise_floor <= 0.25
    )


class BargeInCoordinator:
    """Stop output first, then touch only the exact correlated host request."""

    def __init__(
        self,
        *,
        runtime: BrokerRuntime,
        audio,
        host_probe: HostProbe,
        interrupt_host: Callable[[str], None],
        steer_host: Callable[[str, str], None],
        monotonic: Callable[[], float] = time.monotonic,
        result_sink: Callable[[BargeInResult], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.audio = audio
        self.host_probe = host_probe
        self.interrupt_host = interrupt_host
        self.steer_host = steer_host
        self.monotonic = monotonic
        self.result_sink = result_sink
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending: set[str] = set()
        self._interrupted: set[str] = set()
        self._interrupting: set[str] = set()

    def cancel_audio(self, activated_at: float) -> tuple[bool, float]:
        cancelled = self.audio.cancel_playback()
        latency_ms = max(0.0, (self.monotonic() - activated_at) * 1000)
        return cancelled, latency_ms

    def activate(
        self,
        *,
        activated_at: float,
        prompt: str | None = None,
        playback_cancelled: bool | None = None,
        cancellation_latency_ms: float | None = None,
    ) -> BargeInResult:
        if playback_cancelled is None or cancellation_latency_ms is None:
            playback_cancelled, latency_ms = self.cancel_audio(activated_at)
        else:
            latency_ms = cancellation_latency_ms
        diagnostic = self.runtime.turn_diagnostic()
        request_id = diagnostic["request_id"]
        snapshot = self.runtime.snapshot()
        if snapshot.session is not None and snapshot.phase in {
            BrokerPhase.THINKING,
            BrokerPhase.SPEAKING,
        }:
            self.runtime.barge_in(snapshot.session.session_id)

        action = (
            BargeInAction.PLAYBACK_CANCELLED
            if playback_cancelled
            else BargeInAction.CAPTURE_ONLY
        )
        rationale = "no correlated host turn was active"
        if request_id is not None:
            with self._lock:
                self._pending.add(request_id)
            turn_state = TurnState(diagnostic["state"])
            try:
                if (
                    prompt
                    and turn_state is TurnState.DISPATCHED
                    and HostCapability.STEER_TURN in self.host_probe.capabilities
                ):
                    self.steer_host(request_id, prompt)
                    action = BargeInAction.HOST_STEERED
                    rationale = "the host supports steering the active request"
                elif (
                    turn_state is TurnState.DISPATCHED
                    and HostCapability.INTERRUPT_TURN in self.host_probe.capabilities
                ):
                    with self._condition:
                        self._interrupting.add(request_id)
                    self.interrupt_host(request_id)
                    self.runtime.cancel_turn(request_id)
                    with self._condition:
                        self._interrupted.add(request_id)
                        self._interrupting.discard(request_id)
                        self._condition.notify_all()
                    action = BargeInAction.HOST_INTERRUPTED
                    rationale = "the host confirmed interruption of the active request"
                else:
                    action = BargeInAction.QUEUED_FOR_SAFE_TURN
                    rationale = "the host cannot safely redirect this active request"
            except Exception as error:
                with self._condition:
                    self._interrupting.discard(request_id)
                    self._condition.notify_all()
                action = BargeInAction.QUEUED_FOR_SAFE_TURN
                rationale = f"host interruption was not confirmed: {error}"

        result = BargeInResult(
            request_id,
            action,
            playback_cancelled,
            latency_ms,
            rationale,
        )
        if self.result_sink is not None:
            self.result_sink(result)
        return result

    def redirect_pending(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._pending

    def host_was_interrupted(self, request_id: str) -> bool:
        with self._condition:
            self._condition.wait_for(
                lambda: request_id not in self._interrupting,
                timeout=1.0,
            )
            return request_id in self._interrupted

    def finish(self, request_id: str) -> None:
        with self._lock:
            self._pending.discard(request_id)
            self._interrupted.discard(request_id)
