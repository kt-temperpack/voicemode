"""Authenticated loopback web boundary for the realtime broker."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Awaitable, Callable, Mapping, Protocol

from aiohttp import web

from .security import (
    LOOPBACK_HOST,
    build_loopback_origin,
    compare_capability_token,
    ensure_bounded_bytes,
    extract_bearer_token,
    generate_capability_token,
    redact_public_error_text,
    validate_capability_token,
    validate_forwarded_headers,
    validate_loopback_bind_host,
    validate_loopback_socket_binding,
    validate_port,
)
from .types import (
    MAX_LOCAL_CONTROL_BYTES,
    MAX_LOCAL_EVENT_BACKLOG,
    MAX_SDP_BYTES,
    public_json_dumps,
)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' https://api.openai.com wss://api.openai.com; "
    "media-src 'self' blob:; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Expires": "0",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    **NO_CACHE_HEADERS,
}
EVENT_STREAM_CONTENT_TYPE = "application/x-ndjson"
STATIC_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "assets/app.js": "application/javascript; charset=utf-8",
    "assets/styles.css": "text/css; charset=utf-8",
}
GENERATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_CONTROL_ACTIONS = frozenset(
    {
        "heartbeat",
        "takeover",
        "stop",
        "stop_speaking",
        "cancel_job",
    }
)
RUNTIME_APP_KEY: web.AppKey["LoopbackWebRuntime"] = web.AppKey("realtime_web_runtime")


class AssetLoader(Protocol):
    def __call__(self, asset_path: str) -> bytes:
        ...


class MissingOriginPolicy(Protocol):
    def __call__(self, request: web.Request) -> bool:
        ...


class SnapshotProvider(Protocol):
    def __call__(self) -> Any:
        ...


class SessionStarter(Protocol):
    def __call__(self, offer_sdp: str) -> "SessionBootstrap | Awaitable[SessionBootstrap]":
        ...


class SessionReadyHandler(Protocol):
    def __call__(self, generation: str) -> Mapping[str, Any] | Awaitable[Mapping[str, Any] | None] | None:
        ...


class ControlHandler(Protocol):
    def __call__(
        self,
        action: str,
        generation: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any] | None] | None:
        ...


class GenerationCallback(Protocol):
    def __call__(self, generation: str) -> None | Awaitable[None]:
        ...


class ShutdownCallback(Protocol):
    def __call__(self) -> None | Awaitable[None]:
        ...


@dataclass(frozen=True)
class SessionBootstrap:
    generation: str
    answer_sdp: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _validate_generation(self.generation))
        ensure_bounded_bytes(
            self.answer_sdp,
            label="answer SDP",
            max_bytes=MAX_SDP_BYTES,
        )


@dataclass(frozen=True)
class LoopbackWebConfig:
    capability_token: str
    bind_host: str = LOOPBACK_HOST
    bind_port: int = 0
    event_queue_size: int = MAX_LOCAL_EVENT_BACKLOG
    heartbeat_timeout_seconds: float = 10.0
    keepalive_interval_seconds: float = 2.0
    timeout_callback_timeout_seconds: float = 1.0
    shutdown_callback_timeout_seconds: float = 5.0
    internal_missing_origin_policy: MissingOriginPolicy | None = None
    asset_loader: AssetLoader | None = None
    bound_socket: Any | None = None

    def __post_init__(self) -> None:
        validate_capability_token(self.capability_token)
        validate_loopback_bind_host(self.bind_host)
        validate_port(self.bind_port, allow_zero=True)
        if isinstance(self.event_queue_size, bool) or not isinstance(self.event_queue_size, int):
            raise TypeError("event_queue_size must be an integer")
        if self.event_queue_size < 2:
            raise ValueError("event_queue_size must be at least 2")
        if self.heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        if self.keepalive_interval_seconds <= 0:
            raise ValueError("keepalive_interval_seconds must be positive")
        if self.timeout_callback_timeout_seconds <= 0:
            raise ValueError("timeout_callback_timeout_seconds must be positive")
        if self.shutdown_callback_timeout_seconds <= 0:
            raise ValueError("shutdown_callback_timeout_seconds must be positive")
        if self.bound_socket is not None:
            validate_loopback_socket_binding(self.bound_socket)


@dataclass
class _ControllerState:
    generation: str
    queue: asyncio.Queue[bytes]
    connected: bool = False
    last_heartbeat: float = 0.0
    timeout_notified: bool = False
    expired: bool = False
    teardown_started: bool = False
    timeout_callback_finished: bool = False
    stream_closed: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_live(self, now: float) -> None:
        self.last_heartbeat = now
        self.timeout_notified = False
        self.expired = False
        self.teardown_started = False
        self.timeout_callback_finished = False


@dataclass
class LoopbackWebRuntime:
    config: LoopbackWebConfig
    start_session: SessionStarter
    session_ready: SessionReadyHandler | None
    handle_control: ControlHandler | None
    snapshot_provider: SnapshotProvider
    timeout_callback: GenerationCallback | None
    shutdown_callback: ShutdownCallback | None
    clock: Callable[[], float] = field(default_factory=lambda: asyncio.get_running_loop().time)
    app: web.Application = field(init=False)
    known_generations: set[str] = field(default_factory=set, init=False)
    active_controller: _ControllerState | None = field(default=None, init=False)
    pending_takeover_generation: str | None = field(default=None, init=False)
    closing: bool = field(default=False, init=False)
    _monitor_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.app = web.Application(
            middlewares=[self._security_headers_middleware, self._error_middleware]
        )
        self.app[RUNTIME_APP_KEY] = self
        self.app.on_startup.append(self._on_startup)
        self.app.on_shutdown.append(self._on_shutdown)
        self.app.on_cleanup.append(self._on_cleanup)
        self._add_routes()

    @property
    def capability_token(self) -> str:
        return self.config.capability_token

    def browser_url(self, authority: str) -> str:
        return f"{build_loopback_origin(int(authority.rsplit(':', 1)[1]))}/#{self.capability_token}"

    async def publish_event(self, event: Any) -> None:
        controller = self.active_controller
        if controller is None:
            return
        line = _encode_ndjson({"type": "event", "event": _normalize_public_document(event)})
        if controller.queue.full():
            self._replace_queue_with_resync(controller)
            return
        controller.queue.put_nowait(line)

    async def publish_snapshot(self) -> None:
        controller = self.active_controller
        if controller is None:
            return
        if controller.queue.full():
            self._replace_queue_with_resync(controller)
            return
        controller.queue.put_nowait(self._snapshot_line())

    async def _on_startup(self, app: web.Application) -> None:
        del app
        self._monitor_task = asyncio.create_task(self._monitor_controller())

    async def _on_shutdown(self, app: web.Application) -> None:
        del app
        self.closing = True
        controller = self.active_controller
        if controller is not None:
            controller.connected = False
            self._safe_queue_put(controller.queue, _encode_ndjson({"type": "server_stopping"}))
        if self.shutdown_callback is not None:
            await self._run_bounded_callback(
                self.shutdown_callback(),
                timeout_seconds=self.config.shutdown_callback_timeout_seconds,
            )

    async def _on_cleanup(self, app: web.Application) -> None:
        del app
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _monitor_controller(self) -> None:
        while True:
            await asyncio.sleep(min(self.config.keepalive_interval_seconds, 0.25))
            controller = self.active_controller
            if controller is None:
                continue
            now = self.clock()
            if now - controller.last_heartbeat <= self.config.heartbeat_timeout_seconds:
                continue
            if controller.timeout_notified:
                continue
            controller.timeout_notified = True
            controller.expired = True
            controller.teardown_started = True
            self._safe_queue_put(controller.queue, b"")
            if self.timeout_callback is not None:
                await self._run_bounded_callback(
                    self.timeout_callback(controller.generation),
                    timeout_seconds=self.config.timeout_callback_timeout_seconds,
                )
            controller.timeout_callback_finished = True

    def _add_routes(self) -> None:
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/assets/app.js", self._handle_app_js)
        self.app.router.add_get("/assets/styles.css", self._handle_styles_css)
        self.app.router.add_post("/v1/session", self._handle_start_session)
        self.app.router.add_post("/v1/session/{generation}/ready", self._handle_session_ready)
        self.app.router.add_post("/v1/control", self._handle_control_request)
        self.app.router.add_get("/v1/events", self._handle_events)
        self.app.router.add_get("/health", self._handle_health)

    @web.middleware
    async def _error_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except _PublicRouteError as error:
            return self._apply_security_headers(
                web.json_response(
                    {
                        "error": {
                            "code": error.code,
                            "message": redact_public_error_text(error.message),
                        }
                    },
                    status=error.status,
                )
            )
        except web.HTTPException as error:
            return self._apply_security_headers(error)

    @web.middleware
    async def _security_headers_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        response = await handler(request)
        return self._apply_security_headers(response)

    def _apply_security_headers(self, response: web.StreamResponse) -> web.StreamResponse:
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    async def _handle_index(self, request: web.Request) -> web.Response:
        self._assert_no_forwarding_headers(request)
        self._assert_exact_host(request)
        return web.Response(
            body=self._load_asset("index.html"),
            content_type="text/html",
        )

    async def _handle_app_js(self, request: web.Request) -> web.Response:
        self._assert_no_forwarding_headers(request)
        self._assert_exact_host(request)
        return web.Response(
            body=self._load_asset("assets/app.js"),
            content_type="application/javascript",
        )

    async def _handle_styles_css(self, request: web.Request) -> web.Response:
        self._assert_no_forwarding_headers(request)
        self._assert_exact_host(request)
        return web.Response(
            body=self._load_asset("assets/styles.css"),
            content_type="text/css",
        )

    async def _handle_health(self, request: web.Request) -> web.Response:
        self._assert_no_forwarding_headers(request)
        self._assert_exact_host(request)
        return web.json_response({"ok": True})

    async def _handle_start_session(self, request: web.Request) -> web.Response:
        self._assert_accepting()
        self._assert_mutation_request(request)
        offer_sdp = await self._read_bounded_text(
            request,
            expected_content_type="application/sdp",
            max_bytes=MAX_SDP_BYTES,
            label="offer SDP",
        )
        bootstrap = await _maybe_await(self.start_session(offer_sdp))
        self.known_generations.add(bootstrap.generation)
        return web.Response(
            text=bootstrap.answer_sdp,
            content_type="application/sdp",
            headers={"X-VoiceMode-Generation": bootstrap.generation},
        )

    async def _handle_session_ready(self, request: web.Request) -> web.Response:
        self._assert_accepting()
        self._assert_mutation_request(request)
        generation = _validate_generation(request.match_info["generation"])
        self._require_known_generation(generation)
        payload = {"ok": True, "generation": generation}
        if self.session_ready is not None:
            result = await _maybe_await(self.session_ready(generation))
            if result:
                payload["result"] = _normalize_public_document(result)
        return web.json_response(payload)

    async def _handle_control_request(self, request: web.Request) -> web.Response:
        self._assert_accepting()
        self._assert_mutation_request(request)
        document = await self._read_bounded_json(request, max_bytes=MAX_LOCAL_CONTROL_BYTES)
        if set(document) != {"action", "generation"}:
            raise _PublicRouteError(
                400,
                "invalid_control",
                "control payload must contain only action and generation",
            )
        action = document.get("action")
        generation = document.get("generation")
        if not isinstance(action, str) or action not in ALLOWED_CONTROL_ACTIONS:
            raise _PublicRouteError(400, "invalid_control", "control action is not allowed")
        if not isinstance(generation, str):
            raise _PublicRouteError(400, "invalid_control", "control generation is required")
        generation = _validate_generation(generation)
        self._require_known_generation(generation)

        if action == "heartbeat":
            self._record_heartbeat(generation)
            return web.json_response({"ok": True, "generation": generation, "action": action})

        if action == "takeover":
            self._authorize_takeover(generation)
            return web.json_response({"ok": True, "generation": generation, "action": action})

        payload = {"action": action, "generation": generation}
        result: Mapping[str, Any] | None = None
        if self.handle_control is not None:
            result = await _maybe_await(self.handle_control(action, generation, payload))
        response = {"ok": True, "generation": generation, "action": action}
        if result:
            response["result"] = _normalize_public_document(result)
        return web.json_response(response)

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        self._assert_accepting()
        self._assert_authenticated_request(request, require_origin=False)
        if request.query:
            raise _PublicRouteError(400, "invalid_request", "event stream does not accept query parameters")

        generation = _validate_generation(
            request.headers.get("X-VoiceMode-Generation", "")
        )
        self._require_known_generation(generation)
        controller = self._claim_controller(generation)
        controller.mark_live(self.clock())
        controller.stream_closed.clear()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": EVENT_STREAM_CONTENT_TYPE},
        )
        await response.prepare(request)
        controller.connected = True
        await response.write(self._snapshot_line())
        try:
            while self._stream_should_continue(controller):
                try:
                    line = await asyncio.wait_for(
                        controller.queue.get(),
                        timeout=self.config.keepalive_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    if not self._stream_should_continue(controller):
                        break
                    line = _encode_ndjson({"type": "keepalive"})
                if not self._stream_should_continue(controller):
                    break
                await response.write(line)
        except (asyncio.CancelledError, ConnectionError):
            raise
        except RuntimeError:
            pass
        finally:
            controller.connected = False
            controller.stream_closed.set()
            if self.active_controller is controller and not controller.expired:
                controller.mark_live(self.clock())
            try:
                await response.write_eof()
            except (ConnectionError, RuntimeError):
                pass
        return response

    def _claim_controller(self, generation: str) -> _ControllerState:
        now = self.clock()
        current = self.active_controller
        if current is None:
            controller = self._new_controller(generation, now)
            self.active_controller = controller
            return controller
        if current.generation == generation:
            if current.connected:
                raise _PublicRouteError(
                    409,
                    "controller_active",
                    "the current controller is already connected",
                )
            if current.expired:
                raise _PublicRouteError(
                    409,
                    "takeover_required",
                    "the current generation expired and must start a new session",
                )
            current.mark_live(now)
            return current
        if self._controller_teardown_in_progress(current):
            raise _PublicRouteError(
                409,
                "controller_active",
                "the current controller is still tearing down",
            )
        if self._controller_is_live(current, now):
            raise _PublicRouteError(
                409,
                "controller_active",
                "another generation still owns the local controller",
            )
        if self.pending_takeover_generation != generation:
            raise _PublicRouteError(
                409,
                "takeover_required",
                "a replacement page must request takeover after the old controller is dead",
            )
        controller = self._new_controller(generation, now)
        self.active_controller = controller
        self.pending_takeover_generation = None
        return controller

    def _authorize_takeover(self, generation: str) -> None:
        controller = self.active_controller
        now = self.clock()
        if controller is None:
            self.pending_takeover_generation = generation
            return
        if self._controller_teardown_in_progress(controller):
            raise _PublicRouteError(
                409,
                "controller_active",
                "the current controller is still tearing down",
            )
        if self._controller_is_live(controller, now):
            raise _PublicRouteError(
                409,
                "controller_active",
                "the current controller is still live",
            )
        self.pending_takeover_generation = generation

    def _record_heartbeat(self, generation: str) -> None:
        controller = self.active_controller
        if controller is None or controller.generation != generation:
            raise _PublicRouteError(
                409,
                "generation_mismatch",
                "heartbeat generation does not match the active controller",
            )
        controller.mark_live(self.clock())

    def _new_controller(self, generation: str, now: float) -> _ControllerState:
        controller = _ControllerState(
            generation=generation,
            queue=asyncio.Queue(maxsize=self.config.event_queue_size),
        )
        controller.stream_closed.set()
        controller.mark_live(now)
        return controller

    def _controller_is_live(self, controller: _ControllerState, now: float) -> bool:
        return (
            not controller.expired
            and now - controller.last_heartbeat <= self.config.heartbeat_timeout_seconds
        )

    def _replace_queue_with_resync(self, controller: _ControllerState) -> None:
        _clear_queue(controller.queue)
        self._safe_queue_put(controller.queue, _encode_ndjson({"type": "resync_required"}))
        self._safe_queue_put(controller.queue, self._snapshot_line())

    def _controller_teardown_in_progress(self, controller: _ControllerState) -> bool:
        return controller.teardown_started and not (
            controller.timeout_callback_finished and controller.stream_closed.is_set()
        )

    def _stream_should_continue(self, controller: _ControllerState) -> bool:
        return (
            not self.closing
            and self.active_controller is controller
            and not controller.expired
        )

    def _snapshot_line(self) -> bytes:
        return _encode_ndjson(
            {"type": "snapshot", "snapshot": _normalize_public_document(self.snapshot_provider())}
        )

    def _load_asset(self, asset_path: str) -> bytes:
        if asset_path not in STATIC_ASSETS:
            raise _PublicRouteError(404, "not_found", "static asset is not served")
        loader = self.config.asset_loader or _default_asset_loader
        try:
            body = loader(asset_path)
        except FileNotFoundError as error:
            raise _PublicRouteError(503, "asset_missing", "static asset is unavailable") from error
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("asset_loader must return bytes")
        return bytes(body)

    def _require_known_generation(self, generation: str) -> None:
        if generation not in self.known_generations:
            raise _PublicRouteError(404, "generation_unknown", "generation is not active")

    def _assert_accepting(self) -> None:
        if self.closing:
            raise _PublicRouteError(503, "server_stopping", "loopback server is stopping")

    def _assert_mutation_request(self, request: web.Request) -> None:
        self._assert_authenticated_request(request, require_origin=True)
        if request.query:
            raise _PublicRouteError(400, "invalid_request", "mutating routes do not accept query parameters")

    def _assert_authenticated_request(self, request: web.Request, *, require_origin: bool) -> None:
        self._assert_no_forwarding_headers(request)
        self._assert_exact_host(request)
        self._assert_authorization(request)
        if require_origin:
            self._assert_origin(request)

    def _assert_no_forwarding_headers(self, request: web.Request) -> None:
        try:
            validate_forwarded_headers(request.headers)
        except ValueError as error:
            raise _PublicRouteError(400, "forwarding_not_allowed", str(error))

    def _assert_exact_host(self, request: web.Request) -> None:
        observed_host = request.headers.get("Host")
        if not observed_host:
            raise _PublicRouteError(400, "invalid_host", "Host header is required")
        expected_authority = _expected_authority(request)
        if observed_host != expected_authority:
            raise _PublicRouteError(400, "invalid_host", "Host header does not match the loopback authority")

    def _assert_authorization(self, request: web.Request) -> None:
        header_value = request.headers.get("Authorization")
        if not header_value:
            raise _PublicRouteError(401, "unauthorized", "authorization is required")
        try:
            token = extract_bearer_token(header_value)
        except ValueError as error:
            raise _PublicRouteError(401, "unauthorized", str(error))
        try:
            authorized = compare_capability_token(self.capability_token, token)
        except ValueError as error:
            raise _PublicRouteError(401, "unauthorized", str(error))
        if not authorized:
            raise _PublicRouteError(401, "unauthorized", "authorization token is invalid")

    def _assert_origin(self, request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin:
            policy = self.config.internal_missing_origin_policy
            if policy is not None and policy(request):
                return
            raise _PublicRouteError(400, "invalid_origin", "Origin header is required")
        expected_origin = build_loopback_origin(_expected_port(request))
        if origin != expected_origin:
            raise _PublicRouteError(403, "invalid_origin", "Origin header does not match the loopback origin")

    async def _read_bounded_text(
        self,
        request: web.Request,
        *,
        expected_content_type: str,
        max_bytes: int,
        label: str,
    ) -> str:
        _assert_content_type(request, expected_content_type)
        raw = await _read_bounded_body(request, max_bytes=max_bytes)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _PublicRouteError(400, "invalid_encoding", f"{label} must be valid UTF-8") from error

    async def _read_bounded_json(
        self,
        request: web.Request,
        *,
        max_bytes: int,
    ) -> dict[str, Any]:
        _assert_content_type(request, "application/json")
        raw = await _read_bounded_body(request, max_bytes=max_bytes)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _PublicRouteError(400, "invalid_json", "request body must be valid JSON") from error
        if not isinstance(document, dict):
            raise _PublicRouteError(400, "invalid_json", "request body must be a JSON object")
        return document

    def _safe_queue_put(self, queue: asyncio.Queue[bytes], value: bytes) -> None:
        if queue.full():
            _clear_queue(queue)
        queue.put_nowait(value)

    async def _run_bounded_callback(self, value: Any, *, timeout_seconds: float) -> None:
        try:
            await asyncio.wait_for(_maybe_await(value), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return


class _PublicRouteError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def create_loopback_runtime(
    *,
    config: LoopbackWebConfig,
    start_session: SessionStarter,
    snapshot_provider: SnapshotProvider,
    session_ready: SessionReadyHandler | None = None,
    handle_control: ControlHandler | None = None,
    timeout_callback: GenerationCallback | None = None,
    shutdown_callback: ShutdownCallback | None = None,
    clock: Callable[[], float] | None = None,
) -> LoopbackWebRuntime:
    chosen_clock = clock
    if chosen_clock is None:
        monotonic = __import__("time").monotonic
        chosen_clock = monotonic
    return LoopbackWebRuntime(
        config=config,
        start_session=start_session,
        session_ready=session_ready,
        handle_control=handle_control,
        snapshot_provider=snapshot_provider,
        timeout_callback=timeout_callback,
        shutdown_callback=shutdown_callback,
        clock=chosen_clock,
    )


def make_loopback_config(
    *,
    capability_token: str | None = None,
    bind_host: str = LOOPBACK_HOST,
    bind_port: int = 0,
    event_queue_size: int = MAX_LOCAL_EVENT_BACKLOG,
    heartbeat_timeout_seconds: float = 10.0,
    keepalive_interval_seconds: float = 2.0,
    timeout_callback_timeout_seconds: float = 1.0,
    shutdown_callback_timeout_seconds: float = 5.0,
    internal_missing_origin_policy: MissingOriginPolicy | None = None,
    asset_loader: AssetLoader | None = None,
    bound_socket: Any | None = None,
) -> LoopbackWebConfig:
    return LoopbackWebConfig(
        capability_token=capability_token or generate_capability_token(),
        bind_host=bind_host,
        bind_port=bind_port,
        event_queue_size=event_queue_size,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        keepalive_interval_seconds=keepalive_interval_seconds,
        timeout_callback_timeout_seconds=timeout_callback_timeout_seconds,
        shutdown_callback_timeout_seconds=shutdown_callback_timeout_seconds,
        internal_missing_origin_policy=internal_missing_origin_policy,
        asset_loader=asset_loader,
        bound_socket=bound_socket,
    )


def _default_asset_loader(asset_path: str) -> bytes:
    resource = resources.files("voice_mode.templates").joinpath("realtime").joinpath(asset_path)
    return resource.read_bytes()


def _validate_generation(value: str) -> str:
    ensure_bounded_bytes(value, label="generation", max_bytes=128)
    if GENERATION_PATTERN.fullmatch(value) is None:
        raise _PublicRouteError(400, "invalid_generation", "generation contains unsafe characters")
    return value


def _expected_authority(request: web.Request) -> str:
    transport = request.transport
    if transport is None:
        raise _PublicRouteError(400, "invalid_host", "request transport is unavailable")
    sockname = transport.get_extra_info("sockname")
    if not isinstance(sockname, tuple) or len(sockname) < 2:
        raise _PublicRouteError(400, "invalid_host", "request transport is not bound to IPv4 loopback")
    host = validate_loopback_bind_host(str(sockname[0]))
    port = validate_port(int(sockname[1]))
    return f"{host}:{port}"


def _expected_port(request: web.Request) -> int:
    return int(_expected_authority(request).rsplit(":", 1)[1])


def _assert_content_type(request: web.Request, expected: str) -> None:
    actual = request.content_type
    if actual != expected:
        raise _PublicRouteError(
            415,
            "invalid_content_type",
            f"content type must be {expected}",
        )


async def _read_bounded_body(request: web.Request, *, max_bytes: int) -> bytes:
    content_length = request.content_length
    if content_length is not None and content_length > max_bytes:
        raise _PublicRouteError(413, "body_too_large", f"request body exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.content.iter_chunked(min(max_bytes, 4096)):
        total += len(chunk)
        if total > max_bytes:
            raise _PublicRouteError(413, "body_too_large", f"request body exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize_public_document(value: Any) -> Any:
    return json.loads(public_json_dumps(value))


def _encode_ndjson(value: Any) -> bytes:
    return (public_json_dumps(value) + "\n").encode("utf-8")


def _clear_queue(queue: asyncio.Queue[bytes]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
