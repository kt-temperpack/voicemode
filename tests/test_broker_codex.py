import json
import subprocess

import pytest

from voice_mode.broker.codex import CodexAdapter, CodexTurnError, _fallback_summary


def _runner_factory(stdout, payload, returncode=0, stderr=""):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        output_index = command.index("--output-last-message") + 1
        if payload is not None:
            with open(command[output_index], "w", encoding="utf-8") as handle:
                handle.write(payload)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    return calls, runner


def test_first_turn_starts_thread_and_second_resumes(tmp_path):
    stdout = json.dumps({"type": "thread.started", "thread_id": "thread-123"}) + "\n"
    payload = json.dumps({"display_text": "Full answer", "spoken_summary": "Short answer"})
    calls, runner = _runner_factory(stdout, payload)
    adapter = CodexAdapter(tmp_path, runner=runner)

    first = adapter.run_turn("first")
    second = adapter.run_turn("second")

    assert first.thread_id == second.thread_id == "thread-123"
    assert first.display_text == "Full answer"
    assert first.spoken_summary == "Short answer"
    assert calls[0][0][:2] == ["codex", "exec"]
    assert "resume" not in calls[0][0]
    assert calls[1][0][2] == "resume"
    assert calls[1][0][-2:] == ["thread-123", "second"]
    assert calls[0][1]["cwd"] == tmp_path.resolve()


def test_plain_text_fallback_and_word_limit(tmp_path):
    stdout = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
    raw = " ".join(f"word{i}" for i in range(60))
    _, runner = _runner_factory(stdout, raw)
    result = CodexAdapter(tmp_path, runner=runner).run_turn("hello")
    assert result.display_text == raw
    assert len(result.spoken_summary.rstrip("…").split()) == 45
    assert "`" not in _fallback_summary("Use `voicemode broker run`")


def test_events_are_forwarded_and_failures_are_bounded(tmp_path):
    events = []
    stdout = "not-json\n" + json.dumps({"type": "thread.started", "thread_id": "thread-1"})
    _, runner = _runner_factory(stdout, "")
    with pytest.raises(CodexTurnError, match="empty final response"):
        CodexAdapter(tmp_path, runner=runner, event_sink=events.append).run_turn("hello")
    assert events == [{"type": "thread.started", "thread_id": "thread-1"}]

    _, failed = _runner_factory("", None, returncode=7, stderr="broken")
    with pytest.raises(CodexTurnError, match="broken"):
        CodexAdapter(tmp_path, runner=failed).run_turn("hello")


def test_missing_thread_id_is_rejected(tmp_path):
    _, runner = _runner_factory("{}", json.dumps({"display_text": "x", "spoken_summary": "y"}))
    with pytest.raises(CodexTurnError, match="thread ID"):
        CodexAdapter(tmp_path, runner=runner).run_turn("hello")
