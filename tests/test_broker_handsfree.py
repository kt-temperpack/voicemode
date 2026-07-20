import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_mode.broker import handsfree as handsfree_module
from voice_mode.broker import (
    HostApprovalRequest,
    HostCapability,
    HostCompletion,
    HostEvent,
    HostEventKind,
    HostProbe,
    HostTurn,
    HostTurnState,
)
from voice_mode.broker.handsfree import (
    AppServerCodexRunner,
    EXEC_NEW_THREAD_ID,
    HandsFreeLoop,
    HostTransportLost,
    control_intent,
    wake_command,
)
from voice_mode.broker.codex import CodexTurn
from voice_mode.broker.runtime import BrokerRuntime
from voice_mode.broker.activation import ActivationBus, ActivationEvent, ActivationKind


class FakeAudio:
    def __init__(self, listens):
        self.listens = iter(listens)
        self.spoken = []
        self.cues = []

    async def listen(self):
        return next(self.listens)

    async def speak(self, message):
        self.spoken.append(message)

    async def cue_listening(self):
        self.cues.append("listening")

    async def cue_submitted(self):
        self.cues.append("submitted")

    def begin_push_to_talk(self):
        self.cues.append("ptt-press")

    def release_push_to_talk(self):
        self.cues.append("ptt-release")


class FakeCodex:
    thread_id = "codex-1"

    def __init__(self):
        self.prompts = []

    def run_turn(self, prompt, *, request_id=None, on_started=None):
        self.prompts.append(prompt)
        if on_started is not None:
            on_started()
        if self.thread_id is None:
            self.thread_id = "codex-1"
        return CodexTurn(
            display_text=f"full:{prompt}",
            spoken_summary=f"full:{prompt}",
            thread_id=self.thread_id,
            request_id=request_id,
            host_turn_id=f"host-{request_id}",
            completed_at=datetime.now(timezone.utc),
        )


class FakeAppServerAdapter:
    def __init__(self):
        self.sink = None
        self.calls = []

    def subscribe(self, sink):
        self.sink = sink
        return lambda: setattr(self, "sink", None)

    def start_turn(self, *, request_id, thread_id, prompt):
        self.calls.append(("start", request_id, thread_id, prompt))
        assert self.sink is not None
        approval = HostApprovalRequest(
            request_id, thread_id, "turn-1", "approval-1", "Needs review"
        )
        self.sink(
            HostEvent(
                HostEventKind.APPROVAL_REQUIRED,
                request_id,
                thread_id,
                "turn-1",
                approval=approval,
            )
        )
        completion = HostCompletion(
            request_id,
            thread_id,
            "turn-1",
            "Native response.",
            "Native response.",
            datetime.now(timezone.utc),
        )
        self.sink(
            HostEvent(
                HostEventKind.TURN_COMPLETED,
                request_id,
                thread_id,
                "turn-1",
                completion=completion,
            )
        )
        return HostTurn(request_id, thread_id, "turn-1", HostTurnState.STARTED)

    def steer_turn(self, **kwargs):
        self.calls.append(("steer", kwargs))

    def interrupt_turn(self, **kwargs):
        self.calls.append(("interrupt", kwargs))


def test_host_runner_surfaces_transport_loss_for_evidence_based_recovery():
    adapter = FakeAppServerAdapter()

    def lose_transport(**kwargs):
        adapter.calls.append(("start", kwargs))
        adapter.sink(
            HostEvent(
                HostEventKind.TRANSPORT_LOST,
                None,
                None,
                error="socket closed",
            )
        )
        return HostTurn(
            kwargs["request_id"], kwargs["thread_id"], "turn-1", HostTurnState.STARTED
        )

    adapter.start_turn = lose_transport
    runner = AppServerCodexRunner(adapter, "thread-1", turn_timeout=1)

    with pytest.raises(HostTransportLost, match="socket closed"):
        runner.run_turn("inspect", request_id="request-1")


