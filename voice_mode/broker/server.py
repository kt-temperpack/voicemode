"""Secure local Unix-socket transport and broker application wiring."""

from __future__ import annotations

import logging
import os
import signal
import select
import socket
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from voice_mode.config import (
    BROKER_LONG_POLL_MAX_SECONDS,
    BROKER_MAX_MESSAGE_BYTES,
    BROKER_READ_TIMEOUT_SECONDS,
    BROKER_SOCKET_PATH,
    BROKER_WRITE_TIMEOUT_SECONDS,
)

from .protocol import (
    CloseRequest,
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
from .runtime import BrokerRuntime
from .types import BrokerCapabilities, BrokerError, BrokerErrorCode

logger = logging.getLogger("voicemode.broker")
Dispatcher = Callable[[object], dict]


def _peer_uid(conn: socket.socket) -> int | None:
    try:
        if sys.platform.startswith("linux"):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
        if sys.platform == "darwin" or "bsd" in sys.platform:
            raw = conn.getsockopt(0, 0x001, 72)
            return struct.unpack("II", raw[:8])[1]
    except OSError:
        return None
    return None


class BrokerServer:
    def __init__(
        self,
        socket_path: Path,
        dispatcher: Dispatcher,
        *,
        limits: ProtocolLimits | None = None,
        read_timeout: float = BROKER_READ_TIMEOUT_SECONDS,
        write_timeout: float = BROKER_WRITE_TIMEOUT_SECONDS,
        max_handlers: int = 8,
        event_sink=None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.dispatcher = dispatcher
        self.limits = limits or ProtocolLimits(BROKER_MAX_MESSAGE_BYTES, BROKER_LONG_POLL_MAX_SECONDS)
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self._handler_slots = threading.BoundedSemaphore(max_handlers)
        self._event_sink = event_sink
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._handlers: set[threading.Thread] = set()
        self._handlers_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._server is not None and not self._stop.is_set()

    def _emit(self, name: str, **data) -> None:
        if self._event_sink:
            self._event_sink(name, data)

    def _safe_unlink(self, *, required: bool = False) -> None:
        try:
            info = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            if required:
                raise OSError(f"refusing to replace unsafe broker socket path: {self.socket_path}")
            return
        os.unlink(self.socket_path)

    def start(self) -> None:
        if self.is_running:
            return
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = os.stat(parent)
        if parent_info.st_uid != os.getuid():
            raise OSError(f"broker socket directory is not owned by the current user: {parent}")
        os.chmod(parent, 0o700)
        self._safe_unlink(required=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            server.bind(str(self.socket_path))
            server.listen(8)
            server.settimeout(0.2)
            os.chmod(self.socket_path, 0o600)
        except Exception:
            server.close()
            raise
        finally:
            os.umask(old_umask)
        self._stop.clear()
        self._server = server
        self._emit("BROKER_START", socket_path=str(self.socket_path))

    def serve_forever(self) -> None:
        self.start()
        assert self._server is not None
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                peer = _peer_uid(conn)
                if peer is not None and peer != os.getuid():
                    conn.close()
                    continue
                if not self._handler_slots.acquire(blocking=False):
                    error = BrokerError(BrokerErrorCode.TIMEOUT, "broker is busy", retryable=True)
                    try:
                        conn.sendall(encode_error(error))
                    finally:
                        conn.close()
                    continue
                thread = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                with self._handlers_lock:
                    self._handlers.add(thread)
                thread.start()
        finally:
            self.stop()

    def _read_request(self, conn: socket.socket) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + self.read_timeout
        while len(data) <= self.limits.max_message_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout
            conn.settimeout(remaining)
            chunk = conn.recv(min(4096, self.limits.max_message_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        if len(data) > self.limits.max_message_bytes:
            raise BrokerError(BrokerErrorCode.INVALID_REQUEST, "request exceeds the byte limit")
        if b"\n" in data:
            line, trailing = bytes(data).split(b"\n", 1)
            if trailing:
                raise BrokerError(BrokerErrorCode.INVALID_REQUEST, "trailing request data is not allowed")
            return line
        return bytes(data)

    def _handle(self, conn: socket.socket) -> None:
        current = threading.current_thread()
        request_id = ""
        monitor_done = threading.Event()
        try:
            conn.settimeout(self.read_timeout)
            raw = self._read_request(conn)
            request = decode_request(raw, self.limits)
            request_id = request.request_id
            cancel_event = None
            if isinstance(request, TurnRequest) and request.wait_seconds > 0:
                cancel_event = threading.Event()

                def monitor_disconnect() -> None:
                    while not monitor_done.wait(0.1):
                        try:
                            readable, _, _ = select.select([conn], [], [], 0)
                            if readable and conn.recv(1, socket.MSG_PEEK) == b"":
                                cancel_event.set()
                                return
                        except OSError:
                            cancel_event.set()
                            return

                threading.Thread(target=monitor_disconnect, daemon=True).start()
            dispatch_method = getattr(self.dispatcher, "dispatch", None)
            if dispatch_method is not None:
                result = dispatch_method(request, cancel_event=cancel_event)
            else:
                result = self.dispatcher(request)
            response = encode_success(request_id, result)
        except ProtocolError as error:
            request_id = error.request_id
            self._emit("BROKER_PROTOCOL_ERROR", error_code=error.code.value, retryable=error.retryable)
            response = encode_error(error, request_id)
        except BrokerError as error:
            response = encode_error(error, request_id)
        except socket.timeout:
            response = encode_error(BrokerError(BrokerErrorCode.TIMEOUT, "request timed out", retryable=True), request_id)
        except Exception:
            logger.exception("broker request failed")
            self._emit("BROKER_INTERNAL_ERROR", error_code="internal_error", retryable=False)
            response = encode_error(BrokerError(BrokerErrorCode.INTERNAL_ERROR, "internal broker error"), request_id)
        try:
            conn.settimeout(self.write_timeout)
            conn.sendall(response)
        except OSError:
            pass
        finally:
            monitor_done.set()
            conn.close()
            with self._handlers_lock:
                self._handlers.discard(current)
            self._handler_slots.release()

    def stop(self) -> None:
        if self._stop.is_set() and self._server is None:
            return
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            server.close()
        current = threading.current_thread()
        with self._handlers_lock:
            handlers = list(self._handlers)
        deadline = time.monotonic() + 2.0
        for thread in handlers:
            if thread is not current:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._safe_unlink()
        self._emit("BROKER_STOP", socket_path=str(self.socket_path))


class BrokerDispatcher:
    def __init__(
        self,
        runtime: BrokerRuntime,
        stop_callback: Callable[[], None] | None = None,
        *,
        audio_enabled: bool = False,
    ) -> None:
        self.runtime = runtime
        self.stop_callback = stop_callback
        self.audio_enabled = audio_enabled

    @staticmethod
    def _session_payload(session, age_seconds: float) -> dict:
        return {
            "session_id": session.session_id,
            "codex_session_id": session.codex_session_id[:8],
            "repo_root": session.repo_root,
            "age_seconds": age_seconds,
        }

    def __call__(self, request) -> dict:
        return self.dispatch(request)

    def dispatch(self, request, *, cancel_event=None) -> dict:
        if isinstance(request, StatusRequest):
            snap = self.runtime.snapshot()
            return {
                "kind": "status",
                "state": snap.phase.value,
                "session": self._session_payload(snap.session, snap.session_age_seconds or 0.0) if snap.session else None,
                "pending_turns": snap.pending_turns,
                "uptime_seconds": snap.uptime_seconds,
                "protocol_version": 1,
                "shutting_down": snap.shutting_down,
            }
        if isinstance(request, OpenRequest):
            session = self.runtime.open_session(request.codex_session_id, request.repo_root)
            age = self.runtime.snapshot().session_age_seconds or 0.0
            capabilities = BrokerCapabilities(audio_enabled=self.audio_enabled)
            return {
                "kind": "session",
                "session": self._session_payload(session, age),
                "capabilities": {
                    "protocol_version": capabilities.protocol_version,
                    "pending_turn_limit": capabilities.pending_turn_limit,
                    "audio_enabled": capabilities.audio_enabled,
                },
            }
        if isinstance(request, TurnRequest):
            if request.spoken_summary:
                self.runtime.accept_summary(request.session_id, request.spoken_summary)
            utterance = self.runtime.wait_for_turn(
                request.session_id,
                request.wait_seconds,
                cancel_event=cancel_event,
            )
            if utterance is None:
                return {"kind": "idle"}
            session = self.runtime.snapshot().session
            return {
                "kind": "utterance",
                "utterance_id": utterance.utterance_id,
                "text": utterance.text,
                "captured_at": utterance.captured_at.isoformat(),
                "repo_root": session.repo_root if session else "",
            }
        if isinstance(request, CloseRequest):
            self.runtime.close_session(request.session_id)
            return {"kind": "closed"}
        if isinstance(request, StopRequest):
            self.runtime.begin_shutdown()
            if self.stop_callback:
                threading.Thread(target=self.stop_callback, daemon=True).start()
            return {"kind": "stopping"}
        raise BrokerError(BrokerErrorCode.UNKNOWN_OPERATION, "unknown operation")


def create_broker(socket_path: Path | None = None, *, event_sink=None, audio_enabled: bool = False):
    runtime = BrokerRuntime(event_sink=event_sink)
    dispatcher = BrokerDispatcher(runtime, audio_enabled=audio_enabled)
    server = BrokerServer(socket_path or BROKER_SOCKET_PATH, dispatcher, event_sink=event_sink)
    dispatcher.stop_callback = server.stop
    return runtime, dispatcher, server


def run_broker(socket_path: Path | None = None) -> None:
    from voice_mode.config import EVENT_LOG_DIR, EVENT_LOG_ENABLED, setup_logging
    from voice_mode.utils import initialize_event_logger

    setup_logging()
    event_logger = initialize_event_logger(log_dir=Path(EVENT_LOG_DIR), enabled=True) if EVENT_LOG_ENABLED else None
    sink = (lambda name, data: event_logger.log_event(name, data)) if event_logger else None
    runtime, _dispatcher, server = create_broker(socket_path, event_sink=sink)

    def stop(_signum=None, _frame=None):
        runtime.begin_shutdown()
        server.stop()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
    server.serve_forever()
