from datetime import datetime, timezone

import pytest

from voice_mode.broker import (
    HostApprovalRequest,
    HostCapability,
    HostCompletion,
    HostDisposition,
    HostErrorKind,
    HostEvent,
    HostEventKind,
    HostProbe,
    HostRecoveryEvidence,
    HostThreadSummary,
    HostTurn,
    HostTurnState,
)
from voice_mode.broker.hosts import HostAdapter, HostAdapterError, require_capability


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
ALL_CAPABILITIES = frozenset(HostCapability)


class FakeHostAdapter(HostAdapter):
    def __init__(self) -> None:
        self.summary = HostThreadSummary(
            "thread-1", "/synthetic/repository", "VoiceMode", NOW, True, True
        )
        self.probe_result = HostProbe("fake", True, ALL_CAPABILITIES, "1.0")
        self.events = []
        self.sinks = []
        self.dispositions = {}
        self.fail_kind = None
        self.closed = False

    def _require(self, capability):
        require_capability(self.probe_result, capability)
        if self.fail_kind is not None:
            raise HostAdapterError(self.fail_kind, capability.value, "synthetic failure")

    def probe(self):
        return self.probe_result

    def list_threads(self, repo_root=None):
        self._require(HostCapability.LIST_THREADS)
        if repo_root is not None and repo_root != self.summary.repo_root:
            return ()
        return (self.summary,)

    def read_thread(self, thread_id):
        self._require(HostCapability.READ_THREAD)
        if thread_id != self.summary.thread_id:
            raise HostAdapterError(HostErrorKind.HOST_REJECTION, "read_thread", "unknown thread")
        return self.summary

    def attach_thread(self, thread_id):
        self._require(HostCapability.ATTACH_THREAD)
        return self.read_thread(thread_id)

    def create_thread(self, repo_root, label):
        self._require(HostCapability.CREATE_THREAD)
        self.summary = HostThreadSummary("thread-new", repo_root, label, NOW, True, True)
        return self.summary

    def start_turn(self, *, request_id, thread_id, prompt):
        self._require(HostCapability.START_TURN)
        assert request_id and prompt
        turn = HostTurn(request_id, thread_id, "host-turn-1", HostTurnState.STARTED)
        self.dispositions[request_id] = HostDisposition.IN_PROGRESS
        return turn

    def steer_turn(self, *, request_id, thread_id, host_turn_id, prompt):
        self._require(HostCapability.STEER_TURN)
        assert request_id and prompt
        return HostTurn(request_id, thread_id, host_turn_id, HostTurnState.STEERED)

    def interrupt_turn(self, *, request_id, thread_id, host_turn_id):
        self._require(HostCapability.INTERRUPT_TURN)
        self.dispositions[request_id] = HostDisposition.CANCELLED
        return HostTurn(request_id, thread_id, host_turn_id, HostTurnState.CANCELLED)

    def subscribe(self, sink):
        self._require(HostCapability.SUBSCRIBE_EVENTS)
        self.sinks.append(sink)

        def unsubscribe():
            if sink in self.sinks:
                self.sinks.remove(sink)

        return unsubscribe

    def query_disposition(self, *, request_id, thread_id):
        self._require(HostCapability.QUERY_DISPOSITION)
        assert thread_id
        return self.dispositions.get(request_id, HostDisposition.ABSENT)

    def emit(self, event):
        self.events.append(event)
        for sink in tuple(self.sinks):
            sink(event)

    def close(self):
        self.closed = True