def test_wake_and_control_parsing_is_strict():
    assert wake_command("Computer, check tests", "Computer") == "check tests"
    assert wake_command("Hey Computer, check tests", "Computer") == "check tests"
    assert wake_command("hey, computer", "Computer") == ""
    assert wake_command("Hey computer.", "Computer") == ""
    assert wake_command("Hey computer! Check tests", "Computer") == "Check tests"
    assert wake_command("\u200bHey computer—check tests", "Computer") == "check tests"
    assert wake_command("Hey computer… check tests", "Computer") == "check tests"
    assert wake_command("computer", "Computer") == ""
    assert wake_command("computerized", "Computer") is None
    assert wake_command("my computer is slow", "Computer") is None
    assert control_intent("Go to sleep.") == "sleep"
    assert control_intent("exit voice mode") == "exit"
    assert control_intent("Nice.") == "ack"
    assert control_intent("Thank you!") == "ack"
    assert control_intent("please go to sleep after this") is None


def test_app_server_runner_waits_for_one_native_completion_and_surfaces_approval():
    adapter = FakeAppServerAdapter()
    displayed = []
    runner = AppServerCodexRunner(adapter, "thread-1", display=displayed.append)

    result = runner.run_turn("inspect")

    assert result.display_text == "Native response."
    assert result.thread_id == "thread-1"
    assert len(adapter.calls) == 1
    assert "thread=thread-1" in displayed[0]
    assert "approval=approval-1" in displayed[0]
    runner.close()
    assert adapter.sink is None


def test_auto_adapter_uses_native_app_server_and_exact_thread(monkeypatch, tmp_path):
    calls = []

    class FakeTransport:
        def close(self):
            calls.append("transport-close")

    class FakeHost:
        def probe(self):
            return HostProbe(
                "app-server", True, frozenset({HostCapability.START_TURN})
            )

        def subscribe(self, _sink):
            return lambda: None

        def close(self):
            calls.append("host-close")

    class FakeLoop:
        def __init__(self, **kwargs):
            calls.append(("loop", kwargs))

        async def run(self):
            return None

        def close(self):
            calls.append("loop-close")

        def interrupt(self):
            calls.append("loop-interrupt")

    class FakeServer:
        def start(self):
            calls.append("server-start")

        def serve_forever(self):
            return None

        def stop(self):
            calls.append("server-stop")

    class FakeAudioLifecycle:
        def __init__(self, **kwargs):
            calls.append(("audio", kwargs))

        def start(self):
            calls.append("audio-start")

        async def speak(self, message):
            calls.append(("speak", message))

        def close(self):
            calls.append("audio-close")

    transport = FakeTransport()
    host = FakeHost()
    monkeypatch.setattr(
        handsfree_module.AppServerTransport,
        "start_process",
        lambda **kwargs: transport,
    )
    monkeypatch.setattr(
        handsfree_module.AppServerHostAdapter,
        "connect",
        lambda selected: host,
    )

    def fake_select(adapter, repo_root, **kwargs):
        calls.append(("select", adapter, repo_root, kwargs))
        return SimpleNamespace(thread=SimpleNamespace(thread_id="current-thread"))

    monkeypatch.setattr(handsfree_module, "select_thread", fake_select)
    monkeypatch.setattr(handsfree_module, "HandsFreeLoop", FakeLoop)
    monkeypatch.setattr(
        handsfree_module,
        "create_broker",
        lambda *_args, **_kwargs: (
            BrokerRuntime(),
            SimpleNamespace(interrupt_callback=None),
            FakeServer(),
        ),
    )
    monkeypatch.setattr(handsfree_module, "PersistentVoiceAudio", FakeAudioLifecycle)

    def close_coroutine(coroutine):
        calls.append("loop-run")
        coroutine.close()

    monkeypatch.setattr(handsfree_module.asyncio, "run", close_coroutine)

    handsfree_module.run_handsfree_broker(
        tmp_path / "broker.sock",
        repo_root=tmp_path,
        wake_phrase="Computer",
        voice="am_michael",
        voice_speed=1.25,
        listen_duration=60,
        min_duration=2,
        codex_executable="codex",
        codex_sandbox="workspace-write",
        codex_model="model",
        codex_reasoning_effort="low",
        silence_threshold_ms=1000,
        codex_adapter="auto",
        codex_thread_id="current-thread",
    )

    selection = next(call for call in calls if isinstance(call, tuple) and call[0] == "select")
    assert selection[3]["explicit_thread_id"] == "current-thread"
    loop_call = next(call for call in calls if isinstance(call, tuple) and call[0] == "loop")
    assert loop_call[1]["host_adapter"] is host
    assert loop_call[1]["thread_id"] == "current-thread"
    assert calls.index("loop-close") < calls.index("host-close")


