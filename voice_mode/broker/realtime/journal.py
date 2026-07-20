"""Realtime replay over the shared broker journal."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TypeVar

from voice_mode.broker.journal import JournalRecord

from .types import (
    MAX_REMEMBERED_IDS,
    CodexJobId,
    DeliveryState,
    JobState,
    RealtimeFunctionCallId,
    RealtimeItemId,
    RealtimeResponseId,
    RolloverState,
    SessionState,
    TransportState,
    WorkerDeliveryId,
)

EVENT_SESSION_TRANSITION = "realtime.session_transition"
EVENT_TRANSPORT_TRANSITION = "realtime.transport_transition"
EVENT_USER_ITEM_CLAIMED = "realtime.user_item_claimed"
EVENT_RESPONSE_CLAIMED = "realtime.response_claimed"
EVENT_RESPONSE_COMPLETED = "realtime.response_completed"
EVENT_RESPONSE_CANCELLED = "realtime.response_cancelled"
EVENT_RESPONSE_FAILED = "realtime.response_failed"
EVENT_RESPONSE_UNCERTAIN = "realtime.response_uncertain"
EVENT_FUNCTION_CALL_CLAIMED = "realtime.function_call_claimed"
EVENT_FUNCTION_RESULT_SENT = "realtime.function_result_sent"
EVENT_FUNCTION_RESULT_UNCERTAIN = "realtime.function_result_uncertain"
EVENT_JOB_TRANSITION = "realtime.job_transition"
EVENT_JOB_RECOVERED = "realtime.job_recovered"
EVENT_WORKER_DELIVERY_CLAIMED = "realtime.worker_delivery_claimed"
EVENT_WORKER_DELIVERY_SENT = "realtime.worker_delivery_sent"
EVENT_WORKER_DELIVERY_DELIVERED = "realtime.worker_delivery_delivered"
EVENT_WORKER_DELIVERY_UNCERTAIN = "realtime.worker_delivery_uncertain"
EVENT_WORKER_DELIVERY_DROPPED = "realtime.worker_delivery_dropped"
EVENT_RECONNECT = "realtime.reconnect"
EVENT_ROLLOVER_TRANSITION = "realtime.rollover_transition"
EVENT_SHUTDOWN = "realtime.shutdown"

_TERMINAL_JOB_STATES = {
    JobState.COMPLETED,
    JobState.FAILED,
    JobState.INTERRUPTED,
    JobState.UNCERTAIN,
}
_TERMINAL_RESPONSE_EVENTS = {
    EVENT_RESPONSE_COMPLETED,
    EVENT_RESPONSE_CANCELLED,
    EVENT_RESPONSE_FAILED,
    EVENT_RESPONSE_UNCERTAIN,
}
_RememberedT = TypeVar("_RememberedT")
_IdentifierT = TypeVar("_IdentifierT")
_EnumT = TypeVar("_EnumT")


class RealtimeJournalReplayError(RuntimeError):
    """A realtime journal history is internally inconsistent."""


@dataclass(frozen=True)
class RealtimeJobReplay:
    job_id: CodexJobId
    state: JobState


@dataclass(frozen=True)
class RealtimeJournalReplay:
    claimed_item_ids: frozenset[RealtimeItemId]
    claimed_response_ids: frozenset[RealtimeResponseId]
    claimed_function_call_ids: frozenset[RealtimeFunctionCallId]
    claimed_worker_delivery_ids: frozenset[WorkerDeliveryId]
    active_response_id: RealtimeResponseId | None
    uncertain_response_id: RealtimeResponseId | None
    current_job: RealtimeJobReplay | None
    terminal_completion_available: bool
    worker_delivery_state: DeliveryState | None
    last_transport_state: TransportState
    last_session_state: SessionState
    last_rollover_state: RolloverState


def replay_realtime_journal(
    records: tuple[JournalRecord, ...],
) -> RealtimeJournalReplay:
    item_claims: set[RealtimeItemId] = set()
    response_claims: set[RealtimeResponseId] = set()
    function_claims: set[RealtimeFunctionCallId] = set()
    worker_claims: set[WorkerDeliveryId] = set()
    remembered_items = deque[RealtimeItemId]()
    remembered_responses = deque[RealtimeResponseId]()
    remembered_functions = deque[RealtimeFunctionCallId]()
    remembered_workers = deque[WorkerDeliveryId]()
    response_by_item: dict[RealtimeItemId, RealtimeResponseId] = {}
    active_response_id: RealtimeResponseId | None = None
    uncertain_response_id: RealtimeResponseId | None = None
    job_states: dict[CodexJobId, JobState] = {}
    recovery_ready: set[CodexJobId] = set()
    current_job: RealtimeJobReplay | None = None
    terminal_completion_available = False
    worker_delivery_state: DeliveryState | None = None
    worker_job: CodexJobId | None = None
    worker_delivery_id: WorkerDeliveryId | None = None
    last_transport_state = TransportState.DISCONNECTED
    last_session_state = SessionState.IDLE
    last_rollover_state = RolloverState.IDLE

    for record in records:
        if record.mode not in {None, "realtime"} and not record.event.startswith("realtime."):
            continue
        if not record.event.startswith("realtime."):
            continue

        if record.event == EVENT_SESSION_TRANSITION:
            last_session_state = _enum(record.to_state, SessionState, record, "to_state")
            continue
        if record.event == EVENT_TRANSPORT_TRANSITION:
            last_transport_state = _enum(record.to_state, TransportState, record, "to_state")
            continue
        if record.event == EVENT_ROLLOVER_TRANSITION:
            last_rollover_state = _enum(record.to_state, RolloverState, record, "to_state")
            continue
        if record.event == EVENT_RECONNECT:
            last_session_state = _enum(record.to_state, SessionState, record, "to_state")
            continue
        if record.event == EVENT_SHUTDOWN:
            last_transport_state = TransportState.CLOSED
            if record.to_state is not None:
                last_session_state = _enum(record.to_state, SessionState, record, "to_state")
            continue

        if record.event == EVENT_USER_ITEM_CLAIMED:
            item_id = _identifier(record.realtime_item_id, RealtimeItemId, record, "realtime_item_id")
            _remember(item_claims, remembered_items, item_id)
            continue

        if record.event == EVENT_RESPONSE_CLAIMED:
            item_id = _identifier(record.realtime_item_id, RealtimeItemId, record, "realtime_item_id")
            response_id = _identifier(record.response_id, RealtimeResponseId, record, "response_id")
            if item_id in response_by_item:
                raise RealtimeJournalReplayError(
                    f"record {record.sequence} claims more than one response for {item_id.value}"
                )
            response_by_item[item_id] = response_id
            _remember(item_claims, remembered_items, item_id)
            _remember(response_claims, remembered_responses, response_id)
            active_response_id = response_id
            uncertain_response_id = None
            continue

        if record.event in _TERMINAL_RESPONSE_EVENTS:
            response_id = _identifier(record.response_id, RealtimeResponseId, record, "response_id")
            if response_id not in response_claims:
                raise RealtimeJournalReplayError(
                    f"record {record.sequence} closes an unclaimed response"
                )
            if record.event == EVENT_RESPONSE_UNCERTAIN:
                uncertain_response_id = response_id
            else:
                uncertain_response_id = None
            if active_response_id == response_id:
                active_response_id = None
            continue

        if record.event == EVENT_FUNCTION_CALL_CLAIMED:
            call_id = _identifier(
                record.function_call_id,
                RealtimeFunctionCallId,
                record,
                "function_call_id",
            )
            _remember(function_claims, remembered_functions, call_id)
            continue

        if record.event in {EVENT_FUNCTION_RESULT_SENT, EVENT_FUNCTION_RESULT_UNCERTAIN}:
            call_id = _identifier(
                record.function_call_id,
                RealtimeFunctionCallId,
                record,
                "function_call_id",
            )
            if call_id not in function_claims:
                raise RealtimeJournalReplayError(
                    f"record {record.sequence} sends a result for an unclaimed function call"
                )
            continue

        if record.event == EVENT_JOB_RECOVERED:
            job_id = _identifier(record.job_id, CodexJobId, record, "job_id")
            recovery_ready.add(job_id)
            continue

        if record.event == EVENT_JOB_TRANSITION:
            job_id = _identifier(record.job_id, CodexJobId, record, "job_id")
            next_state = _enum(record.to_state, JobState, record, "to_state")
            previous_state = job_states.get(job_id)
            if previous_state in _TERMINAL_JOB_STATES and next_state != previous_state:
                if job_id not in recovery_ready:
                    raise RealtimeJournalReplayError(
                        f"record {record.sequence} revives terminal job {job_id.value} without recovery evidence"
                    )
                recovery_ready.remove(job_id)
            job_states[job_id] = next_state
            current_job = RealtimeJobReplay(job_id=job_id, state=next_state)
            terminal_completion_available = next_state in _TERMINAL_JOB_STATES
            if not terminal_completion_available:
                worker_delivery_state = None
                worker_job = None
                worker_delivery_id = None
            continue

        if record.event == EVENT_WORKER_DELIVERY_CLAIMED:
            delivery_id = _identifier(
                record.worker_delivery_id,
                WorkerDeliveryId,
                record,
                "worker_delivery_id",
            )
            job_id = _identifier(record.job_id, CodexJobId, record, "job_id")
            state = job_states.get(job_id)
            if state not in _TERMINAL_JOB_STATES:
                raise RealtimeJournalReplayError(
                    f"record {record.sequence} claims worker delivery before the job is terminal"
                )
            _remember(worker_claims, remembered_workers, delivery_id)
            worker_delivery_state = DeliveryState.CLAIMED
            worker_job = job_id
            worker_delivery_id = delivery_id
            continue

        if record.event in {
            EVENT_WORKER_DELIVERY_SENT,
            EVENT_WORKER_DELIVERY_DELIVERED,
            EVENT_WORKER_DELIVERY_UNCERTAIN,
            EVENT_WORKER_DELIVERY_DROPPED,
        }:
            delivery_id = _identifier(
                record.worker_delivery_id,
                WorkerDeliveryId,
                record,
                "worker_delivery_id",
            )
            if delivery_id not in worker_claims or worker_delivery_id != delivery_id:
                raise RealtimeJournalReplayError(
                    f"record {record.sequence} updates worker delivery without a matching claim"
                )
            if record.event == EVENT_WORKER_DELIVERY_SENT:
                worker_delivery_state = DeliveryState.SENT
            elif record.event == EVENT_WORKER_DELIVERY_DELIVERED:
                worker_delivery_state = DeliveryState.DELIVERED
            elif record.event == EVENT_WORKER_DELIVERY_UNCERTAIN:
                worker_delivery_state = DeliveryState.UNCERTAIN
            else:
                worker_delivery_state = DeliveryState.DROPPED
            continue

    if terminal_completion_available and worker_job is not None and current_job is not None:
        terminal_completion_available = current_job.job_id == worker_job

    return RealtimeJournalReplay(
        claimed_item_ids=frozenset(item_claims),
        claimed_response_ids=frozenset(response_claims),
        claimed_function_call_ids=frozenset(function_claims),
        claimed_worker_delivery_ids=frozenset(worker_claims),
        active_response_id=active_response_id,
        uncertain_response_id=uncertain_response_id,
        current_job=current_job,
        terminal_completion_available=terminal_completion_available,
        worker_delivery_state=worker_delivery_state,
        last_transport_state=last_transport_state,
        last_session_state=last_session_state,
        last_rollover_state=last_rollover_state,
    )


def _remember(
    values: set[_RememberedT],
    ordered: deque[_RememberedT],
    value: _RememberedT,
) -> None:
    if value in values:
        return
    values.add(value)
    ordered.append(value)
    if len(ordered) > MAX_REMEMBERED_IDS:
        evicted = ordered.popleft()
        values.discard(evicted)


def _identifier(
    raw: str | None,
    wrapper: type[_IdentifierT],
    record: JournalRecord,
    field_name: str,
) -> _IdentifierT:
    if raw is None:
        raise RealtimeJournalReplayError(
            f"record {record.sequence} is missing {field_name}"
        )
    try:
        return wrapper(raw)
    except (TypeError, ValueError) as error:
        raise RealtimeJournalReplayError(
            f"record {record.sequence} has an invalid {field_name}"
        ) from error


def _enum(
    raw: str | None,
    wrapper: type[_EnumT],
    record: JournalRecord,
    field_name: str,
) -> _EnumT:
    if raw is None:
        raise RealtimeJournalReplayError(
            f"record {record.sequence} is missing {field_name}"
        )
    try:
        return wrapper(raw)
    except ValueError as error:
        raise RealtimeJournalReplayError(
            f"record {record.sequence} has an invalid {field_name}"
        ) from error
