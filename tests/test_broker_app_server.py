from collections import deque
import pytest

from voice_mode.broker import (
    HostCapability,
    HostDisposition,
    HostErrorKind,
    HostTurnState,
)
from voice_mode.broker.hosts import AppServerHostAdapter, HostAdapterError
from voice_mode.broker.hosts.app_server_transport import (
    AppServerClosed,
    AppServerRemoteError,
)


def thread_payload(
    thread_id="thread-1",
    *,
    cwd="/synthetic/repository",
    status="active",
    title="Current work",
):
    return {
        "id": thread_id,
        "cwd": cwd,
        "name": title,
        "preview": "Synthetic preview",
        "updatedAt": 1_752_892_800,
        "status": {"type": status},
    }


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.closed = False
        self.sinks = []

    def initialize(self, client_info, *, timeout):
        self.calls.append(("initialize", {"clientInfo": client_info}, timeout))
        return {
            "codexHome": "/synthetic/codex-home",
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "codex-test",
        }

    def request(self, method, params, *, timeout):
        self.calls.append((method, params, timeout))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(self)
        return response

    def subscribe(self, sink):
        self.sinks.append(sink)

        def unsubscribe():
            if sink in self.sinks:
                self.sinks.remove(sink)

        return unsubscribe

    def publish(self, method, params, request_id=None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        for sink in tuple(self.sinks):
            sink(message)

    def close(self):
        self.closed = True


def adapter_with(*responses, request_timeout=10.0):
    transport = ScriptedTransport(*responses)
    adapter = AppServerHostAdapter(
        transport,
        {
            "codexHome": "/synthetic/codex-home",
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "codex-test",
        },
        request_timeout=request_timeout,
    )
    return adapter, transport


def test_connect_initializes_before_exposing_adapter():
    transport = ScriptedTransport()
    adapter = AppServerHostAdapter.connect(
        transport, client_name="voicemode-test", client_version="1.2.3"
    )
    assert transport.calls == [
        (
            "initialize",
            {"clientInfo": {"name": "voicemode-test", "version": "1.2.3"}},
            10.0,
        )
    ]
    adapter.close()
    assert transport.closed is True


def test_probe_discovers_thread_contract_and_is_cached():
    adapter, transport = adapter_with({"data": [], "nextCursor": None})
    first = adapter.probe()
    second = adapter.probe()
    assert first is second
    assert first.available is True
    assert first.adapter == "app-server"
    assert first.capabilities == frozenset(
        {
            HostCapability.LIST_THREADS,
            HostCapability.READ_THREAD,
            HostCapability.ATTACH_THREAD,
            HostCapability.CREATE_THREAD,
            HostCapability.START_TURN,
            HostCapability.STEER_TURN,
            HostCapability.INTERRUPT_TURN,
            HostCapability.SUBSCRIBE_EVENTS,
            HostCapability.QUERY_DISPOSITION,
        }
    )
    assert [call[0] for call in transport.calls] == ["thread/list"]


def test_probe_respects_live_declared_method_capabilities():
    transport = ScriptedTransport({"data": [], "nextCursor": None})
    adapter = AppServerHostAdapter(
        transport,
        {
            "userAgent": "codex-limited",
            "capabilities": {
                "methods": ["thread/list", "thread/read", "turn/start"]
            },
        },
    )

    probe = adapter.probe()

    assert probe.capabilities == frozenset(
        {
            HostCapability.LIST_THREADS,
            HostCapability.READ_THREAD,
            HostCapability.START_TURN,
            HostCapability.SUBSCRIBE_EVENTS,
            HostCapability.QUERY_DISPOSITION,
        }
    )
    assert HostCapability.INTERRUPT_TURN not in probe.capabilities


def test_list_threads_is_repo_scoped_paginated_and_normalized(tmp_path):
    adapter, transport = adapter_with(
        {"data": [thread_payload("thread-2", cwd=str(tmp_path))], "nextCursor": "next"},
        {
            "data": [thread_payload("thread-1", cwd=str(tmp_path), status="notLoaded")],
            "nextCursor": None,
        },
    )
    threads = adapter.list_threads(str(tmp_path / "."))
    assert [thread.thread_id for thread in threads] == ["thread-2", "thread-1"]
    assert threads[0].repo_root == str(tmp_path.resolve())
    assert threads[0].active is True
    assert threads[1].active is False
    first_params = transport.calls[0][1]
    assert first_params["cwd"] == str(tmp_path.resolve())
    assert transport.calls[1][1]["cursor"] == "next"


def test_read_attach_and_create_use_current_codex_methods(tmp_path):
    payload = thread_payload(cwd=str(tmp_path), status="idle")
    adapter, transport = adapter_with(
        {"thread": payload},
        {"thread": payload},
        {"thread": thread_payload("thread-new", cwd=str(tmp_path), status="idle")},
        {},
    )
    assert adapter.read_thread("thread-1").thread_id == "thread-1"
    assert adapter.attach_thread("thread-1").active is True
    created = adapter.create_thread(str(tmp_path), "VoiceMode: synthetic")
    assert created.thread_id == "thread-new"
    assert created.title == "VoiceMode: synthetic"
    assert created.broker_owned is True
    assert [(method, params) for method, params, _timeout in transport.calls] == [
        ("thread/read", {"threadId": "thread-1", "includeTurns": False}),
        ("thread/resume", {"threadId": "thread-1", "excludeTurns": True}),
        (
            "thread/start",
            {"cwd": str(tmp_path.resolve()), "threadSource": "appServer"},
        ),
        ("thread/setName", {"threadId": "thread-new", "name": "VoiceMode: synthetic"}),
    ]


def test_start_and_steer_preserve_request_correlation_without_policy_overrides():
    adapter, transport = adapter_with(
        {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
        {"turnId": "turn-1"},
    )

    started = adapter.start_turn(
        request_id="request-1", thread_id="thread-1", prompt="inspect"
    )
    steered = adapter.steer_turn(
        request_id="request-2",
        thread_id="thread-1",
        host_turn_id="turn-1",
        prompt="also test",
    )

    assert started.state is HostTurnState.STARTED
    assert steered.state is HostTurnState.STEERED
    assert adapter.query_disposition(
        request_id="request-1", thread_id="thread-1"
    ) is HostDisposition.IN_PROGRESS
    assert transport.calls == [
        (
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "inspect"}],
                "clientUserMessageId": "request-1",
            },
            10.0,
        ),
        (
            "turn/steer",
            {
                "threadId": "thread-1",
                "expectedTurnId": "turn-1",
                "input": [{"type": "text", "text": "also test"}],
                "clientUserMessageId": "request-2",
            },
            10.0,
        ),
    ]


