import threading
from datetime import datetime, timezone

import pytest

from voice_mode.broker import (
    BrokerError,
    BrokerErrorCode,
    DispatchDisposition,
)
from voice_mode.broker.journal import JournalEvent, TurnJournal, atomic_append
from voice_mode.broker.runtime import BrokerRuntime


def immediate(task):
    task()


def make_journal(tmp_path, session_id="broker-session", **overrides):
    return TurnJournal(
        tmp_path,
        session_id,
        retention_scheduler=immediate,
        **overrides,
    )


def make_runtime(tmp_path, *, journal=None, ids=None):
    identity = iter(ids or ["session-1", "utterance-1", "request-1"])
    runtime = BrokerRuntime(
        uuid_factory=lambda: next(identity),
        utc_now=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        journal=journal,
    )
    session = runtime.open_session("thread-1", str(tmp_path))
    runtime.activate(session.session_id)
    return runtime, session


def accept(runtime, session):
    return runtime.accept_turn(
        session.session_id,
        "a private prompt",
        host_adapter="app-server",
        host_thread_id="thread-1",
    )


def test_concurrent_claimers_authorize_exactly_one_host_submission(tmp_path):
    journal = make_journal(tmp_path)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)
    barrier = threading.Barrier(9)
    claims = []

    def claim():
        barrier.wait()
        claims.append(runtime.claim_dispatch(envelope.request_id))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert sum(claim.should_dispatch for claim in claims) == 1
    assert {claim.disposition for claim in claims} == {DispatchDisposition.CLAIMED}
    assert [record.event for record in journal.read()].count("dispatch_claimed") == 1


def test_identifiers_are_assigned_once_at_acceptance_and_remain_stable(tmp_path):
    runtime, session = make_runtime(tmp_path)
    envelope = accept(runtime, session)

    first = runtime.claim_dispatch(envelope.request_id)
    second = runtime.claim_dispatch(envelope.request_id)
    confirmed = runtime.confirm_dispatch(envelope.request_id)

    assert envelope.utterance_id == "utterance-1"
    assert envelope.request_id == "request-1"
    assert first.request_id == second.request_id == confirmed.request_id == "request-1"
    assert first.should_dispatch is True
    assert second.should_dispatch is False
    assert confirmed.disposition is DispatchDisposition.CONFIRMED


def test_failed_claim_append_does_not_publish_dispatch_authority(tmp_path):
    writes = 0

    def fail_second(path, payload):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated durable write failure")
        atomic_append(path, payload)

    journal = make_journal(tmp_path, writer=fail_second)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)

    with pytest.raises(OSError, match="durable write failure"):
        runtime.claim_dispatch(envelope.request_id)

    assert runtime.dispatch_disposition(envelope.request_id) is DispatchDisposition.PENDING
    assert [record.event for record in journal.read()] == ["turn_accepted"]


def test_failed_acceptance_append_does_not_publish_a_pending_turn(tmp_path):
    def fail(_path, _payload):
        raise OSError("simulated acceptance write failure")

    journal = make_journal(tmp_path, writer=fail)
    runtime, session = make_runtime(tmp_path, journal=journal)

    with pytest.raises(OSError, match="acceptance write failure"):
        accept(runtime, session)

    assert journal.read() == ()
    with pytest.raises(BrokerError) as caught:
        runtime.dispatch_disposition("request-1")
    assert caught.value.code is BrokerErrorCode.INVALID_REQUEST


def test_failed_confirmation_remains_claimed_and_cannot_redispatch(tmp_path):
    writes = 0

    def fail_third(path, payload):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("simulated confirmation write failure")
        atomic_append(path, payload)

    journal = make_journal(tmp_path, writer=fail_third)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)
    runtime.claim_dispatch(envelope.request_id)

    with pytest.raises(OSError, match="confirmation write failure"):
        runtime.confirm_dispatch(envelope.request_id)

    assert runtime.dispatch_disposition(envelope.request_id) is DispatchDisposition.CLAIMED
    assert runtime.claim_dispatch(envelope.request_id).should_dispatch is False
    recovered = BrokerRuntime(journal=make_journal(tmp_path))
    assert recovered.claim_dispatch(envelope.request_id).disposition is DispatchDisposition.UNCERTAIN


def test_restart_after_claim_is_uncertain_and_never_redispatched(tmp_path):
    journal = make_journal(tmp_path)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)
    assert runtime.claim_dispatch(envelope.request_id).should_dispatch is True

    recovered = BrokerRuntime(journal=make_journal(tmp_path))

    assert recovered.dispatch_disposition(envelope.request_id) is DispatchDisposition.UNCERTAIN
    claim = recovered.claim_dispatch(envelope.request_id)
    assert claim.disposition is DispatchDisposition.UNCERTAIN
    assert claim.should_dispatch is False


def test_restart_after_confirmation_is_still_never_redispatched(tmp_path):
    journal = make_journal(tmp_path)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)
    runtime.claim_dispatch(envelope.request_id)
    runtime.confirm_dispatch(envelope.request_id)

    recovered = BrokerRuntime(journal=make_journal(tmp_path))

    assert recovered.claim_dispatch(envelope.request_id).disposition is DispatchDisposition.UNCERTAIN
    assert recovered.claim_dispatch(envelope.request_id).should_dispatch is False


@pytest.mark.parametrize(
    ("terminal_event", "expected"),
    [
        ("host_completed", DispatchDisposition.COMPLETED),
        ("turn_cancelled", DispatchDisposition.CANCELLED),
    ],
)
def test_recovery_classifies_terminal_evidence(tmp_path, terminal_event, expected):
    journal = make_journal(tmp_path)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)
    runtime.claim_dispatch(envelope.request_id)
    journal.append(JournalEvent(event=terminal_event, request_id=envelope.request_id))

    recovered = BrokerRuntime(journal=make_journal(tmp_path))

    assert recovered.dispatch_disposition(envelope.request_id) is expected
    assert recovered.claim_dispatch(envelope.request_id).should_dispatch is False


def test_unclaimed_recovery_is_safe_to_cancel_not_automatically_submit(tmp_path):
    journal = make_journal(tmp_path)
    runtime, session = make_runtime(tmp_path, journal=journal)
    envelope = accept(runtime, session)

    recovered = BrokerRuntime(journal=make_journal(tmp_path))

    assert recovered.dispatch_disposition(envelope.request_id) is DispatchDisposition.SAFE_TO_CANCEL
    assert recovered.claim_dispatch(envelope.request_id).should_dispatch is False


def test_acceptance_enforces_one_pending_turn_and_journal_omits_transcript(tmp_path):
    journal = make_journal(tmp_path)
    runtime, session = make_runtime(
        tmp_path,
        journal=journal,
        ids=["session-1", "utterance-1", "request-1", "utterance-2", "request-2"],
    )
    accept(runtime, session)

    with pytest.raises(BrokerError) as caught:
        accept(runtime, session)

    assert caught.value.code is BrokerErrorCode.QUEUE_FULL
    assert "a private prompt" not in journal.path.read_text()


def test_unknown_request_is_rejected(tmp_path):
    runtime = BrokerRuntime()

    with pytest.raises(BrokerError) as caught:
        runtime.claim_dispatch("missing")

    assert caught.value.code is BrokerErrorCode.INVALID_REQUEST
