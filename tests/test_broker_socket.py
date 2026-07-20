import json
import os
import socket
import stat
import threading
import time

import pytest

from voice_mode.broker import BrokerError, BrokerErrorCode
from voice_mode.broker.client import BrokerClient, BrokerUnavailable
from voice_mode.broker.server import BrokerServer, create_broker


def wait_ready(path, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("broker socket was not ready")


@pytest.fixture
def live_broker(tmp_path):
    path = tmp_path / "broker.sock"
    runtime, dispatcher, server = create_broker(path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_ready(path)
    yield runtime, BrokerClient(path, request_id_factory=lambda: "request-1"), server
    runtime.begin_shutdown()
    server.stop()
    thread.join(timeout=2)


def test_real_round_trip_and_content_redaction(live_broker, tmp_path):
    runtime, client, _server = live_broker
    opened = client.open("codex-private-identifier", str(tmp_path))
    session_id = opened["session"]["session_id"]
    assert opened["session"]["codex_session_id"] == "codex-pr"
    runtime.activate(session_id)
    runtime.enqueue_utterance(session_id, "recognizable secret")
    turn = client.turn(session_id)
    assert turn["text"] == "recognizable secret"
    status = client.status()
    assert "recognizable secret" not in json.dumps(status)
    assert status["state"] == "thinking"
    assert client.close(session_id) == {"kind": "closed"}


def test_stop_is_graceful_and_removes_socket(live_broker):
    _runtime, client, server = live_broker
    assert client.stop() == {"kind": "stopping"}
    deadline = time.monotonic() + 2
    while server.socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not server.socket_path.exists()


def test_socket_permissions_and_stale_socket_recovery(tmp_path):
    path = tmp_path / "b.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = BrokerServer(path, lambda _request: {"kind": "idle"})
    server.start()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) == 0o700
    server.stop()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_refuses_to_replace_non_socket_paths(tmp_path, kind):
    path = tmp_path / "b.sock"
    target = tmp_path / "target"
    target.write_text("keep")
    if kind == "file":
        path.write_text("keep")
    else:
        path.symlink_to(target)
    server = BrokerServer(path, lambda _request: {"kind": "idle"})
    with pytest.raises(OSError, match="unsafe"):
        server.start()
    assert path.exists() or path.is_symlink()


def test_bad_request_does_not_poison_server(live_broker):
    _runtime, client, server = live_broker
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(server.socket_path))
    conn.sendall(b"not json\n")
    response = json.loads(conn.recv(4096))
    conn.close()
    assert response["error"]["code"] == "invalid_json"
    assert client.status()["kind"] == "status"


def test_client_maps_missing_socket_and_runtime_errors(tmp_path, live_broker):
    with pytest.raises(BrokerUnavailable):
        BrokerClient(tmp_path / "missing.sock").status()
    _runtime, client, _server = live_broker
    with pytest.raises(BrokerError) as caught:
        client.close("wrong")
    assert caught.value.code is BrokerErrorCode.SESSION_NOT_FOUND


def test_client_rejects_malformed_response(tmp_path):
    path = tmp_path / "fake.sock"
    ready = threading.Event()

    def fake():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        ready.set()
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(b'{"version":1,"request_id":"wrong","ok":true,"result":{"kind":"status"}}\n')
        conn.close()
        listener.close()

    thread = threading.Thread(target=fake)
    thread.start()
    ready.wait(1)
    with pytest.raises(BrokerError) as caught:
        BrokerClient(path, request_id_factory=lambda: "right").status()
    thread.join(timeout=1)
    assert caught.value.code is BrokerErrorCode.INTERNAL_ERROR


def test_client_disconnect_cancels_long_poll(live_broker, tmp_path):
    _runtime, client, server = live_broker
    opened = client.open("codex", str(tmp_path))
    session_id = opened["session"]["session_id"]
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(server.socket_path))
    request = {
        "version": 1,
        "request_id": "disconnect",
        "operation": "turn",
        "payload": {"session_id": session_id, "spoken_summary": "", "wait_seconds": 2},
    }
    conn.sendall((json.dumps(request) + "\n").encode())
    conn.close()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with server._handlers_lock:
            if not server._handlers:
                break
        time.sleep(0.01)
    with server._handlers_lock:
        assert not server._handlers
    assert client.status()["kind"] == "status"