@pytest.mark.asyncio
async def test_loop_ignores_ambient_then_reuses_codex_for_followup(tmp_path):
    audio = FakeAudio([])
    codex = FakeCodex()
    codex.thread_id = None
    displayed = []
    runtime = BrokerRuntime()
    loop = HandsFreeLoop(
        repo_root=tmp_path,
        runtime=runtime,
        audio=audio,
        wake_phrase="Computer",
        host_adapter=handsfree_module.ExecCodexAdapter(codex),
        thread_id=EXEC_NEW_THREAD_ID,
        display=displayed.append,
    )

    # A third wake exits after proving the sleep transition.
    audio.listens = iter(
        [
            "ambient speech",
            "Computer, inspect the repo",
            "run focused tests",
            "nice",
            "Computer, exit voice mode",
        ]
    )
    await loop.run()

    assert codex.prompts == ["inspect the repo", "run focused tests"]
    assert any("full:inspect the repo" in line for line in displayed)
    assert "Codex thread: codex-1" in displayed
    assert "Open it later: codex resume codex-1" in displayed
    assert runtime.snapshot().shutting_down is True
    assert runtime.snapshot().session is None
    fixture = Path(__file__).parent / "fixtures" / "broker" / "handsfree_cues.json"
    assert audio.cues == json.loads(fixture.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_loop_announces_exact_thread_before_first_dispatch(tmp_path):
    audio = FakeAudio(
        [
            "Computer, inspect the repo",
            "nice",
            "Computer, exit voice mode",
        ]
    )
    codex = FakeCodex()
    codex.thread_id = "current-thread"
    displayed = []
    runtime = BrokerRuntime()
    loop = HandsFreeLoop(
        repo_root=tmp_path,
        runtime=runtime,
        audio=audio,
        wake_phrase="Computer",
        host_adapter=handsfree_module.ExecCodexAdapter(codex),
        thread_id="current-thread",
        display=displayed.append,
    )

    await loop.run()

    adapter_index = displayed.index("Codex adapter: exec")
    thread_index = displayed.index("Codex thread: current-thread")
    ready_index = displayed.index("Hands-free Codex ready")
    dispatch_index = displayed.index("Codex: working…")
    assert adapter_index < thread_index < ready_index < dispatch_index
    assert any("separate Codex child" in line for line in displayed)
    assert displayed.count("Codex thread: current-thread") == 1
    assert codex.prompts == ["inspect the repo"]
    assert runtime.snapshot().shutting_down is True


@pytest.mark.asyncio
async def test_push_to_talk_bypasses_wake_phrase_without_touching_codex_adapter(tmp_path):
    audio = FakeAudio(["inspect directly", "nice", "Computer, exit voice mode"])
    codex = FakeCodex()
    codex.thread_id = "current-thread"
    bus = ActivationBus()
    loop = HandsFreeLoop(
        repo_root=tmp_path,
        runtime=BrokerRuntime(),
        audio=audio,
        wake_phrase="Computer",
        host_adapter=handsfree_module.ExecCodexAdapter(codex),
        thread_id="current-thread",
        activation_bus=bus,
        display=lambda _message: None,
    )
    bus.publish(
        ActivationEvent.now(ActivationKind.PUSH_TO_TALK_PRESS, "test-hotkey")
    )
    bus.publish(
        ActivationEvent.now(ActivationKind.PUSH_TO_TALK_RELEASE, "test-hotkey")
    )

    await loop.run()

    assert codex.prompts == ["inspect directly"]
    assert audio.cues[:2] == ["ptt-press", "ptt-release"]


@pytest.mark.asyncio
async def test_transcription_failure_does_not_crash_loop(tmp_path):
    class FailingAudio(FakeAudio):
        async def listen(self):
            raise TimeoutError("local STT timed out")

    displayed = []
    loop = HandsFreeLoop(
        repo_root=tmp_path,
        runtime=BrokerRuntime(),
        audio=FailingAudio([]),
        wake_phrase="Computer",
        host_adapter=handsfree_module.ExecCodexAdapter(FakeCodex()),
        thread_id=EXEC_NEW_THREAD_ID,
        display=displayed.append,
    )

    assert await loop._listen_safely() is None
    assert displayed == ["Voice input error: local STT timed out"]
