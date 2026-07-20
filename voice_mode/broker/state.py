"""Pure lifecycle transitions for the conversation broker."""

from __future__ import annotations

from .types import BrokerError, BrokerErrorCode, BrokerEvent, BrokerPhase


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