def test_interrupt_waits_for_terminal_cancellation_evidence():
    def confirm_interruption(transport):
        transport.publish(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "items": [], "status": "interrupted"},
            },
        )
        return {}

    adapter, _transport = adapter_with(
        {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
        confirm_interruption,
    )
    adapter.start_turn(request_id="request-1", thread_id="thread-1", prompt="work")

    interrupted = adapter.interrupt_turn(
        request_id="request-1", thread_id="thread-1", host_turn_id="turn-1"
    )

    assert interrupted.state is HostTurnState.CANCELLED
    assert adapter.query_disposition(
        request_id="request-1", thread_id="thread-1"
    ) is HostDisposition.CANCELLED


def test_rejected_steer_is_not_converted_into_a_new_turn():
    adapter, transport = adapter_with(
        {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
        AppServerRemoteError(-32000, "turn cannot accept steering"),
    )
    adapter.start_turn(request_id="request-1", thread_id="thread-1", prompt="work")

    with pytest.raises(HostAdapterError) as caught:
        adapter.steer_turn(
            request_id="request-2",
            thread_id="thread-1",
            host_turn_id="turn-1",
            prompt="follow up",
        )

    assert caught.value.kind is HostErrorKind.HOST_REJECTION
    assert [call[0] for call in transport.calls] == ["turn/start", "turn/steer"]
    assert adapter.query_disposition(
        request_id="request-1", thread_id="thread-1"
    ) is HostDisposition.IN_PROGRESS


def test_interrupt_without_terminal_evidence_is_ambiguous():
    adapter, _transport = adapter_with(
        {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
        {},
        request_timeout=0.01,
    )
    adapter.start_turn(request_id="request-1", thread_id="thread-1", prompt="work")

    with pytest.raises(HostAdapterError) as caught:
        adapter.interrupt_turn(
            request_id="request-1", thread_id="thread-1", host_turn_id="turn-1"
        )

    assert caught.value.kind is HostErrorKind.AMBIGUOUS


def test_transport_failure_is_classified_for_recovery():
    adapter, _transport = adapter_with(AppServerClosed("synthetic disconnect"))
    with pytest.raises(HostAdapterError) as caught:
        adapter.list_threads()
    assert caught.value.kind is HostErrorKind.RETRYABLE_TRANSPORT
    assert caught.value.retryable is True


def test_invalid_thread_payload_fails_closed():
    adapter, _transport = adapter_with({"thread": {"id": "missing-cwd"}})
    with pytest.raises(HostAdapterError) as caught:
        adapter.read_thread("missing-cwd")
    assert caught.value.kind is HostErrorKind.HOST_REJECTION
