import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_mode.broker.hosts import (
    AppServerClosed,
    AppServerProtocolFault,
    AppServerRemoteError,
    AppServerRequestCancelled,
    AppServerRequestTimeout,
    AppServerTransport,
    AppServerTransportError,
)


FIXTURES = Path(__file__).parent / "fixtures" / "codex_app_server"


class PipePeer:
    def __init__(self, *, max_message_bytes=1024, stderr=None):
        server_read_fd, client_write_fd = os.pipe()
        client_read_fd, server_write_fd = os.pipe()
        self.server_reader = os.fdopen(server_read_fd, "rb", buffering=0)
        self.server_writer = os.fdopen(server_write_fd, "wb", buffering=0)
        self.transport = AppServerTransport(
            os.fdopen(client_read_fd, "rb", buffering=0),
            os.fdopen(client_write_fd, "wb", buffering=0),
            max_message_bytes=max_message_bytes,
            stderr=stderr,
            diagnostic_tail_bytes=16,
        )

    def read(self):
        return json.loads(self.server_reader.readline())

    def write(self, message, *, fragments=None):
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if fragments is None:
            self.server_writer.write(encoded)
        else:
            offset = 0
            for size in fragments:
                self.server_writer.write(encoded[offset : offset + size])
                offset += size
            self.server_writer.write(encoded[offset:])
        self.server_writer.flush()

    def close(self):
        self.transport.close()
        for stream in (self.server_reader, self.server_writer):
            try:
                stream.close()
            except OSError:
                pass


@pytest.fixture
def peer():
    value = PipePeer()
    yield value
    value.close()


def test_initialize_handshake_matches_golden_and_handles_fragmented_response(peer):
    golden = [json.loads(line) for line in (FIXTURES / "initialize_exchange.jsonl").read_text().splitlines()]
    observed = []

    def server():
        observed.append(peer.read())
        peer.write(golden[1], fragments=[1, 2, 3, 5])
        observed.append(peer.read())

    thread = threading.Thread(target=server)
    thread.start()
    result = peer.transport.initialize({"name": "voicemode", "version": "test"})
    thread.join(timeout=1)
    assert observed == [golden[0], golden[2]]
    assert result == golden[1]["result"]


def test_concurrent_requests_use_monotonic_ids_and_correlate_reverse_responses(peer):
    results = {}

    def call(method):
        results[method] = peer.transport.request(method, timeout=1)

    threads = [threading.Thread(target=call, args=(method,)) for method in ("first", "second")]
    for thread in threads:
        thread.start()
    requests = [peer.read(), peer.read()]
    assert sorted(request["id"] for request in requests) == [1, 2]
    for request in reversed(requests):
        peer.write({"id": request["id"], "result": request["method"]})
    for thread in threads:
        thread.join(timeout=1)
    assert results == {"first": "first", "second": "second"}


def test_notifications_and_server_requests_are_routed_without_blocking(peer):
    received = []
    unsubscribe = peer.transport.subscribe(received.append)
    peer.write({"method": "turn/completed", "params": {"id": "turn-1"}})
    peer.write({"id": "approval-1", "method": "approval", "params": {}})
    deadline = time.monotonic() + 1
    while len(received) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    unsubscribe()
    assert [message["method"] for message in received] == ["turn/completed", "approval"]


@pytest.mark.parametrize(
    "bad_line",
    [
        b"not-json\n",
        b"[]\n",
        b'{"jsonrpc":"1.0","method":"event"}\n',
    ],
)
def test_malformed_protocol_closes_transport(bad_line):
    peer = PipePeer()
    try:
        peer.server_writer.write(bad_line)
        peer.server_writer.flush()
        deadline = time.monotonic() + 1
        while not peer.transport.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert isinstance(peer.transport.fault, AppServerProtocolFault)
        with pytest.raises(AppServerClosed):
            peer.transport.notify("later")
    finally:
        peer.close()


def test_unknown_and_duplicate_response_ids_are_protocol_faults(peer):
    peer.write({"id": 99, "result": {}})
    deadline = time.monotonic() + 1
    while not peer.transport.closed and time.monotonic() < deadline:
        time.sleep(0.005)
    assert "unknown" in str(peer.transport.fault)

    other = PipePeer()
    try:
        result = []
        thread = threading.Thread(
            target=lambda: result.append(other.transport.request("once", timeout=1))
        )
        thread.start()
        request = other.read()
        response = {"id": request["id"], "result": "done"}
        other.write(response)
        thread.join(timeout=1)
        other.write(response)
        deadline = time.monotonic() + 1
        while not other.transport.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert result == ["done"]
        assert "duplicate" in str(other.transport.fault)
    finally:
        other.close()


