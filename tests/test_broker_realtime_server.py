import asyncio
import socket
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from voice_mode.broker.realtime.web import (
    LoopbackWebConfig,
    SessionBootstrap,
    create_loopback_runtime,
    make_loopback_config,
)


ASSETS = {
    "index.html": b"<!doctype html><title>VoiceMode</title><div id='app'></div>",
    "assets/app.js": b"console.log('vm');",
    "assets/styles.css": b"body{background:#000;}",
}


@dataclass
class Harness:
    offer_sdps: list[str] = field(default_factory=list)
    ready_calls: list[str] = field(default_factory=list)
    control_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    timeout_calls: list[str] = field(default_factory=list)
    shutdown_calls: int = 0
    snapshot_version: int = 0

    def asset_loader(self, asset_path: str) -> bytes:
        return ASSETS[asset_path]

    def start_session(self, offer_sdp: str) -> SessionBootstrap:
        self.offer_sdps.append(offer_sdp)
        return SessionBootstrap(generation=f"gen-{len(self.offer_sdps)}", answer_sdp="v=0\nanswer")

    def session_ready(self, generation: str) -> dict[str, Any]:
        self.ready_calls.append(generation)
        return {"ready": True}

    def handle_control(self, action: str, generation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.control_calls.append((action, generation, payload))
        return {"accepted": action}

    def snapshot(self) -> dict[str, Any]:
        self.snapshot_version += 1
        return {"phase": "ready", "snapshot_version": self.snapshot_version}

    async def on_timeout(self, generation: str) -> None:
        self.timeout_calls.append(generation)

    async def on_shutdown(self) -> None:
        self.shutdown_calls += 1


async def _start_client(
    harness: Harness,
    *,
    config: LoopbackWebConfig | None = None,
    clock=None,
) -> tuple[TestClient, Any]:
    runtime = create_loopback_runtime(
        config=config or make_loopback_config(
            capability_token="A" * 32,
            asset_loader=harness.asset_loader,
            heartbeat_timeout_seconds=0.1,
            keepalive_interval_seconds=0.05,
            event_queue_size=2,
        ),
        start_session=harness.start_session,
        session_ready=harness.session_ready,
        handle_control=harness.handle_control,
        snapshot_provider=harness.snapshot,
        timeout_callback=harness.on_timeout,
        shutdown_callback=harness.on_shutdown,
        clock=clock,
    )
    server = TestServer(runtime.app, host="127.0.0.1", port=0)
    client = TestClient(server)
    await client.start_server()
    return client, runtime


def _auth_headers(client: TestClient, token: str, *, include_origin: bool = True, host: str | None = None) -> dict[str, str]:
    authority = host or client.make_url("/").authority
    headers = {
        "Authorization": f"Bearer {token}",
        "Host": authority,
    }
    if include_origin:
        headers["Origin"] = f"http://{authority}"
    return headers


@pytest.mark.asyncio
async def test_app_construction_rejects_non_loopback_bind_targets() -> None:
    with pytest.raises(ValueError):
        make_loopback_config(capability_token="A" * 32, bind_host="0.0.0.0")

    loopback_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    external_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        loopback_socket.bind(("127.0.0.1", 0))
        external_socket.bind(("0.0.0.0", 0))
        assert make_loopback_config(
            capability_token="A" * 32,
            bound_socket=loopback_socket,
        )
        with pytest.raises(ValueError):
            make_loopback_config(
                capability_token="A" * 32,
                bound_socket=external_socket,
            )
    finally:
        loopback_socket.close()
        external_socket.close()


@pytest.mark.asyncio
async def test_public_routes_serve_exact_assets_and_security_headers() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    del runtime
    try:
        root = await client.get("/", headers={"Host": client.make_url("/").authority})
        assert root.status == 200
        assert await root.text() == ASSETS["index.html"].decode()
        assert root.headers["Content-Security-Policy"]
        assert root.headers["Cache-Control"] == "no-store"
        assert root.headers["Referrer-Policy"] == "no-referrer"

        app_js = await client.get("/assets/app.js", headers={"Host": client.make_url("/").authority})
        assert app_js.status == 200
        assert await app_js.text() == ASSETS["assets/app.js"].decode()

        styles = await client.get(
            "/assets/styles.css",
            headers={"Host": client.make_url('/').authority},
        )
        assert styles.status == 200
        assert await styles.text() == ASSETS["assets/styles.css"].decode()

        traversal = await client.get(
            "/assets/%2E%2E/%2E%2E/pyproject.toml",
            headers={"Host": client.make_url('/').authority},
        )
        assert traversal.status == 404

        health = await client.get("/health", headers={"Host": client.make_url('/').authority})
        assert health.status == 200
        assert await health.json() == {"ok": True}
        assert "generation" not in await health.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authenticated_mutations_require_exact_host_origin_and_bearer() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    del runtime
    try:
        authority = client.make_url("/").authority
        good_headers = _auth_headers(client, "A" * 32)

        bad_host = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={**good_headers, "Host": "localhost:9999", "Content-Type": "application/sdp"},
        )
        assert bad_host.status == 400
        assert (await bad_host.json())["error"]["code"] == "invalid_host"

        bad_origin = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={
                **good_headers,
                "Origin": "http://localhost:9999",
                "Content-Type": "application/sdp",
            },
        )
        assert bad_origin.status == 403
        assert (await bad_origin.json())["error"]["code"] == "invalid_origin"

        missing_bearer = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={"Host": authority, "Origin": f"http://{authority}", "Content-Type": "application/sdp"},
        )
        assert missing_bearer.status == 401

        forwarded = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={
                **good_headers,
                "Content-Type": "application/sdp",
                "X-Forwarded-For": "1.2.3.4",
            },
        )
        assert forwarded.status == 400
        assert (await forwarded.json())["error"]["code"] == "forwarding_not_allowed"

        query = await client.post(
            "/v1/control?token=nope",
            json={"action": "heartbeat", "generation": "gen-1"},
            headers=good_headers,
        )
        assert query.status == 400
        assert (await query.json())["error"]["code"] == "invalid_request"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_and_control_routes_enforce_limits_and_closed_schema() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    try:
        good_headers = _auth_headers(client, "A" * 32)

        wrong_type = await client.post(
            "/v1/session",
            data='{"bad":true}',
            headers={**good_headers, "Content-Type": "application/json"},
        )
        assert wrong_type.status == 415

        oversized = await client.post(
            "/v1/session",
            data="x" * (64 * 1024 + 1),
            headers={**good_headers, "Content-Type": "application/sdp"},
        )
        assert oversized.status == 413

        created = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={**good_headers, "Content-Type": "application/sdp"},
        )
        assert created.status == 200
        assert created.headers["X-VoiceMode-Generation"] == "gen-1"
        assert await created.text() == "v=0\nanswer"
        assert harness.offer_sdps == ["v=0\noffer"]

        ready = await client.post("/v1/session/gen-1/ready", headers=good_headers)
        assert ready.status == 200
        assert (await ready.json())["result"] == {"ready": True}
        assert harness.ready_calls == ["gen-1"]

        invalid_control = await client.post(
            "/v1/control",
            json={"action": "launch_missiles", "generation": "gen-1"},
            headers=good_headers,
        )
        assert invalid_control.status == 400

        extra_field = await client.post(
            "/v1/control",
            json={"action": "stop", "generation": "gen-1", "unexpected": True},
            headers=good_headers,
        )
        assert extra_field.status == 400
        assert (await extra_field.json())["error"]["code"] == "invalid_control"

        generation_unknown = await client.post(
            "/v1/control",
            json={"action": "heartbeat", "generation": "gen-missing"},
            headers=good_headers,
        )
        assert generation_unknown.status == 404

        stop = await client.post(
            "/v1/control",
            json={"action": "stop", "generation": "gen-1"},
            headers=good_headers,
        )
        assert stop.status == 200
        assert (await stop.json())["result"] == {"accepted": "stop"}
        assert harness.control_calls[0][0] == "stop"

        assert runtime.capability_token == "A" * 32
        assert "A" * 32 not in ASSETS["index.html"].decode()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_event_stream_requires_authentication_takeover_and_heartbeat() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    try:
        headers = _auth_headers(client, "A" * 32)
        created = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={**headers, "Content-Type": "application/sdp"},
        )
        assert created.status == 200

        event_headers = {**headers, "X-VoiceMode-Generation": "gen-1"}
        stream = await client.get("/v1/events", headers=event_headers)
        assert stream.status == 200
        first_line = await asyncio.wait_for(stream.content.readline(), timeout=1)
        assert b"snapshot" in first_line

        duplicate = await client.get("/v1/events", headers=event_headers)
        assert duplicate.status == 409
        assert (await duplicate.json())["error"]["code"] == "controller_active"

        heartbeat = await client.post(
            "/v1/control",
            json={"action": "heartbeat", "generation": "gen-1"},
            headers=headers,
        )
        assert heartbeat.status == 200

        await runtime.publish_event({"kind": "status", "text": "running"})
        event_line = await asyncio.wait_for(stream.content.readline(), timeout=1)
        assert b"running" in event_line

        await asyncio.sleep(0.15)
        assert harness.timeout_calls == ["gen-1"]

        second = await client.post(
            "/v1/session",
            data="v=0\noffer-2",
            headers={**headers, "Content-Type": "application/sdp"},
        )
        assert second.status == 200

        blocked = await client.get(
            "/v1/events",
            headers={**headers, "X-VoiceMode-Generation": "gen-2"},
        )
        assert blocked.status == 409
        assert (await blocked.json())["error"]["code"] == "takeover_required"

        takeover = await client.post(
            "/v1/control",
            json={"action": "takeover", "generation": "gen-2"},
            headers=headers,
        )
        assert takeover.status == 200

        replacement = await client.get(
            "/v1/events",
            headers={**headers, "X-VoiceMode-Generation": "gen-2"},
        )
        assert replacement.status == 200
        line = await asyncio.wait_for(replacement.content.readline(), timeout=1)
        assert b"snapshot" in line
        stale_tail = await asyncio.wait_for(stream.content.read(), timeout=1)
        assert b"snapshot" not in stale_tail
        assert stream.content.at_eof()
        await stream.release()
        await replacement.release()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_errors_keep_security_and_no_store_headers() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    del runtime
    try:
        response = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={
                "Authorization": "Bearer wrong-token",
                "Host": client.make_url("/").authority,
                "Origin": f"http://{client.make_url('/').authority}",
                "Content-Type": "application/sdp",
            },
        )
        assert response.status == 401
        assert response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_resyncs_when_event_queue_overflows() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    try:
        headers = _auth_headers(client, "A" * 32)
        created = await client.post(
            "/v1/session",
            data="v=0\noffer",
            headers={**headers, "Content-Type": "application/sdp"},
        )
        assert created.status == 200

        controller = runtime._new_controller("gen-1", runtime.clock())
        runtime.active_controller = controller
        await runtime.publish_event({"idx": 1})
        await runtime.publish_event({"idx": 2})
        await runtime.publish_event({"idx": 3})

        first = controller.queue.get_nowait()
        second = controller.queue.get_nowait()
        assert b"resync_required" in first
        assert b"snapshot" in second
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_shutdown_calls_injected_teardown_once() -> None:
    harness = Harness()
    client, runtime = await _start_client(harness)
    del runtime
    await client.close()
    assert harness.shutdown_calls == 1


@pytest.mark.asyncio
async def test_route_inventory_is_closed() -> None:
    runtime = create_loopback_runtime(
        config=make_loopback_config(capability_token="A" * 32, asset_loader=lambda path: ASSETS[path]),
        start_session=lambda offer: SessionBootstrap(generation="gen-1", answer_sdp=offer),
        snapshot_provider=lambda: {"phase": "ready"},
    )
    resource_paths = sorted(
        route.resource.canonical
        for route in runtime.app.router.routes()
        if route.resource is not None
    )
    assert resource_paths == [
        "/",
        "/",
        "/assets/app.js",
        "/assets/app.js",
        "/assets/styles.css",
        "/assets/styles.css",
        "/health",
        "/health",
        "/v1/control",
        "/v1/events",
        "/v1/events",
        "/v1/session",
        "/v1/session/{generation}/ready",
    ]
