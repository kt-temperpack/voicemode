import asyncio
import io
import json
from pathlib import Path

import pytest

from voice_mode.broker.cues import (
    CueDisposition,
    CueKind,
    CuePolicy,
    cue_for_event,
)
from voice_mode.broker.presentation import TerminalMode, TerminalPresenter
from voice_mode.broker.types import BrokerEvent


FIXTURE = Path(__file__).parent / "fixtures" / "broker" / "cue_terminal_events.json"


class Stream(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def test_only_named_reducer_events_authorize_cues():
    assert cue_for_event(BrokerEvent.LISTEN_STARTED) is CueKind.RISING
    assert cue_for_event(BrokerEvent.UTTERANCE_ENQUEUED) is CueKind.FALLING
    assert cue_for_event(BrokerEvent.BARGE_IN) is CueKind.INTERRUPTION
    assert cue_for_event(BrokerEvent.FAULT) is CueKind.FAILURE
    for event in (
        BrokerEvent.OPEN,
        BrokerEvent.ACTIVATE,
        BrokerEvent.UTTERANCE_DELIVERED,
        BrokerEvent.SUMMARY_ACCEPTED,
        BrokerEvent.PLAYBACK_FINISHED,
        BrokerEvent.FOLLOWUP_EXPIRED,
        BrokerEvent.CLOSE,
        BrokerEvent.RESET,
    ):
        assert cue_for_event(event) is None


@pytest.mark.asyncio
async def test_golden_event_sequence_has_exact_cues_and_privacy_safe_jsonl():
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    played = []

    def player(kind):
        async def play():
            played.append(kind.value)

        return play

    policy = CuePolicy({kind: player(kind) for kind in CueKind})
    stream = Stream()
    terminal = TerminalPresenter(stream, mode=TerminalMode.JSONL)

    for item in golden["events"]:
        event = BrokerEvent(item["event"])
        await policy.emit(event, item["id"])
        terminal.state(
            item["event"],
            item["state"],
            transcript=item.get("transcript"),
        )

    assert played == golden["expected_cues"]
    assert [json.loads(line) for line in stream.getvalue().splitlines()] == golden[
        "expected_jsonl"
    ]
    assert "private example" not in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_events_play_once_without_overlap():
    active = 0
    max_active = 0
    calls = []

    async def play():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append("play")
        await asyncio.sleep(0)
        active -= 1

    policy = CuePolicy({CueKind.RISING: play, CueKind.FALLING: play})
    first, duplicate, second = await asyncio.gather(
        policy.emit(BrokerEvent.LISTEN_STARTED, "same-event"),
        policy.emit(BrokerEvent.LISTEN_STARTED, "same-event"),
        policy.emit(BrokerEvent.UTTERANCE_ENQUEUED, "next-event"),
    )

    assert first == duplicate
    assert second.disposition is CueDisposition.PLAYED
    assert calls == ["play", "play"]
    assert max_active == 1


@pytest.mark.asyncio
async def test_failed_or_silent_event_is_terminal_and_never_replayed():
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        raise OSError("device disappeared")

    policy = CuePolicy({CueKind.FAILURE: fail})
    failed = await policy.emit(BrokerEvent.FAULT, "failure-1")
    retried = await policy.emit(BrokerEvent.FAULT, "failure-1")
    silent = await policy.emit(BrokerEvent.FOLLOWUP_EXPIRED, "timeout-1")

    assert failed == retried
    assert failed.disposition is CueDisposition.FAILED
    assert silent.disposition is CueDisposition.SUPPRESSED
    assert calls == 1


def test_terminal_modes_keep_one_tty_line_and_require_double_transcript_opt_in():
    tty = Stream(tty=True)
    terminal = TerminalPresenter(tty)
    terminal.state("listen_started", "listening")
    terminal.state("utterance_enqueued", "thinking")
    assert tty.getvalue().endswith("\r\x1b[2KVoiceMode: thinking")
    assert "\n" not in tty.getvalue()

    lines = Stream()
    line_terminal = TerminalPresenter(lines)
    line_terminal.state("followup_expired", "asleep")
    line_terminal.line("ready")
    assert lines.getvalue() == (
        "VoiceMode event=followup_expired state=asleep\nready\n"
    )

    denied = Stream()
    TerminalPresenter(
        denied,
        mode=TerminalMode.JSONL,
        include_transcript=True,
        transcript_authorized=False,
    ).state("utterance_enqueued", "thinking", transcript="secret")
    assert "secret" not in denied.getvalue()

    allowed = Stream()
    TerminalPresenter(
        allowed,
        mode=TerminalMode.JSONL,
        include_transcript=True,
        transcript_authorized=True,
    ).state("utterance_enqueued", "thinking", transcript="allowed")
    assert json.loads(allowed.getvalue())["transcript"] == "allowed"

    structured = Stream()
    structured_terminal = TerminalPresenter(structured, mode=TerminalMode.JSONL)
    structured_terminal.line("Codex: working")
    assert json.loads(structured.getvalue()) == {
        "event": "message",
        "message": "Codex: working",
        "version": 1,
    }
    assert "\x1b" not in structured.getvalue()
