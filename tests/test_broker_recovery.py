from datetime import datetime, timezone

import pytest

from voice_mode.broker import (
    BrokerError,
    HostCompletion,
    HostDisposition,
    HostErrorKind,
    HostRecoveryEvidence,
)
from voice_mode.broker.hosts import HostAdapterError
from voice_mode.broker.journal import TurnJournal
from voice_mode.broker.recovery import RecoveryAction, RecoveryCoordinator
from voice_mode.broker.runtime import BrokerRuntime

from .test_broker_app_server import adapter_with, thread_payload


def immediate(task):
    task()


def journal(tmp_path):
    return TurnJournal(tmp_path, "recovery-session", retention_scheduler=immediate)


def durable_request(tmp_path, *, confirmed=True):
    identities = iter(["session-1", "utterance-1", "request-1"])
    evidence = journal(tmp_path)
    runtime = BrokerRuntime(uuid_factory=lambda: next(identities), journal=evidence)
    session = runtime.open_session("thread-1", str(tmp_path))
    runtime.activate(session.session_id)
    envelope = runtime.accept_turn(
        session.session_id,
        "private prompt",
        host_adapter="app-server",
        host_thread_id="thread-1",
    )
    if confirmed:
        runtime.claim_dispatch(envelope.request_id)
        runtime.confirm_dispatch(envelope.request_id)
    return evidence, envelope


class RecoveredAdapter:
    def __init__(self, evidence):
        self.evidence = evidence
        self.attached = []

    def attach_thread(self, thread_id):
        self.attached.append(thread_id)

    def recover_request(self, *, request_id, thread_id):
        assert request_id == "request-1"
        assert thread_id == "thread-1"
        return self.evidence


def completion():
    return HostCompletion(
        "request-1",
        "thread-1",
        "turn-1",
        "Recovered answer.",
        "Recovered answer.",
        datetime(2026, 7, 20, tzinfo=timezone.utc),
    )


def test_app_server_recovers_completed_request_from_thread_history(tmp_path):
    payload = thread_payload(cwd=str(tmp_path))
    payload["turns"] = [
        {
            "id": "turn-1",
            "clientUserMessageId": "request-1",
            "status": "completed",
            "completedAt": 1_752_892_800,
            "items": [{"type": "agentMessage", "text": "Recovered answer."}],
        }
    ]
    adapter, transport = adapter_with({"thread": payload})

    evidence = adapter.recover_request(request_id="request-1", thread_id="thread-1")

    assert evidence.disposition is HostDisposition.COMPLETED
    assert evidence.completion.display_text == "Recovered answer."
    assert transport.calls[0][1] == {"threadId": "thread-1", "includeTurns": True}


@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        ([], HostDisposition.ABSENT),
        (
            [
                {
                    "id": "turn-1",
                    "clientUserMessageId": "request-1",
                    "status": {"type": "inProgress"},
                    "items": [],
                }
            ],
            HostDisposition.IN_PROGRESS,
        ),
    ],
)
def test_app_server_recovers_nonterminal_dispositions(tmp_path, turns, expected):
    payload = thread_payload(cwd=str(tmp_path))
    payload["turns"] = turns
    adapter, _transport = adapter_with({"thread": payload})

    assert adapter.recover_request(
        request_id="request-1", thread_id="thread-1"
    ).disposition is expected