def test_timeout_and_cancellation_tombstone_late_responses(peer):
    with pytest.raises(AppServerRequestTimeout):
        peer.transport.request("slow", timeout=0.01)
    timed_out = peer.read()
    peer.write({"id": timed_out["id"], "result": "late"})

    cancelled = threading.Event()
    outcome = []

    def call():
        try:
            peer.transport.request("cancel", timeout=1, cancel_event=cancelled)
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    request = peer.read()
    cancelled.set()
    thread.join(timeout=1)
    peer.write({"id": request["id"], "result": "late"})
    assert isinstance(outcome[0], AppServerRequestCancelled)
    assert peer.transport.closed is False


def test_remote_errors_remain_structured(peer):
    outcome = []

    def call():
        try:
            peer.transport.request("reject", timeout=1)
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    request = peer.read()
    peer.write(
        {
            "id": request["id"],
            "error": {"code": -32001, "message": "rejected", "data": {"reason": "test"}},
        }
    )
    thread.join(timeout=1)
    assert isinstance(outcome[0], AppServerRemoteError)
    assert outcome[0].code == -32001
    assert outcome[0].data == {"reason": "test"}


def test_size_limit_closes_oversized_input():
    peer = PipePeer(max_message_bytes=256)
    try:
        peer.server_writer.write(b"{" + b"x" * 300 + b"}\n")
        peer.server_writer.flush()
        deadline = time.monotonic() + 1
        while not peer.transport.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert "size limit" in str(peer.transport.fault)
    finally:
        peer.close()


def test_stderr_is_separate_and_bounded():
    stderr_read, stderr_write = os.pipe()
    stderr_reader = os.fdopen(stderr_read, "rb", buffering=0)
    peer = PipePeer(stderr=stderr_reader)
    try:
        os.write(stderr_write, b"diagnostic-prefix-0123456789")
        os.close(stderr_write)
        deadline = time.monotonic() + 1
        while not peer.transport.diagnostic_tail and time.monotonic() < deadline:
            time.sleep(0.005)
        assert peer.transport.diagnostic_tail == "refix-0123456789"
    finally:
        peer.close()


def test_process_factory_is_injected_and_command_is_fixed():
    calls = []
    process = SimpleNamespace(stdin=None, stdout=None, stderr=None, terminate=lambda: None)

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    with pytest.raises(AppServerTransportError):
        AppServerTransport.start_process(executable="codex-test", process_factory=factory)
    assert calls[0][0][0] == ["codex-test", "app-server", "--stdio"]
    assert calls[0][1]["stderr"] == -1


def test_peer_death_wakes_pending_request_and_fresh_transport_restarts_cleanly(peer):
    outcome = []

    def call():
        try:
            peer.transport.request("in-flight", timeout=1)
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=call)
    thread.start()
    assert peer.read()["id"] == 1
    peer.server_writer.close()
    thread.join(timeout=1)
    assert isinstance(outcome[0], AppServerClosed)

    restarted = PipePeer()
    try:
        result = []
        thread = threading.Thread(
            target=lambda: result.append(restarted.transport.request("after-restart", timeout=1))
        )
        thread.start()
        request = restarted.read()
        restarted.write({"id": request["id"], "result": "healthy"})
        thread.join(timeout=1)
        assert request["id"] == 1
        assert result == ["healthy"]
    finally:
        restarted.close()


def test_connects_to_user_owned_unix_socket(tmp_path):
    path = tmp_path / "app-server.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    observed = []

    def server():
        connection, _ = listener.accept()
        with connection, connection.makefile("rb") as reader:
            observed.append(json.loads(reader.readline()))

    thread = threading.Thread(target=server)
    thread.start()
    transport = AppServerTransport.connect_unix(path)
    try:
        transport.notify("ping", {"value": 1})
    finally:
        transport.close()
        thread.join(timeout=1)
        listener.close()
    assert observed == [
        {"method": "ping", "params": {"value": 1}}
    ]


def test_connect_unix_rejects_non_socket(tmp_path):
    path = tmp_path / "not-a-socket"
    path.write_text("synthetic", encoding="utf-8")
    with pytest.raises(AppServerTransportError, match="not a socket"):
        AppServerTransport.connect_unix(path)
