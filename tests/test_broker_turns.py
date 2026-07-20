from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from voice_mode.broker import (
    BrokerErrorCode,
    BrokerPhase,
    CanonicalResponse,
    InvalidTurnTransition,
    PresentationState,
    TurnEnvelope,
    TurnEvent,
    TurnEventKind,
    TurnIntent,
    TurnProjection,
    TurnState,
    project_broker_phase,
    reduce_turn,
)


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


def capture(utterance_id: str = "utterance-1") -> TurnEnvelope:
    return TurnEnvelope(
        schema_version=1,
        utterance_id=utterance_id,
        request_id=None,
        broker_session_id="session-1",
        repo_root="/synthetic/repository",
        host_adapter="fake",
        host_thread_id="thread-1",
        state=TurnState.CAPTURING,
        transcript=None,
        control_intent=None,
        accepted_at=None,
    )


def accepted(envelope: TurnEnvelope | None = None) -> TurnEnvelope:
    return replace(
        envelope or capture(),
        request_id="request-1",
        state=TurnState.ACCEPTED,
        transcript="synthetic request",
        accepted_at=NOW,
    )


def response() -> CanonicalResponse:
    return CanonicalResponse(
        schema_version=1,
        request_id="request-1",
        thread_id="thread-1",
        display_text="One canonical response.",
        spoken_text="One canonical response.",
        host_turn_id="host-turn-1",
        completed_at=NOW,
    )


def projection(
    state: TurnState,
    presentation: PresentationState = PresentationState.NONE,
) -> TurnProjection:
    if state is TurnState.IDLE:
        return TurnProjection()
    envelope = capture()
    if state is not TurnState.CAPTURING:
        envelope = replace(accepted(envelope), state=state)
    canonical = response() if state is TurnState.HOST_COMPLETED else None
    return TurnProjection(envelope, canonical, presentation)


CASES = [
    projection(TurnState.IDLE),
    projection(TurnState.CAPTURING),
    projection(TurnState.ACCEPTED),
    projection(TurnState.DISPATCH_REQUESTED),
    projection(TurnState.DISPATCHED),
    *[
        projection(TurnState.HOST_COMPLETED, presentation)
        for presentation in (
            PresentationState.READY,
            PresentationState.VISIBLE,
            PresentationState.TTS_STARTED,
            PresentationState.COMPLETE,
            PresentationState.TTS_FAILED,
        )
    ],
    projection(TurnState.CANCELLED),
    projection(TurnState.RECOVERY_UNCERTAIN),
]


def event_for(kind: TurnEventKind, current: TurnProjection) -> TurnEvent:
    if kind is TurnEventKind.CAPTURE_STARTED:
        return TurnEvent(kind, envelope=capture("utterance-next"))
    if kind is TurnEventKind.TRANSCRIPT_ACCEPTED:
        return TurnEvent(kind, envelope=accepted(current.envelope))
    if kind is TurnEventKind.HOST_COMPLETED:
        return TurnEvent(kind, response=response())
    return TurnEvent(kind)


ALLOWED = {
    (TurnState.IDLE, PresentationState.NONE, TurnEventKind.CAPTURE_STARTED),
    (TurnState.CAPTURING, PresentationState.NONE, TurnEventKind.TRANSCRIPT_ACCEPTED),
    (TurnState.CAPTURING, PresentationState.NONE, TurnEventKind.CANCELLED),
    (TurnState.ACCEPTED, PresentationState.NONE, TurnEventKind.DISPATCH_REQUESTED),
    (TurnState.ACCEPTED, PresentationState.NONE, TurnEventKind.CANCELLED),
    (TurnState.DISPATCH_REQUESTED, PresentationState.NONE, TurnEventKind.DISPATCH_CONFIRMED),
    (TurnState.DISPATCH_REQUESTED, PresentationState.NONE, TurnEventKind.CANCELLED),
    (TurnState.DISPATCH_REQUESTED, PresentationState.NONE, TurnEventKind.RECOVERY_UNCERTAIN),
    (TurnState.DISPATCHED, PresentationState.NONE, TurnEventKind.HOST_COMPLETED),
    (TurnState.DISPATCHED, PresentationState.NONE, TurnEventKind.CANCELLED),
    (TurnState.DISPATCHED, PresentationState.NONE, TurnEventKind.RECOVERY_UNCERTAIN),
    (TurnState.HOST_COMPLETED, PresentationState.READY, TurnEventKind.VISIBLE_PRESENTED),
    (TurnState.HOST_COMPLETED, PresentationState.READY, TurnEventKind.CANCELLED),
    (TurnState.HOST_COMPLETED, PresentationState.VISIBLE, TurnEventKind.TTS_STARTED),
    (TurnState.HOST_COMPLETED, PresentationState.VISIBLE, TurnEventKind.CANCELLED),
    (TurnState.HOST_COMPLETED, PresentationState.TTS_STARTED, TurnEventKind.TTS_COMPLETED),
    (TurnState.HOST_COMPLETED, PresentationState.TTS_STARTED, TurnEventKind.TTS_FAILED),
    (TurnState.HOST_COMPLETED, PresentationState.TTS_STARTED, TurnEventKind.CANCELLED),
    (TurnState.HOST_COMPLETED, PresentationState.COMPLETE, TurnEventKind.CAPTURE_STARTED),
    (TurnState.HOST_COMPLETED, PresentationState.TTS_FAILED, TurnEventKind.CAPTURE_STARTED),
    (TurnState.CANCELLED, PresentationState.NONE, TurnEventKind.CAPTURE_STARTED),
}


