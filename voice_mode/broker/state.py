"""Pure lifecycle transitions for the conversation broker."""

from __future__ import annotations

from .types import (
    BrokerError,
    BrokerErrorCode,
    BrokerEvent,
    BrokerPhase,
    PresentationState,
    TurnProjection,
    TurnState,
)


class InvalidTransition(BrokerError):
    def __init__(self, phase: BrokerPhase, event: BrokerEvent) -> None:
        self.phase = phase
        self.event = event
        super().__init__(
            BrokerErrorCode.INVALID_REQUEST,
            f"event {event.value} is invalid while broker is {phase.value}",
        )


_TRANSITIONS: dict[tuple[BrokerPhase, BrokerEvent], BrokerPhase] = {
    (BrokerPhase.ASLEEP, BrokerEvent.OPEN): BrokerPhase.ENGAGED,
    (BrokerPhase.ENGAGED, BrokerEvent.ACTIVATE): BrokerPhase.LISTENING,
    (BrokerPhase.ENGAGED, BrokerEvent.LISTEN_STARTED): BrokerPhase.LISTENING,
    (BrokerPhase.LISTENING, BrokerEvent.UTTERANCE_ENQUEUED): BrokerPhase.THINKING,
    (BrokerPhase.THINKING, BrokerEvent.UTTERANCE_DELIVERED): BrokerPhase.THINKING,
    (BrokerPhase.THINKING, BrokerEvent.SUMMARY_ACCEPTED): BrokerPhase.SPEAKING,
    (BrokerPhase.SPEAKING, BrokerEvent.PLAYBACK_FINISHED): BrokerPhase.ENGAGED,
    (BrokerPhase.SPEAKING, BrokerEvent.BARGE_IN): BrokerPhase.LISTENING,
    (BrokerPhase.THINKING, BrokerEvent.BARGE_IN): BrokerPhase.LISTENING,
    (BrokerPhase.ENGAGED, BrokerEvent.FOLLOWUP_EXPIRED): BrokerPhase.ASLEEP,
    (BrokerPhase.ASLEEP, BrokerEvent.RESET): BrokerPhase.ASLEEP,
}

for _phase in BrokerPhase:
    _TRANSITIONS[(_phase, BrokerEvent.FAULT)] = BrokerPhase.ASLEEP
    if _phase is not BrokerPhase.ASLEEP:
        _TRANSITIONS[(_phase, BrokerEvent.CLOSE)] = BrokerPhase.ASLEEP


def transition(phase: BrokerPhase, event: BrokerEvent) -> BrokerPhase:
    try:
        return _TRANSITIONS[(phase, event)]
    except KeyError as exc:
        raise InvalidTransition(phase, event) from exc


def project_broker_phase(session_phase: BrokerPhase, projection: TurnProjection) -> BrokerPhase:
    """Project detailed turn state onto the stable protocol-v1 phase vocabulary."""

    if session_phase is BrokerPhase.ASLEEP:
        return BrokerPhase.ASLEEP
    if projection.state is TurnState.CAPTURING:
        return BrokerPhase.LISTENING
    if projection.state in {
        TurnState.ACCEPTED,
        TurnState.DISPATCH_REQUESTED,
        TurnState.DISPATCHED,
        TurnState.RECOVERY_UNCERTAIN,
    }:
        return BrokerPhase.THINKING
    if (
        projection.state is TurnState.HOST_COMPLETED
        and projection.presentation
        in {PresentationState.READY, PresentationState.VISIBLE, PresentationState.TTS_STARTED}
    ):
        return BrokerPhase.SPEAKING
    return session_phase
