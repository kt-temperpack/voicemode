"""Strict newline-delimited JSON protocol for the local broker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .types import BrokerError, BrokerErrorCode

PROTOCOL_VERSION = 1
MAX_NESTING = 16
MAX_SUMMARY_CHARS = 4_000


@dataclass(frozen=True)
class ProtocolLimits:
    max_message_bytes: int = 65_536
    long_poll_max_seconds: float = 120.0


@dataclass(frozen=True)
class StatusRequest:
    request_id: str


@dataclass(frozen=True)
class OpenRequest:
    request_id: str
    codex_session_id: str
    repo_root: str


@dataclass(frozen=True)
class TurnRequest:
    request_id: str
    session_id: str
    spoken_summary: str
    wait_seconds: float


@dataclass(frozen=True)
class CloseRequest:
    request_id: str
    session_id: str


@dataclass(frozen=True)
class StopRequest:
    request_id: str


BrokerRequest = StatusRequest | OpenRequest | TurnRequest | CloseRequest | StopRequest


class ProtocolError(BrokerError):
    def __init__(self, code: BrokerErrorCode, message: str, *, request_id: str = "") -> None:
        self.request_id = request_id
        super().__init__(code, message)


def _fail(code: BrokerErrorCode, message: str, request_id: str = "") -> None:
    raise ProtocolError(code, message, request_id=request_id)


def _check_depth(value: Any) -> None:
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_NESTING:
            _fail(BrokerErrorCode.INVALID_REQUEST, "request nesting is too deep")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _exact_fields(value: Mapping[str, Any], expected: set[str], request_id: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing:
        _fail(BrokerErrorCode.INVALID_REQUEST, f"missing field: {sorted(missing)[0]}", request_id)
    if unknown:
        _fail(BrokerErrorCode.INVALID_REQUEST, f"unknown field: {sorted(unknown)[0]}", request_id)


def _bounded_string(
    payload: Mapping[str, Any],
    field: str,
    request_id: str,
    *,
    allow_empty: bool = False,
    max_chars: int = 4_000,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        _fail(BrokerErrorCode.INVALID_REQUEST, f"{field} must be a string", request_id)
    if not allow_empty and not value:
        _fail(BrokerErrorCode.INVALID_REQUEST, f"{field} must not be empty", request_id)
    if len(value) > max_chars or len(value.encode("utf-8")) > max_chars:
        _fail(BrokerErrorCode.INVALID_REQUEST, f"{field} is too long", request_id)
    return value


def decode_request(raw: bytes, limits: ProtocolLimits | None = None) -> BrokerRequest:
    limits = limits or ProtocolLimits()
    if len(raw) > limits.max_message_bytes:
        _fail(BrokerErrorCode.INVALID_REQUEST, "request exceeds the byte limit")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _fail(BrokerErrorCode.INVALID_JSON, "request is not valid UTF-8 JSON")
    _check_depth(value)
    if not isinstance(value, dict):
        _fail(BrokerErrorCode.INVALID_REQUEST, "request must be an object")

    request_id = value.get("request_id") if isinstance(value.get("request_id"), str) else ""
    _exact_fields(value, {"version", "request_id", "operation", "payload"}, request_id)
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        _fail(BrokerErrorCode.INVALID_REQUEST, "version must be an integer", request_id)
    if version != PROTOCOL_VERSION:
        _fail(BrokerErrorCode.UNSUPPORTED_VERSION, "unsupported protocol version", request_id)
    if not (1 <= len(request_id) <= 128) or not request_id.isprintable():
        _fail(BrokerErrorCode.INVALID_REQUEST, "request_id must be 1-128 printable characters")
    operation = value["operation"]
    if not isinstance(operation, str):
        _fail(BrokerErrorCode.INVALID_REQUEST, "operation must be a string", request_id)
    payload = value["payload"]
    if not isinstance(payload, dict):
        _fail(BrokerErrorCode.INVALID_REQUEST, "payload must be an object", request_id)

    if operation == "status":
        _exact_fields(payload, set(), request_id)
        return StatusRequest(request_id)
    if operation == "stop":
        _exact_fields(payload, set(), request_id)
        return StopRequest(request_id)
    if operation == "open":
        _exact_fields(payload, {"codex_session_id", "repo_root"}, request_id)
        codex_id = _bounded_string(payload, "codex_session_id", request_id, max_chars=256)
        repo_root = _bounded_string(payload, "repo_root", request_id, max_chars=4_096)
        path = Path(repo_root).expanduser()
        if not path.is_absolute():
            _fail(BrokerErrorCode.INVALID_REQUEST, "repo_root must be absolute", request_id)
        return OpenRequest(request_id, codex_id, str(path.resolve(strict=False)))
    if operation == "turn":
        allowed = {"session_id", "spoken_summary", "wait_seconds"}
        unknown = payload.keys() - allowed
        if unknown or "session_id" not in payload:
            if unknown:
                _fail(BrokerErrorCode.INVALID_REQUEST, f"unknown field: {sorted(unknown)[0]}", request_id)
            _fail(BrokerErrorCode.INVALID_REQUEST, "missing field: session_id", request_id)
        session_id = _bounded_string(payload, "session_id", request_id, max_chars=128)
        summary_payload = {"spoken_summary": payload.get("spoken_summary", "")}
        summary = _bounded_string(
            summary_payload,
            "spoken_summary",
            request_id,
            allow_empty=True,
            max_chars=MAX_SUMMARY_CHARS,
        )
        wait = payload.get("wait_seconds", 0.0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            _fail(BrokerErrorCode.INVALID_REQUEST, "wait_seconds must be a number", request_id)
        if wait < 0 or wait > limits.long_poll_max_seconds:
            _fail(BrokerErrorCode.INVALID_REQUEST, "wait_seconds is outside the allowed range", request_id)
        return TurnRequest(request_id, session_id, summary, float(wait))
    if operation == "close":
        _exact_fields(payload, {"session_id"}, request_id)
        return CloseRequest(
            request_id,
            _bounded_string(payload, "session_id", request_id, max_chars=128),
        )
    _fail(BrokerErrorCode.UNKNOWN_OPERATION, "unknown operation", request_id)


def encode_success(request_id: str, result: Mapping[str, Any]) -> bytes:
    envelope = {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": dict(result),
    }
    return (json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n").encode()


def encode_error(error: BrokerError, request_id: str = "") -> bytes:
    envelope = {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": error.code.value,
            "message": error.public_message,
            "retryable": error.retryable,
        },
    }
    return (json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n").encode()
