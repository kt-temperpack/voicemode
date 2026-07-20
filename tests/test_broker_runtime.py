import threading
from datetime import datetime, timezone

import pytest

from voice_mode.broker import BrokerError, BrokerErrorCode, BrokerPhase
from voice_mode.broker.runtime import BrokerRuntime


class Clock:
    value = 10.0

    def __call__(self):
        return self.value


@pytest.fixture
def runtime(tmp_path):
    sequence = iter(["session-1", "utterance-1", "utterance-2"])
    return BrokerRuntime(
        uuid_factory=lambda: next(sequence),
        utc_now=lambda: datetime(2026, 7, 19, tzinfo=timezone.utc),
    )


def test_open_is_idempotent_for_same_caller_and_busy_for_another(runtime, tmp_path):
    first = runtime.open_session("codex-1", str(tmp_path))
    assert runtime.open_session("codex-1", str(tmp_path)) is first
    with pytest.raises(BrokerError) as caught:
        runtime.open_session("codex-2", str(tmp_path))
    assert caught.value.code is BrokerErrorCode.SESSION_BUSY


def test_fake_adapter_lifecycle_and_exactly_once_delivery(runtime, tmp_path):
    session = runtime.open_session("codex", str(tmp_path))
    runtime.activate(session.session_id)
    runtime.enqueue_utterance(session.session_id, "private words")
    turn = runtime.wait_for_turn(session.session_id, 0)
    assert turn.text == "private words"
    assert runtime.wait_for_turn(session.session_id, 0) is None
    runtime.accept_summary(session.session_id, "private answer")
    assert runtime.snapshot().phase is BrokerPhase.SPEAKING
    runtime.finish_playback(session.session_id)
    assert runtime.snapshot().phase is BrokerPhase.ENGAGED


def test_queue_full_close_and_shutdown_wake_waiters(runtime, tmp_path):
    session = runtime.open_session("codex", str(tmp_path))
    runtime.start_listening(session.session_id)
    runtime.enqueue_utterance(session.session_id, "one")
    with pytest.raises(BrokerError) as caught:
        runtime.enqueue_utterance(session.session_id, "two")
    assert caught.value.code is BrokerErrorCode.QUEUE_FULL
    runtime.close_session(session.session_id)
    runtime.close_session(session.session_id)
    with pytest.raises(BrokerError):
        runtime.wait_for_turn(session.session_id, 0)
    runtime.begin_shutdown()
    with pytest.raises(BrokerError) as stopped:
        runtime.open_session("new", str(tmp_path))
    assert stopped.value.code is BrokerErrorCode.SHUTTING_DOWN


def test_snapshot_and_events_never_expose_content(tmp_path):
    clock = Clock()
    events = []
    ids = iter(["session", "utterance"])
    runtime = BrokerRuntime(monotonic=clock, uuid_factory=lambda: next(ids), event_sink=lambda n, d: events.append((n, d)))
    session = runtime.open_session("codex-secret", str(tmp_path))
    runtime.activate(session.session_id)
    runtime.enqueue_utterance(session.session_id, "recognizable secret")
    clock.value = 13.0
    snap = runtime.snapshot()
    assert snap.uptime_seconds == 3.0
    assert "recognizable secret" not in repr(snap)
    assert "recognizable secret" not in repr(events)
    assert any(name == "BROKER_PHASE_CHANGE" for name, _ in events)
    allowed = {
        "old_phase", "new_phase", "duration_seconds", "session_id",
        "codex_session_prefix", "repo_root", "queue_count",
    }
    assert all(set(data) <= allowed for _name, data in events)


def test_close_wakes_a_blocked_waiter(runtime, tmp_path):
    session = runtime.open_session("codex", str(tmp_path))
    outcome = []

    def wait():
        try:
            runtime.wait_for_turn(session.session_id, 2)
        except BrokerError as error:
            outcome.append(error.code)

    thread = threading.Thread(target=wait)
    thread.start()
    runtime.close_session(session.session_id)
    thread.join(timeout=1)
    assert outcome == [BrokerErrorCode.SESSION_NOT_FOUND]


def test_shutdown_wakes_waiter_with_shutting_down(runtime, tmp_path):
    session = runtime.open_session("codex", str(tmp_path))
    outcome = []

    def wait():
        try:
            runtime.wait_for_turn(session.session_id, 2)
        except BrokerError as error:
            outcome.append(error.code)

    thread = threading.Thread(target=wait)
    thread.start()
    runtime.begin_shutdown()
    thread.join(timeout=1)
    assert outcome == [BrokerErrorCode.SHUTTING_DOWN]


def test_two_waiters_deliver_one_pending_turn_once(runtime, tmp_path):
    session = runtime.open_session("codex", str(tmp_path))
    runtime.activate(session.session_id)
    barrier = threading.Barrier(3)
    outcomes = []

    def wait():
        barrier.wait()
        result = runtime.wait_for_turn(session.session_id, 0.2)
        outcomes.append(None if result is None else result.utterance_id)

    threads = [threading.Thread(target=wait) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    runtime.enqueue_utterance(session.session_id, "one delivery")
    for thread in threads:
        thread.join(timeout=1)
    assert sorted(outcomes, key=lambda value: value or "") == [None, "utterance-1"]
