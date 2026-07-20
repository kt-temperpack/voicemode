"""Pure, host-independent reducer for one broker turn."""

from __future__ import annotations

from dataclasses import replace

from .types import (
    BrokerError,
    BrokerErrorCode,
    CanonicalResponse,
    PresentationState,
    TurnEnvelope,
    TurnEvent,
    TurnEventKind,
    TurnIntent,
    TurnProjection,
    TurnReduction,
    TurnState,
)


class InvalidTurnTransition(BrokerError):
    def __init__(
        self,
        projection: TurnProjection,
        event: TurnEvent,
        reason: str | None = None,
    ) -> None:
        self.projection = projection
        self.event = event
        detail = reason or "event is not valid for the current turn state"
        super().__init__(
            BrokerErrorCode.INVALID_REQUEST,
            (
                f"turn event {event.kind.value} is invalid while turn is "
                f"{projection.state.value}/{projection.presentation.value}: {detail}"
            ),
        )


def _reject(
    projection: TurnProjection,
    event: TurnEvent,
    reason: str | None = None,
) -> InvalidTurnTransition:
    return InvalidTurnTransition(projection, event, reason)


def _require_envelope(projection: TurnProjection, event: TurnEvent) -> TurnEnvelope:
    if projection.envelope is None:
        raise _reject(projection, event, "turn envelope is missing")
    return projection.envelope


def _validate_capture(envelope: TurnEnvelope, projection: TurnProjection, event: TurnEvent) -> None:
    if envelope.schema_version != 1:
        raise _reject(projection, event, "unsupported turn envelope schema")
    if (
        not envelope.utterance_id
        or not envelope.broker_session_id
        or not envelope.repo_root
        or not envelope.host_adapter
    ):
        raise _reject(projection, event, "capture identity is incomplete")
    if envelope.state is not TurnState.CAPTURING:
        raise _reject(projection, event, "capture envelope must be in capturing state")
    if envelope.request_id is not None or envelope.accepted_at is not None:
        raise _reject(projection, event, "capture cannot already have an accepted request")


def _validate_accepted(envelope: TurnEnvelope, projection: TurnProjection, event: TurnEvent) -> None:
    if envelope.schema_version != 1:
        raise _reject(projection, event, "unsupported turn envelope schema")
    if envelope.state is not TurnState.ACCEPTED:
        raise _reject(projection, event, "accepted envelope must be in accepted state")
    if not envelope.request_id or envelope.accepted_at is None:
        raise _reject(projection, event, "accepted request identity is incomplete")
    has_transcript = bool(envelope.transcript and envelope.transcript.strip())
    if has_transcript == bool(envelope.control_intent):
        raise _reject(projection, event, "exactly one request payload is required")


def _validate_response(
    envelope: TurnEnvelope,
    response: CanonicalResponse,
    projection: TurnProjection,
    event: TurnEvent,
) -> None:
    if response.schema_version != 1:
        raise _reject(projection, event, "unsupported canonical response schema")
    if response.request_id != envelope.request_id:
        raise _reject(projection, event, "response request does not match the turn")
    if response.thread_id != envelope.host_thread_id:
        raise _reject(projection, event, "response thread does not match the turn")
    if not response.host_turn_id or not response.display_text.strip():
        raise _reject(projection, event, "response is not presentable")
    if response.spoken_text.strip():
        display = " ".join(response.display_text.replace("`", "").split()).casefold()
        spoken = (
            " ".join(response.spoken_text.replace("`", "").split())
            .rstrip("…")
            .rstrip(".,;:")
            .casefold()
        )
        if spoken not in display:
            raise _reject(
                projection,
                event,
                "spoken excerpt is not contained in the canonical response",
            )


