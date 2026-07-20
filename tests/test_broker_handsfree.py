from types import SimpleNamespace

import pytest

from voice_mode.broker.handsfree import HandsFreeLoop, control_intent, wake_command
from voice_mode.broker.runtime import BrokerRuntime


class FakeAudio:
    def __init__(self, listens):
        self.listens = iter(listens)
        self.spoken = []

    async def listen(self):
        return next(self.listens)

    async def speak(self, message):
        self.spoken.append(message)

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
    assert wake_command("computer", "Computer") == ""
    assert wake_command("computerized", "Computer") is None
    assert wake_command("my computer is slow", "Computer") is None
    assert control_intent("Go to sleep.") == "sleep"
    assert control_intent("exit voice mode") == "exit"
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
            "go to sleep",
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
