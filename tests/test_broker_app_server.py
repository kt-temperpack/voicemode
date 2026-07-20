from collections import deque
import pytest

from voice_mode.broker import HostCapability, HostErrorKind
from voice_mode.broker.hosts import AppServerHostAdapter, HostAdapterError
from voice_mode.broker.hosts.app_server_transport import AppServerClosed


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
        return response

    def close(self):
        self.closed = True


def adapter_with(*responses):
    transport = ScriptedTransport(*responses)
    adapter = AppServerHostAdapter(
        transport,
        {
            "codexHome": "/synthetic/codex-home",
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "codex-test",
        },
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
        }
    )
    assert [call[0] for call in transport.calls] == ["thread/list"]


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


@pytest.mark.parametrize(
    "invoke",
    [
        lambda adapter: adapter.start_turn(
            request_id="request", thread_id="thread", prompt="prompt"
        ),
        lambda adapter: adapter.steer_turn(
            request_id="request",
            thread_id="thread",
            host_turn_id="turn",
            prompt="prompt",
        ),
        lambda adapter: adapter.interrupt_turn(
            request_id="request", thread_id="thread", host_turn_id="turn"
        ),
        lambda adapter: adapter.query_disposition(
            request_id="request", thread_id="thread"
        ),
    ],
)
def test_unimplemented_turn_operations_fail_before_transport_io(invoke):
    adapter, transport = adapter_with()
    with pytest.raises(HostAdapterError) as caught:
        invoke(adapter)
    assert caught.value.kind is HostErrorKind.UNSUPPORTED
    assert transport.calls == []


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
