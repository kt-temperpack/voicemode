from datetime import datetime, timezone

import pytest

from voice_mode.broker.journal import JournalRecord
from voice_mode.broker.realtime.journal import (
    EVENT_FUNCTION_CALL_CLAIMED,
    EVENT_FUNCTION_RESULT_SENT,
    EVENT_JOB_RECOVERED,
    EVENT_JOB_TRANSITION,
    EVENT_RESPONSE_CLAIMED,
    EVENT_RESPONSE_COMPLETED,
    EVENT_RESPONSE_UNCERTAIN,
    EVENT_ROLLOVER_TRANSITION,
    EVENT_SESSION_TRANSITION,
    EVENT_SHUTDOWN,
    EVENT_TRANSPORT_TRANSITION,
    EVENT_USER_ITEM_CLAIMED,
    EVENT_WORKER_DELIVERY_CLAIMED,
    EVENT_WORKER_DELIVERY_DELIVERED,
    EVENT_WORKER_DELIVERY_UNCERTAIN,
    RealtimeJournalReplayError,
    replay_realtime_journal,
)
from voice_mode.broker.realtime.types import (
    DeliveryState,
    JobState,
    RolloverState,
    SessionState,
    TransportState,
    MAX_REMEMBERED_IDS,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc).isoformat()


def record(sequence: int, event: str, **kwargs) -> JournalRecord:
    return JournalRecord(
        schema_version=1,
        sequence=sequence,
        occurred_at=NOW,
        monotonic_duration_ms=sequence * 10,
        event=event,
        mode="realtime",
        **kwargs,
    )


def test_replay_reconstructs_realtime_state():
    replay = replay_realtime_journal(
        (
            record(1, EVENT_SESSION_TRANSITION, to_state=SessionState.STARTING.value),
            record(2, EVENT_TRANSPORT_TRANSITION, to_state=TransportState.CONNECTED.value),
            record(3, EVENT_USER_ITEM_CLAIMED, realtime_item_id="item_fixture_0001"),
            record(
                4,
                EVENT_RESPONSE_CLAIMED,
                realtime_item_id="item_fixture_0001",
                response_id="response_fixture_0001",
            ),
            record(5, EVENT_RESPONSE_COMPLETED, response_id="response_fixture_0001"),
            record(6, EVENT_FUNCTION_CALL_CLAIMED, function_call_id="call_fixture_0001"),
            record(7, EVENT_FUNCTION_RESULT_SENT, function_call_id="call_fixture_0001"),
            record(
                8,
                EVENT_JOB_TRANSITION,
                job_id="job_fixture_0001",
                to_state=JobState.RUNNING.value,
            ),
            record(
                9,
                EVENT_JOB_TRANSITION,
                job_id="job_fixture_0001",
                to_state=JobState.COMPLETED.value,
            ),
            record(
                10,
                EVENT_WORKER_DELIVERY_CLAIMED,
                job_id="job_fixture_0001",
                worker_delivery_id="delivery_fixture_0001",
            ),
            record(
                11,
                EVENT_WORKER_DELIVERY_DELIVERED,
                worker_delivery_id="delivery_fixture_0001",
            ),
            record(12, EVENT_ROLLOVER_TRANSITION, to_state=RolloverState.ACTIVE.value),
            record(13, EVENT_SHUTDOWN, to_state=SessionState.CLOSED.value),
        )
    )

    assert {item.value for item in replay.claimed_item_ids} == {"item_fixture_0001"}
    assert {item.value for item in replay.claimed_response_ids} == {"response_fixture_0001"}
    assert {item.value for item in replay.claimed_function_call_ids} == {"call_fixture_0001"}
    assert {item.value for item in replay.claimed_worker_delivery_ids} == {
        "delivery_fixture_0001"
    }
    assert replay.active_response_id is None
    assert replay.uncertain_response_id is None
    assert replay.current_job is not None
    assert replay.current_job.job_id.value == "job_fixture_0001"
    assert replay.current_job.state is JobState.COMPLETED
    assert replay.terminal_completion_available is True
    assert replay.worker_delivery_state is DeliveryState.DELIVERED
    assert replay.last_transport_state is TransportState.CLOSED
    assert replay.last_session_state is SessionState.CLOSED
    assert replay.last_rollover_state is RolloverState.ACTIVE


