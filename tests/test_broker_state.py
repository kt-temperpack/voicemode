from dataclasses import FrozenInstanceError

import pytest

from voice_mode.broker import (
    BrokerCapabilities,
    BrokerErrorCode,
    BrokerEvent,
    BrokerPhase,
    InvalidTransition,
    transition,
)


ALLOWED = {
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
    **{(phase, BrokerEvent.FAULT): BrokerPhase.ASLEEP for phase in BrokerPhase},
    **{
        (phase, BrokerEvent.CLOSE): BrokerPhase.ASLEEP
        for phase in BrokerPhase
        if phase is not BrokerPhase.ASLEEP
    },
}


@pytest.mark.parametrize(("phase", "event", "expected"), [(*key, value) for key, value in ALLOWED.items()])
def test_allowed_transitions(phase, event, expected):
    assert transition(phase, event) is expected


@pytest.mark.parametrize(
    ("phase", "event"),
    [pair for pair in ((p, e) for p in BrokerPhase for e in BrokerEvent) if pair not in ALLOWED],
)
def test_all_other_transitions_are_rejected(phase, event):
    with pytest.raises(InvalidTransition) as caught:
        transition(phase, event)
    assert caught.value.code is BrokerErrorCode.INVALID_REQUEST
    assert repr(phase) not in str(caught.value)


def test_public_values_are_stable_and_records_are_frozen():
    assert [phase.value for phase in BrokerPhase] == [
        "asleep", "engaged", "listening", "thinking", "speaking"
    ]
    capabilities = BrokerCapabilities()
    with pytest.raises(FrozenInstanceError):
        capabilities.audio_enabled = True