class HostAdapterContract:
    """Reusable conformance suite for every concrete host adapter."""

    @pytest.fixture
    def adapter(self):
        raise NotImplementedError

    def test_probe_and_thread_lifecycle(self, adapter):
        probe = adapter.probe()
        assert probe.available is True
        assert probe.capabilities == ALL_CAPABILITIES
        assert adapter.list_threads("/synthetic/repository")[0].thread_id == "thread-1"
        assert adapter.read_thread("thread-1").repo_root == "/synthetic/repository"
        assert adapter.attach_thread("thread-1").active is True
        created = adapter.create_thread("/different/repository", "Hands-free")
        assert created.thread_id == "thread-new"
        assert created.broker_owned is True

    def test_every_dispatch_is_request_correlated(self, adapter):
        started = adapter.start_turn(
            request_id="request-1", thread_id="thread-1", prompt="synthetic prompt"
        )
        steered = adapter.steer_turn(
            request_id="request-2",
            thread_id="thread-1",
            host_turn_id=started.host_turn_id,
            prompt="synthetic steering",
        )
        interrupted = adapter.interrupt_turn(
            request_id="request-2",
            thread_id="thread-1",
            host_turn_id=started.host_turn_id,
        )
        assert (started.request_id, steered.request_id, interrupted.request_id) == (
            "request-1",
            "request-2",
            "request-2",
        )
        assert adapter.query_disposition(
            request_id="request-2", thread_id="thread-1"
        ) is HostDisposition.CANCELLED

    def test_subscription_and_close_are_idempotent(self, adapter):
        received = []
        unsubscribe = adapter.subscribe(received.append)
        event = HostEvent(HostEventKind.TURN_STARTED, "request-1", "thread-1")
        adapter.emit(event)
        unsubscribe()
        unsubscribe()
        adapter.emit(event)
        adapter.close()
        adapter.close()
        assert received == [event]
        assert adapter.closed is True

class TestFakeHostAdapter(HostAdapterContract):
    @pytest.fixture
    def adapter(self):
        return FakeHostAdapter()


@pytest.mark.parametrize(
    ("disposition", "expected_rationale"),
    (
        (HostDisposition.ABSENT, "the host has no evidence for the broker request ID"),
        (
            HostDisposition.IN_PROGRESS,
            "the host still shows the broker request in progress",
        ),
        (
            HostDisposition.COMPLETED,
            "the host reports the broker request completed without a canonical response payload",
        ),
        (HostDisposition.CANCELLED, "the host has terminal cancellation evidence"),
        (
            HostDisposition.UNCERTAIN,
            "the host evidence is present but not safe to classify",
        ),
    ),
)
def test_default_recover_request_maps_query_disposition_into_typed_evidence(
    disposition, expected_rationale
):
    adapter = FakeHostAdapter()
    adapter.dispositions["request-1"] = disposition

    evidence = adapter.recover_request(request_id="request-1", thread_id="thread-1")

    assert evidence == HostRecoveryEvidence(disposition, expected_rationale)


@pytest.mark.parametrize("kind", list(HostErrorKind))
def test_fake_adapter_demonstrates_every_failure_class(kind):
    adapter = FakeHostAdapter()
    adapter.fail_kind = kind
    with pytest.raises(HostAdapterError) as caught:
        adapter.start_turn(
            request_id="request-1", thread_id="thread-1", prompt="synthetic prompt"
        )
    assert caught.value.kind is kind
    assert caught.value.operation == HostCapability.START_TURN.value
    assert caught.value.retryable is (kind is HostErrorKind.RETRYABLE_TRANSPORT)


def test_capabilities_fail_before_adapter_io():
    unavailable = HostProbe("fake", False, frozenset(), reason="offline")
    with pytest.raises(HostAdapterError) as caught:
        require_capability(unavailable, HostCapability.START_TURN)
    assert caught.value.kind is HostErrorKind.UNAVAILABLE

    limited = HostProbe("fake", True, frozenset({HostCapability.READ_THREAD}))
    with pytest.raises(HostAdapterError) as caught:
        require_capability(limited, HostCapability.START_TURN)
    assert caught.value.kind is HostErrorKind.UNSUPPORTED


def test_completion_and_approval_are_typed_without_host_specific_payloads():
    completion = HostCompletion(
        "request-1",
        "thread-1",
        "host-turn-1",
        "One visible response.",
        "One spoken response.",
        NOW,
    )
    canonical = completion.canonical_response()
    approval = HostApprovalRequest(
        "request-1", "thread-1", "host-turn-1", "approval-1", "Needs user review"
    )
    event = HostEvent(
        HostEventKind.APPROVAL_REQUIRED,
        "request-1",
        "thread-1",
        "host-turn-1",
        approval=approval,
    )
    assert canonical.display_text == "One visible response."
    assert event.approval is approval
