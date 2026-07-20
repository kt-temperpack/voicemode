"""Bounded JSON-RPC transport for a persistent Codex app-server connection."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO


class AppServerTransportError(RuntimeError):
    """Base class for bounded app-server transport failures."""


class AppServerProtocolFault(AppServerTransportError):
    """The peer violated JSON-RPC framing or correlation."""


class AppServerClosed(AppServerTransportError):
    """The transport closed before an operation completed."""


class AppServerRequestTimeout(AppServerTransportError):
    """A request exceeded its caller-owned deadline."""


class AppServerRequestCancelled(AppServerTransportError):
    """A request was cancelled locally before a response arrived."""


class AppServerRemoteError(AppServerTransportError):
    """A structured JSON-RPC error returned by the app server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(message[:1000])


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


NotificationSink = Callable[[dict[str, Any]], None]
ProcessFactory = Callable[..., subprocess.Popen]


class AppServerTransport:
    """Thread-safe newline-delimited JSON-RPC client with strict correlation."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        *,
        process: subprocess.Popen | None = None,
        socket_owner: socket.socket | None = None,
        max_message_bytes: int = 4 * 1024 * 1024,
        diagnostic_tail_bytes: int = 16 * 1024,
        stderr: BinaryIO | None = None,
    ) -> None:
        if max_message_bytes < 256:
            raise ValueError("max_message_bytes must be at least 256")
        self._reader = reader
        self._writer = writer
        self._process = process
        self._socket_owner = socket_owner
        self._stderr = stderr
        self._max_message_bytes = max_message_bytes
        self._diagnostic_tail_bytes = diagnostic_tail_bytes
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._completed_ids: deque[int] = deque(maxlen=1024)
        self._cancelled_ids: deque[int] = deque(maxlen=1024)
        self._sinks: list[NotificationSink] = []
        self._next_id = 1
        self._closed = False
        self._closing = False
        self._fault: BaseException | None = None
        self._diagnostic_tail = bytearray()
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name="voicemode-app-server-reader",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread: threading.Thread | None = None
        if stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop,
                name="voicemode-app-server-stderr",
                daemon=True,
            )
            self._stderr_thread.start()

    @classmethod
    def start_process(
        cls,
        *,
        executable: str = "codex",
        process_factory: ProcessFactory = subprocess.Popen,
        max_message_bytes: int = 4 * 1024 * 1024,
        diagnostic_tail_bytes: int = 16 * 1024,
    ) -> AppServerTransport:
        process = process_factory(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise AppServerTransportError("app-server process did not expose stdio")
        return cls(
            process.stdout,
            process.stdin,
            process=process,
            stderr=process.stderr,
            max_message_bytes=max_message_bytes,
            diagnostic_tail_bytes=diagnostic_tail_bytes,
        )

    @classmethod
    def connect_unix(
        cls,
        path: str | Path,
        *,
        timeout: float = 5.0,
        max_message_bytes: int = 4 * 1024 * 1024,
    ) -> AppServerTransport:
        socket_path = Path(path)
        metadata = socket_path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode):
            raise AppServerTransportError(f"app-server path is not a socket: {socket_path}")
        if metadata.st_uid != os.getuid():
            raise AppServerTransportError(f"app-server socket is not owned by this user: {socket_path}")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect(str(socket_path))
            connection.settimeout(None)
            reader = connection.makefile("rb")
            writer = connection.makefile("wb")
        except BaseException:
            connection.close()
            raise
        return cls(
            reader,
            writer,
            socket_owner=connection,
            max_message_bytes=max_message_bytes,
        )

    @property
    def diagnostic_tail(self) -> str:
        with self._lock:
            return bytes(self._diagnostic_tail).decode("utf-8", errors="replace")

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def fault(self) -> BaseException | None:
        with self._lock:
            return self._fault

    def subscribe(self, sink: NotificationSink) -> Callable[[], None]:
        with self._lock:
            self._raise_if_closed()
            self._sinks.append(sink)

        def unsubscribe() -> None:
            with self._lock:
                if sink in self._sinks:
                    self._sinks.remove(sink)

        return unsubscribe

    def initialize(
        self,
        client_info: Mapping[str, Any],
        *,
        timeout: float = 10.0,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {"clientInfo": dict(client_info)},
            timeout=timeout,
            cancel_event=cancel_event,
        )
        if not isinstance(result, dict):
            self._fail(AppServerProtocolFault("initialize result must be an object"))
            raise self._fault  # type: ignore[misc]
        self.notify("initialized", {})
        return result

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        if not method:
            raise ValueError("method must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._lock:
            self._raise_if_closed()
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        try:
            self._write_message(
                {"id": request_id, "method": method, "params": dict(params or {})}
            )
        except BaseException:
            with self._lock:
                self._pending.pop(request_id, None)
            raise

        deadline_step = min(timeout, 0.05) if cancel_event is not None else timeout
        elapsed = 0.0
        while not pending.event.wait(deadline_step):
            elapsed += deadline_step
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_pending(request_id)
                raise AppServerRequestCancelled(f"request {request_id} was cancelled")
            if elapsed >= timeout:
                self._cancel_pending(request_id)
                raise AppServerRequestTimeout(f"request {request_id} timed out")
            deadline_step = min(0.05, timeout - elapsed)
        if pending.error is not None:
            raise pending.error
        return pending.result

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if not method:
            raise ValueError("method must not be empty")
        self._write_message(
            {"method": method, "params": dict(params or {})}
        )

    def respond(
        self,
        request_id: int | str,
        *,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        if error is not None:
            payload = {"id": request_id, "error": dict(error)}
        else:
            payload = {"id": request_id, "result": result}
        self._write_message(payload)

    def _cancel_pending(self, request_id: int) -> None:
        with self._lock:
            if self._pending.pop(request_id, None) is not None:
                self._cancelled_ids.append(request_id)

    def _write_message(self, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(encoded) > self._max_message_bytes:
            raise AppServerTransportError("outgoing app-server message exceeds size limit")
        with self._write_lock:
            with self._lock:
                self._raise_if_closed()
            try:
                self._writer.write(encoded)
                self._writer.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                fault = AppServerClosed("app-server write failed")
                self._fail(fault)
                raise fault from exc

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._reader.readline(self._max_message_bytes + 1)
                if not line:
                    with self._lock:
                        deliberate = self._closing
                    if not deliberate:
                        self._fail(AppServerClosed("app-server closed its output"))
                    return
                if len(line) > self._max_message_bytes or not line.endswith(b"\n"):
                    self._fail(AppServerProtocolFault("app-server message exceeds size limit"))
                    return
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._fail(AppServerProtocolFault("app-server returned malformed JSON"))
                    return
                version = message.get("jsonrpc") if isinstance(message, dict) else None
                if not isinstance(message, dict) or version not in {None, "2.0"}:
                    self._fail(AppServerProtocolFault("app-server returned an invalid JSON-RPC object"))
                    return
                if "method" in message:
                    self._publish(message)
                else:
                    self._resolve_response(message)
                with self._lock:
                    if self._closed:
                        return
        except (OSError, ValueError) as exc:
            with self._lock:
                deliberate = self._closing
            if not deliberate:
                self._fail(AppServerClosed(f"app-server read failed: {exc}"))
        finally:
            try:
                self._reader.close()
            except OSError:
                pass

    def _resolve_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            self._fail(AppServerProtocolFault("app-server response ID must be an integer"))
            return
        with self._lock:
            if request_id in self._cancelled_ids:
                return
            if request_id in self._completed_ids:
                self._fail(AppServerProtocolFault(f"duplicate app-server response ID {request_id}"))
                return
            pending = self._pending.pop(request_id, None)
            if pending is None:
                self._fail(AppServerProtocolFault(f"unknown app-server response ID {request_id}"))
                return
            self._completed_ids.append(request_id)
        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            fault = AppServerProtocolFault(
                "app-server response must contain exactly one of result or error"
            )
            pending.error = fault
            pending.event.set()
            self._fail(fault)
            return
        if has_error:
            error = message["error"]
            if not isinstance(error, dict) or not isinstance(error.get("code"), int):
                fault = AppServerProtocolFault("app-server returned an invalid error")
                pending.error = fault
                pending.event.set()
                self._fail(fault)
                return
            pending.error = AppServerRemoteError(
                error["code"], str(error.get("message", "app-server request failed")), error.get("data")
            )
        else:
            pending.result = message["result"]
        pending.event.set()

    def _publish(self, message: dict[str, Any]) -> None:
        if not isinstance(message.get("method"), str):
            self._fail(AppServerProtocolFault("app-server method must be a string"))
            return
        with self._lock:
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink(message)
            except Exception:
                continue

    def _stderr_loop(self) -> None:
        assert self._stderr is not None
        try:
            while chunk := self._stderr.read(4096):
                with self._lock:
                    self._diagnostic_tail.extend(chunk)
                    excess = len(self._diagnostic_tail) - self._diagnostic_tail_bytes
                    if excess > 0:
                        del self._diagnostic_tail[:excess]
        except (OSError, ValueError):
            return
        finally:
            try:
                self._stderr.close()
            except OSError:
                pass

    def _raise_if_closed(self) -> None:
        if self._closed:
            if self._fault is not None:
                raise AppServerClosed(str(self._fault)) from self._fault
            raise AppServerClosed("app-server transport is closed")

    def _fail(self, fault: BaseException) -> None:
        with self._lock:
            if self._closed:
                return
            self._fault = fault
            self._closed = True
            pending = tuple(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = fault
            request.event.set()
        self._close_resources()

    def _close_resources(self) -> None:
        try:
            self._writer.close()
        except OSError:
            pass
        if self._socket_owner is not None:
            try:
                self._socket_owner.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket_owner.close()
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1)

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._closed = True
            pending = tuple(self._pending.values())
            self._pending.clear()
        for request in pending:
            request.error = AppServerClosed("app-server transport closed")
            request.event.set()
        self._close_resources()