def test_replay_marks_uncertain_outcomes_without_replaying_effects():
    replay = replay_realtime_journal(
        (
            record(1, EVENT_USER_ITEM_CLAIMED, realtime_item_id="item_fixture_0001"),
            record(
                2,
                EVENT_RESPONSE_CLAIMED,
                realtime_item_id="item_fixture_0001",
                response_id="response_fixture_0001",
            ),
            record(3, EVENT_RESPONSE_UNCERTAIN, response_id="response_fixture_0001"),
            record(
                4,
                EVENT_JOB_TRANSITION,
                job_id="job_fixture_0001",
                to_state=JobState.COMPLETED.value,
            ),
            record(
                5,
                EVENT_WORKER_DELIVERY_CLAIMED,
                job_id="job_fixture_0001",
                worker_delivery_id="delivery_fixture_0001",
            ),
            record(
                6,
                EVENT_WORKER_DELIVERY_UNCERTAIN,
                worker_delivery_id="delivery_fixture_0001",
            ),
        )
    )

    assert replay.uncertain_response_id is not None
    assert replay.uncertain_response_id.value == "response_fixture_0001"
    assert replay.worker_delivery_state is DeliveryState.UNCERTAIN
    assert replay.terminal_completion_available is True


def test_replay_rejects_duplicate_response_claims_for_one_item():
    with pytest.raises(RealtimeJournalReplayError, match="more than one response"):
        replay_realtime_journal(
            (
                record(
                    1,
                    EVENT_RESPONSE_CLAIMED,
                    realtime_item_id="item_fixture_0001",
                    response_id="response_fixture_0001",
                ),
                record(
                    2,
                    EVENT_RESPONSE_CLAIMED,
                    realtime_item_id="item_fixture_0001",
                    response_id="response_fixture_0002",
                ),
            )
        )


def test_replay_rejects_worker_delivery_without_a_claim():
    with pytest.raises(RealtimeJournalReplayError, match="without a matching claim"):
        replay_realtime_journal(
            (
                record(
                    1,
                    EVENT_JOB_TRANSITION,
                    job_id="job_fixture_0001",
                    to_state=JobState.COMPLETED.value,
                ),
                record(
                    2,
                    EVENT_WORKER_DELIVERY_DELIVERED,
                    worker_delivery_id="delivery_fixture_0001",
                ),
            )
        )


def test_replay_rejects_terminal_job_revival_without_recovery_evidence():
    with pytest.raises(RealtimeJournalReplayError, match="without recovery evidence"):
        replay_realtime_journal(
            (
                record(
                    1,
                    EVENT_JOB_TRANSITION,
                    job_id="job_fixture_0001",
                    to_state=JobState.COMPLETED.value,
                ),
                record(
                    2,
                    EVENT_JOB_TRANSITION,
                    job_id="job_fixture_0001",
                    to_state=JobState.RUNNING.value,
                ),
            )
        )


def test_replay_allows_terminal_job_revival_with_explicit_recovery_evidence():
    replay = replay_realtime_journal(
        (
            record(
                1,
                EVENT_JOB_TRANSITION,
                job_id="job_fixture_0001",
                to_state=JobState.COMPLETED.value,
            ),
            record(2, EVENT_JOB_RECOVERED, job_id="job_fixture_0001"),
            record(
                3,
                EVENT_JOB_TRANSITION,
                job_id="job_fixture_0001",
                to_state=JobState.RUNNING.value,
            ),
        )
    )

    assert replay.current_job is not None
    assert replay.current_job.state is JobState.RUNNING
    assert replay.terminal_completion_available is False


def test_replay_bounds_remembered_ids():
    records = tuple(
        record(
            index + 1,
            EVENT_USER_ITEM_CLAIMED,
            realtime_item_id=f"item_fixture_{index:04d}",
        )
        for index in range(MAX_REMEMBERED_IDS + 8)
    )

    replay = replay_realtime_journal(records)

    assert len(replay.claimed_item_ids) == MAX_REMEMBERED_IDS
    assert all(item.value.startswith("item_fixture_") for item in replay.claimed_item_ids)


def test_replay_is_deterministic_for_same_records():
    records = (
        record(1, EVENT_SESSION_TRANSITION, to_state=SessionState.READY.value),
        record(2, EVENT_TRANSPORT_TRANSITION, to_state=TransportState.CONNECTED.value),
    )

    assert replay_realtime_journal(records) == replay_realtime_journal(records)
