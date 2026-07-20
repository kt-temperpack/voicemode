import json

import pytest

from voice_mode.broker.protocol import (
    CloseRequest,
    DiagnosticRequest,
    InterruptRequest,
    OpenRequest,
    ProtocolError,
    ProtocolLimits,
    StatusRequest,
    StopRequest,
    TurnRequest,
    decode_request,
    encode_error,
    encode_success,
)
from voice_mode.broker.types import BrokerError, BrokerErrorCode
from voice_mode.config import _bounded_env_number


def wire(operation, payload=None, *, version=1, **overrides):
    value = {"version": version, "request_id": "req-1", "operation": operation, "payload": payload or {}}
    value.update(overrides)
    return json.dumps(value).encode()


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (wire("status"), StatusRequest),
        (wire("stop"), StopRequest),
        (wire("open", {"codex_session_id": "codex-1", "repo_root": "/tmp/repo"}), OpenRequest),
        (wire("turn", {"session_id": "s", "spoken_summary": "done", "wait_seconds": 1}), TurnRequest),
        (wire("close", {"session_id": "s"}), CloseRequest),
        (wire("status", version=2), StatusRequest),
        (wire("diagnostic", version=2), DiagnosticRequest),
        (wire("interrupt", {"session_id": "s"}, version=2), InterruptRequest),
    ],
)
def test_valid_operations(raw, kind):
    assert isinstance(decode_request(raw), kind)


def test_open_canonicalizes_repo_root(tmp_path):
    request = decode_request(wire("open", {"codex_session_id": "c", "repo_root": str(tmp_path / "x" / "..") }))
    assert request.repo_root == str(tmp_path.resolve())


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"no", BrokerErrorCode.INVALID_JSON),
        (b"\xff", BrokerErrorCode.INVALID_JSON),
        (wire("status", version=3), BrokerErrorCode.UNSUPPORTED_VERSION),
        (wire("wat"), BrokerErrorCode.UNKNOWN_OPERATION),
        (wire("status", {"extra": True}), BrokerErrorCode.INVALID_REQUEST),
        (wire("open", {"codex_session_id": "c", "repo_root": "relative"}), BrokerErrorCode.INVALID_REQUEST),
        (wire("turn", {"session_id": "s", "wait_seconds": True}), BrokerErrorCode.INVALID_REQUEST),
        (wire("turn", {"session_id": "s", "wait_seconds": 121}), BrokerErrorCode.INVALID_REQUEST),
    ],
)
def test_invalid_requests_have_exact_codes(raw, code):
    with pytest.raises(ProtocolError) as caught:
        decode_request(raw)
    assert caught.value.code is code


def test_bounds_bytes_summary_and_nesting():
    with pytest.raises(ProtocolError):
        decode_request(b"{}" * 100, ProtocolLimits(max_message_bytes=8))
    with pytest.raises(ProtocolError):
        decode_request(wire("turn", {"session_id": "s", "spoken_summary": "x" * 4001}))
    with pytest.raises(ProtocolError):
        decode_request(wire("turn", {"session_id": "s", "spoken_summary": "é" * 3000}))
    nested = []
    for _ in range(20):
        nested = [nested]
    with pytest.raises(ProtocolError):
        decode_request(wire("status", {"x": nested}))


def test_response_envelopes_are_stable_and_do_not_serialize_exceptions():
    success = json.loads(encode_success("r", {"kind": "idle"}))
    assert success == {"version": 1, "request_id": "r", "ok": True, "result": {"kind": "idle"}}
    failure = json.loads(encode_error(BrokerError(BrokerErrorCode.QUEUE_FULL, "full", retryable=True), "r"))
    assert failure["error"] == {"code": "queue_full", "message": "full", "retryable": True}
    assert "Traceback" not in json.dumps(failure)


def test_v2_response_and_unsupported_version_recovery_are_explicit():
    success = json.loads(
        encode_success("r", {"kind": "diagnostic"}, version=2)
    )
    assert success["version"] == 2

    with pytest.raises(ProtocolError) as caught:
        decode_request(wire("status", version=99))
    failure = json.loads(encode_error(caught.value, caught.value.request_id))
    assert failure["error"]["code"] == "unsupported_version"
    assert failure["error"]["recovery_command"] == "voicemode broker status --json"


def test_v1_rejects_v2_only_operations():
    for operation, payload in (
        ("diagnostic", {}),
        ("interrupt", {"session_id": "session-1"}),
    ):
        with pytest.raises(ProtocolError) as caught:
            decode_request(wire(operation, payload))
        assert caught.value.code is BrokerErrorCode.UNKNOWN_OPERATION


def test_broker_numeric_configuration_is_clamped(monkeypatch):
    monkeypatch.setenv("BROKER_TEST_NUMBER", "999")
    assert _bounded_env_number("BROKER_TEST_NUMBER", 5, 1, 10, int) == 10
    monkeypatch.setenv("BROKER_TEST_NUMBER", "invalid")
    assert _bounded_env_number("BROKER_TEST_NUMBER", 5, 1, 10, int) == 5
