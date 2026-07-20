"""Validated client for the local conversation broker."""

from __future__ import annotations

import json
import socket
import time
import uuid
from pathlib import Path
from typing import Callable

from voice_mode.config import BROKER_READ_TIMEOUT_SECONDS, BROKER_SOCKET_PATH

from .protocol import PROTOCOL_VERSION
from .types import BrokerError, BrokerErrorCode


class BrokerUnavailable(BrokerError):
    def __init__(self, message: str = "broker is not running") -> None:
        super().__init__(BrokerErrorCode.TIMEOUT, message, retryable=True)


class BrokerClient:
    def __init__(
        self,
        socket_path: Path = BROKER_SOCKET_PATH,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float = BROKER_READ_TIMEOUT_SECONDS,
        request_id_factory: Callable[[], object] = uuid.uuid4,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.request_id_factory = request_id_factory

    def _request(self, operation: str, payload: dict, *, wait_seconds: float = 0.0) -> dict:
        request_id = str(self.request_id_factory())
        envelope = {"version": 1, "request_id": request_id, "operation": operation, "payload": payload}
        wire = (json.dumps(envelope, separators=(",", ":")) + "\n").encode()
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(self.connect_timeout)
            conn.connect(str(self.socket_path))
            conn.sendall(wire)
            deadline = time.monotonic() + self.read_timeout + wait_seconds
            data = bytearray()
            while len(data) <= 1_048_576:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout
                conn.settimeout(remaining)
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            raise BrokerUnavailable() from exc
        finally:
            conn.close()
        if len(data) > 1_048_576:
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an oversized response")
        if b"\n" in data and bytes(data).split(b"\n", 1)[1]:
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned trailing response data")
        try:
            response = json.loads(bytes(data).split(b"\n", 1)[0])
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as exc:
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response") from exc
        if not isinstance(response, dict) or response.get("version") != PROTOCOL_VERSION:
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        if response.get("request_id") != request_id or not isinstance(response.get("ok"), bool):
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        if response["ok"]:
            if set(response) != {"version", "request_id", "ok", "result"} or not isinstance(response["result"], dict):
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
            if not isinstance(response["result"].get("kind"), str):
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
            return self._validate_result(operation, response["result"])
        if set(response) != {"version", "request_id", "ok", "error"} or not isinstance(response["error"], dict):
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        error = response["error"]
        try:
            code = BrokerErrorCode(error["code"])
            message = error["message"]
            retryable = error["retryable"]
            if not isinstance(message, str) or not isinstance(retryable, bool):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response") from exc
        raise BrokerError(code, message, retryable=retryable)

    @staticmethod
    def _validate_result(operation: str, result: dict) -> dict:
        kind = result.get("kind")
        expected_kinds = {
            "status": {"status"},
            "open": {"session"},
            "turn": {"idle", "utterance"},
            "close": {"closed"},
            "stop": {"stopping"},
        }
        if kind not in expected_kinds.get(operation, set()):
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        required = {
            "status": {"kind", "state", "session", "pending_turns", "uptime_seconds", "protocol_version", "shutting_down"},
            "session": {"kind", "session", "capabilities"},
            "idle": {"kind"},
            "utterance": {"kind", "utterance_id", "text", "captured_at", "repo_root"},
            "closed": {"kind"},
            "stopping": {"kind"},
        }[kind]
        if set(result) != required:
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        if kind == "status":
            if (
                result["state"] not in {"asleep", "engaged", "listening", "thinking", "speaking"}
                or isinstance(result["pending_turns"], bool)
                or not isinstance(result["pending_turns"], int)
                or isinstance(result["uptime_seconds"], bool)
                or not isinstance(result["uptime_seconds"], (int, float))
                or result["protocol_version"] != 1
                or not isinstance(result["shutting_down"], bool)
            ):
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        if kind == "utterance" and not all(isinstance(result[field], str) for field in required - {"kind"}):
            raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        if kind in {"status", "session"} and result.get("session") is not None:
            session = result["session"]
            if not isinstance(session, dict) or set(session) != {"session_id", "codex_session_id", "repo_root", "age_seconds"}:
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
            if not all(isinstance(session[field], str) for field in ("session_id", "codex_session_id", "repo_root")):
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
            if isinstance(session["age_seconds"], bool) or not isinstance(session["age_seconds"], (int, float)):
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        if kind == "session":
            capabilities = result["capabilities"]
            if (
                not isinstance(capabilities, dict)
                or set(capabilities) != {"protocol_version", "pending_turn_limit", "audio_enabled"}
                or capabilities["protocol_version"] != 1
                or capabilities["pending_turn_limit"] != 1
                or not isinstance(capabilities["audio_enabled"], bool)
            ):
                raise BrokerError(BrokerErrorCode.INTERNAL_ERROR, "broker returned an invalid response")
        return result

    def status(self) -> dict:
        return self._request("status", {})

    def open(self, codex_session_id: str, repo_root: str) -> dict:
        return self._request("open", {"codex_session_id": codex_session_id, "repo_root": repo_root})

    def turn(self, session_id: str, spoken_summary: str = "", wait_seconds: float = 0.0) -> dict:
        return self._request(
            "turn",
            {"session_id": session_id, "spoken_summary": spoken_summary, "wait_seconds": wait_seconds},
            wait_seconds=wait_seconds,
        )

    def close(self, session_id: str) -> dict:
        return self._request("close", {"session_id": session_id})

    def stop(self) -> dict:
        return self._request("stop", {})