@pytest.mark.parametrize("current", CASES)
@pytest.mark.parametrize("kind", list(TurnEventKind))
def test_every_projection_event_pair_is_explicit(current, kind):
    key = (current.state, current.presentation, kind)
    before = current
    if key in ALLOWED:
        assert reduce_turn(current, event_for(kind, current)).projection is not current
    else:
        with pytest.raises(InvalidTurnTransition) as caught:
            reduce_turn(current, event_for(kind, current))
        assert caught.value.code is BrokerErrorCode.INVALID_REQUEST
        assert current == before


def test_happy_path_emits_each_external_effect_once():
    current = TurnProjection()
    events = [
        TurnEvent(TurnEventKind.CAPTURE_STARTED, envelope=capture()),
        TurnEvent(TurnEventKind.TRANSCRIPT_ACCEPTED, envelope=accepted()),
        TurnEvent(TurnEventKind.DISPATCH_REQUESTED),
        TurnEvent(TurnEventKind.DISPATCH_CONFIRMED),
        TurnEvent(TurnEventKind.HOST_COMPLETED, response=response()),
        TurnEvent(TurnEventKind.VISIBLE_PRESENTED),
        TurnEvent(TurnEventKind.TTS_STARTED),
        TurnEvent(TurnEventKind.TTS_COMPLETED),
    ]
    intents = []
    for event in events:
        reduction = reduce_turn(current, event)
        current = reduction.projection
        intents.extend(reduction.intents)

    assert intents == [
        TurnIntent.DISPATCH_HOST,
        TurnIntent.PRESENT_VISIBLE,
        TurnIntent.START_TTS,
    ]
    assert current.presentation is PresentationState.COMPLETE
    for duplicate in (
        TurnEvent(TurnEventKind.DISPATCH_REQUESTED),
        TurnEvent(TurnEventKind.HOST_COMPLETED, response=response()),
        TurnEvent(TurnEventKind.VISIBLE_PRESENTED),
        TurnEvent(TurnEventKind.TTS_STARTED),
    ):
        with pytest.raises(InvalidTurnTransition):
            reduce_turn(current, duplicate)


def test_silent_response_completes_without_tts_intent():
    current = projection(TurnState.DISPATCHED)
    silent = replace(response(), spoken_text="")
    current = reduce_turn(
        current, TurnEvent(TurnEventKind.HOST_COMPLETED, response=silent)
    ).projection
    reduction = reduce_turn(current, TurnEvent(TurnEventKind.VISIBLE_PRESENTED))
    assert reduction.intents == ()
    assert reduction.projection.presentation is PresentationState.COMPLETE


def test_identity_and_payload_mismatches_fail_closed():
    current = projection(TurnState.CAPTURING)
    wrong_utterance = accepted(capture("different"))
    with pytest.raises(InvalidTurnTransition):
        reduce_turn(
            current,
            TurnEvent(TurnEventKind.TRANSCRIPT_ACCEPTED, envelope=wrong_utterance),
        )
    with pytest.raises(InvalidTurnTransition):
        reduce_turn(
            current,
            TurnEvent(
                TurnEventKind.TRANSCRIPT_ACCEPTED,
                envelope=replace(accepted(), repo_root="/different/repository"),
            ),
        )

    current = projection(TurnState.DISPATCHED)
    with pytest.raises(InvalidTurnTransition):
        reduce_turn(
            current,
            TurnEvent(
                TurnEventKind.HOST_COMPLETED,
                response=replace(response(), request_id="different"),
            ),
        )


def test_control_intent_is_accepted_but_never_dispatched_to_host():
    current = projection(TurnState.CAPTURING)
    control = replace(accepted(), transcript=None, control_intent="stop")
    reduction = reduce_turn(
        current, TurnEvent(TurnEventKind.TRANSCRIPT_ACCEPTED, envelope=control)
    )
    assert reduction.intents == (TurnIntent.HANDLE_CONTROL,)
    current = reduction.projection
    with pytest.raises(InvalidTurnTransition):
        reduce_turn(current, TurnEvent(TurnEventKind.DISPATCH_REQUESTED))


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (projection(TurnState.CAPTURING), BrokerPhase.LISTENING),
        (projection(TurnState.ACCEPTED), BrokerPhase.THINKING),
        (projection(TurnState.DISPATCHED), BrokerPhase.THINKING),
        (
            projection(TurnState.HOST_COMPLETED, PresentationState.TTS_STARTED),
            BrokerPhase.SPEAKING,
        ),
        (
            projection(TurnState.HOST_COMPLETED, PresentationState.COMPLETE),
            BrokerPhase.LISTENING,
        ),
    ],
)
def test_protocol_v1_phase_projection_is_stable(current, expected):
    assert project_broker_phase(BrokerPhase.LISTENING, current) is expected
    assert project_broker_phase(BrokerPhase.ASLEEP, current) is BrokerPhase.ASLEEP


def test_kernel_records_are_frozen():
    current = projection(TurnState.ACCEPTED)
    with pytest.raises(FrozenInstanceError):
        current.envelope.request_id = "replacement"