def reduce_turn(projection: TurnProjection, event: TurnEvent) -> TurnReduction:
    """Return the next immutable projection and its authorized I/O intents."""

    state = projection.state
    presentation = projection.presentation

    if event.kind is TurnEventKind.CAPTURE_STARTED:
        reusable = state in {TurnState.IDLE, TurnState.CANCELLED} or (
            state is TurnState.HOST_COMPLETED
            and presentation in {PresentationState.COMPLETE, PresentationState.TTS_FAILED}
        )
        if not reusable or event.envelope is None:
            raise _reject(projection, event)
        _validate_capture(event.envelope, projection, event)
        return TurnReduction(TurnProjection(envelope=event.envelope))

    envelope = _require_envelope(projection, event)

    if event.kind is TurnEventKind.TRANSCRIPT_ACCEPTED:
        if state is not TurnState.CAPTURING or event.envelope is None:
            raise _reject(projection, event)
        stable_identity = (
            "utterance_id",
            "broker_session_id",
            "repo_root",
            "host_adapter",
            "host_thread_id",
        )
        if any(
            getattr(event.envelope, field) != getattr(envelope, field)
            for field in stable_identity
        ):
            raise _reject(projection, event, "accepted transcript replaced capture identity")
        _validate_accepted(event.envelope, projection, event)
        intents = (
            (TurnIntent.HANDLE_CONTROL,) if event.envelope.control_intent is not None else ()
        )
        return TurnReduction(TurnProjection(envelope=event.envelope), intents)

    if event.kind is TurnEventKind.DISPATCH_REQUESTED:
        if state is not TurnState.ACCEPTED or envelope.control_intent is not None:
            raise _reject(projection, event)
        if not envelope.host_thread_id:
            raise _reject(projection, event, "host thread is required before dispatch")
        updated = replace(envelope, state=TurnState.DISPATCH_REQUESTED)
        return TurnReduction(TurnProjection(envelope=updated), (TurnIntent.DISPATCH_HOST,))

    if event.kind is TurnEventKind.DISPATCH_CONFIRMED:
        if state is not TurnState.DISPATCH_REQUESTED:
            raise _reject(projection, event)
        return TurnReduction(TurnProjection(envelope=replace(envelope, state=TurnState.DISPATCHED)))

    if event.kind is TurnEventKind.HOST_COMPLETED:
        if state is not TurnState.DISPATCHED or event.response is None:
            raise _reject(projection, event)
        _validate_response(envelope, event.response, projection, event)
        updated = replace(envelope, state=TurnState.HOST_COMPLETED)
        next_projection = TurnProjection(updated, event.response, PresentationState.READY)
        return TurnReduction(next_projection, (TurnIntent.PRESENT_VISIBLE,))

    if event.kind is TurnEventKind.VISIBLE_PRESENTED:
        if state is not TurnState.HOST_COMPLETED or presentation is not PresentationState.READY:
            raise _reject(projection, event)
        response = projection.response
        if response is None:
            raise _reject(projection, event, "canonical response is missing")
        if response.spoken_text.strip():
            next_presentation = PresentationState.VISIBLE
            intents = (TurnIntent.START_TTS,)
        else:
            next_presentation = PresentationState.COMPLETE
            intents = ()
        return TurnReduction(replace(projection, presentation=next_presentation), intents)

    if event.kind is TurnEventKind.TTS_STARTED:
        if state is not TurnState.HOST_COMPLETED or presentation is not PresentationState.VISIBLE:
            raise _reject(projection, event)
        return TurnReduction(replace(projection, presentation=PresentationState.TTS_STARTED))

    if event.kind is TurnEventKind.TTS_COMPLETED:
        if state is not TurnState.HOST_COMPLETED or presentation is not PresentationState.TTS_STARTED:
            raise _reject(projection, event)
        return TurnReduction(replace(projection, presentation=PresentationState.COMPLETE))

    if event.kind is TurnEventKind.TTS_FAILED:
        if state is not TurnState.HOST_COMPLETED or presentation is not PresentationState.TTS_STARTED:
            raise _reject(projection, event)
        return TurnReduction(replace(projection, presentation=PresentationState.TTS_FAILED))

    if event.kind is TurnEventKind.CANCELLED:
        cancellable = state in {
            TurnState.CAPTURING,
            TurnState.ACCEPTED,
            TurnState.DISPATCH_REQUESTED,
            TurnState.DISPATCHED,
        } or (
            state is TurnState.HOST_COMPLETED
            and presentation in {
                PresentationState.READY,
                PresentationState.VISIBLE,
                PresentationState.TTS_STARTED,
            }
        )
        if not cancellable:
            raise _reject(projection, event)
        return TurnReduction(
            TurnProjection(envelope=replace(envelope, state=TurnState.CANCELLED))
        )

    if event.kind is TurnEventKind.RECOVERY_UNCERTAIN:
        if state not in {TurnState.DISPATCH_REQUESTED, TurnState.DISPATCHED}:
            raise _reject(projection, event)
        return TurnReduction(
            TurnProjection(envelope=replace(envelope, state=TurnState.RECOVERY_UNCERTAIN))
        )

    raise _reject(projection, event)
