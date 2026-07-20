import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_mode.broker.handsfree import HandsFreeLoop, control_intent, wake_command
from voice_mode.broker.runtime import BrokerRuntime


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


class FakeCodex:
    thread_id = "codex-1"

    def __init__(self):
        self.prompts = []

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(
            display_text=f"full:{prompt}",
            spoken_summary=f"short:{prompt}",
            thread_id=self.thread_id,
        )


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


@pytest.mark.asyncio
async def test_loop_ignores_ambient_then_reuses_codex_for_followup(tmp_path):
    audio = FakeAudio([])
    codex = FakeCodex()
    displayed = []
    runtime = BrokerRuntime()
    loop = HandsFreeLoop(
        repo_root=tmp_path,
        runtime=runtime,
        audio=audio,
        wake_phrase="Computer",
        codex_factory=lambda _root: codex,
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
        codex_factory=lambda _root: codex,
        initial_thread_id="current-thread",
        adapter_kind="exec",
        display=displayed.append,
    )

    await loop.run()

    adapter_index = displayed.index("Codex adapter: exec")
    thread_index = displayed.index("Codex thread: current-thread")
    dispatch_index = displayed.index("Codex: working…")
    assert adapter_index < thread_index < dispatch_index
    assert displayed.count("Codex thread: current-thread") == 1
    assert codex.prompts == ["inspect the repo"]
    assert runtime.snapshot().shutting_down is True


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
        codex_factory=lambda _root: FakeCodex(),
        display=displayed.append,
    )

    assert await loop._listen_safely() is None
    assert displayed == ["Voice input error: local STT timed out"]