def test_completed_recovery_restores_one_presentable_response(tmp_path):
    evidence_journal, _envelope = durable_request(tmp_path)
    recovered_runtime = BrokerRuntime(journal=journal(tmp_path))
    adapter = RecoveredAdapter(
        HostRecoveryEvidence(
            HostDisposition.COMPLETED,
            "host history proves completion",
            completion(),
        )
    )
    coordinator = RecoveryCoordinator(
        recovered_runtime,
        evidence_journal,
        lambda: adapter,
        sleeper=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    decision = coordinator.recover(
        request_id="request-1",
        thread_id="thread-1",
        dispatch_confirmed=True,
    )

    assert decision.action is RecoveryAction.PRESENT
    assert adapter.attached == ["thread-1"]
    assert recovered_runtime.mark_visible_presented("request-1") is True
    assert recovered_runtime.mark_visible_presented("request-1") is False
    assert recovered_runtime.dispatch_frozen_reason is None


def test_absent_unconfirmed_request_requires_explicit_retry(tmp_path):
    evidence_journal, _envelope = durable_request(tmp_path, confirmed=False)
    recovered_runtime = BrokerRuntime(journal=journal(tmp_path))
    adapter = RecoveredAdapter(
        HostRecoveryEvidence(HostDisposition.ABSENT, "request is absent")
    )
    coordinator = RecoveryCoordinator(
        recovered_runtime,
        evidence_journal,
        lambda: adapter,
        sleeper=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    decision = coordinator.recover(
        request_id="request-1",
        thread_id="thread-1",
        dispatch_confirmed=False,
    )

    assert decision.action is RecoveryAction.RETRY_ALLOWED
    assert recovered_runtime.claim_dispatch("request-1").should_dispatch is False


def test_ambiguous_confirmed_request_never_redispatches(tmp_path):
    evidence_journal, _envelope = durable_request(tmp_path)
    recovered_runtime = BrokerRuntime(journal=journal(tmp_path))
    coordinator = RecoveryCoordinator(
        recovered_runtime,
        evidence_journal,
        lambda: RecoveredAdapter(
            HostRecoveryEvidence(HostDisposition.UNCERTAIN, "history is ambiguous")
        ),
        sleeper=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    decision = coordinator.recover(
        request_id="request-1",
        thread_id="thread-1",
        dispatch_confirmed=True,
    )

    assert decision.action is RecoveryAction.MANUAL
    assert recovered_runtime.claim_dispatch("request-1").should_dispatch is False
    assert "ambiguous" in coordinator.status().rationale


def test_repeated_connection_failures_open_circuit_and_keep_dispatch_frozen(tmp_path):
    evidence_journal, _envelope = durable_request(tmp_path)
    recovered_runtime = BrokerRuntime(journal=journal(tmp_path))
    sleeps = []
    attempts = 0

    def fail():
        nonlocal attempts
        attempts += 1
        raise HostAdapterError(
            HostErrorKind.RETRYABLE_TRANSPORT,
            "connect",
            "synthetic disconnect",
        )

    coordinator = RecoveryCoordinator(
        recovered_runtime,
        evidence_journal,
        fail,
        max_attempts=3,
        base_delay=0.25,
        sleeper=sleeps.append,
        jitter=lambda _delay: 0,
    )

    decision = coordinator.recover(
        request_id="request-1",
        thread_id="thread-1",
        dispatch_confirmed=True,
    )

    assert decision.action is RecoveryAction.CIRCUIT_OPEN
    assert sleeps == [0.25, 0.5]
    assert attempts == 3
    assert recovered_runtime.dispatch_frozen_reason is not None
    assert coordinator.recover(
        request_id="request-1",
        thread_id="thread-1",
        dispatch_confirmed=True,
    ).action is RecoveryAction.CIRCUIT_OPEN
    assert attempts == 3


def test_frozen_runtime_rejects_new_dispatch_but_status_and_shutdown_work(tmp_path):
    identities = iter(["session-1", "utterance-1", "request-1"])
    runtime = BrokerRuntime(uuid_factory=lambda: next(identities))
    session = runtime.open_session("thread-1", str(tmp_path))
    runtime.activate(session.session_id)
    envelope = runtime.accept_turn(
        session.session_id,
        "prompt",
        host_adapter="app-server",
        host_thread_id="thread-1",
    )
    runtime.freeze_dispatch("transport lost")

    with pytest.raises(BrokerError, match="dispatch is frozen"):
        runtime.claim_dispatch(envelope.request_id)
    assert runtime.snapshot().shutting_down is False
    runtime.begin_shutdown()
    assert runtime.snapshot().shutting_down is True
