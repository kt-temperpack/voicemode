import asyncio
import threading
from datetime import datetime, timezone

import pytest

from voice_mode.broker import BrokerError, CanonicalResponse
from voice_mode.broker.journal import TurnJournal
from voice_mode.broker.presentation import Presenter
from voice_mode.broker.runtime import BrokerRuntime


def immediate(task):
    task()


def prepared_turn(tmp_path, *, spoken_text="Complete answer", journal=None):
    identities = iter(["session-1", "utterance-1", "request-1"])
    runtime = BrokerRuntime(
        uuid_factory=lambda: next(identities),
        utc_now=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        journal=journal,
    )
    session = runtime.open_session("thread-1", str(tmp_path))
    runtime.activate(session.session_id)
    envelope = runtime.accept_turn(
        session.session_id,
        "private prompt",
        host_adapter="app-server",
        host_thread_id="thread-1",
    )
    assert runtime.claim_dispatch(envelope.request_id).should_dispatch
    runtime.confirm_dispatch(envelope.request_id)
    response = CanonicalResponse(
        schema_version=1,
        request_id=envelope.request_id,
        thread_id="thread-1",
        display_text="Complete answer with details.",
        spoken_text=spoken_text,
        host_turn_id="host-turn-1",
        completed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    return runtime, response


@pytest.mark.asyncio
async def test_duplicate_present_calls_write_and_speak_exactly_once(tmp_path):
    displayed = []
    spoken = []

    async def speak(text):
        spoken.append(text)

    runtime, response = prepared_turn(tmp_path)
    presenter = Presenter(runtime, display=displayed.append, speak=speak)

    first = await presenter.present(response)
    second = await presenter.present(response)

    assert first == (True, True)
    assert second == (False, False)
    assert displayed == ["Complete answer with details."]
    assert spoken == ["Complete answer"]


def test_concurrent_visible_claims_write_once(tmp_path):
    displayed = []

    async def speak(_text):
        raise AssertionError("speech is not part of this test")

    runtime, response = prepared_turn(tmp_path)
    presenter = Presenter(runtime, display=displayed.append, speak=speak)
    barrier = threading.Barrier(9)
    outcomes = []

    def show():
        barrier.wait()
        outcomes.append(presenter.show_final(response))

    threads = [threading.Thread(target=show) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=1)

    assert outcomes.count(True) == 1
    assert displayed == ["Complete answer with details."]


@pytest.mark.asyncio
async def test_tts_failure_is_terminal_and_not_retried(tmp_path):
    attempts = []

    async def fail(text):
        attempts.append(text)
        raise RuntimeError("speaker unavailable")

    runtime, response = prepared_turn(tmp_path)
    presenter = Presenter(runtime, display=lambda _text: None, speak=fail)

    assert await presenter.present(response) == (True, False)
    assert await presenter.speak_final(response) is False
    assert attempts == ["Complete answer"]


@pytest.mark.asyncio
async def test_cancellation_during_tts_is_terminal_and_propagates(tmp_path):
    started = asyncio.Event()

    async def block(_text):
        started.set()
        await asyncio.Future()

    runtime, response = prepared_turn(tmp_path)
    presenter = Presenter(runtime, display=lambda _text: None, speak=block)
    presenter.show_final(response)
    task = asyncio.create_task(presenter.speak_final(response))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await presenter.speak_final(response) is False


def test_failed_visible_write_is_claimed_before_output_and_not_retried(tmp_path):
    attempts = []

    def fail(text):
        attempts.append(text)
        raise OSError("terminal closed")

    async def speak(_text):
        return None

    runtime, response = prepared_turn(tmp_path)
    presenter = Presenter(runtime, display=fail, speak=speak)

    with pytest.raises(OSError, match="terminal closed"):
        presenter.show_final(response)
    assert presenter.show_final(response) is False
    assert attempts == ["Complete answer with details."]


@pytest.mark.asyncio
async def test_empty_spoken_text_completes_without_playback(tmp_path):
    spoken = []

    async def speak(text):
        spoken.append(text)

    runtime, response = prepared_turn(tmp_path, spoken_text="")
    presenter = Presenter(runtime, display=lambda _text: None, speak=speak)

    assert await presenter.present(response) == (True, False)
    assert spoken == []


def test_conflicting_duplicate_completion_is_rejected(tmp_path):
    async def speak(_text):
        return None

    runtime, response = prepared_turn(tmp_path)
    presenter = Presenter(runtime, display=lambda _text: None, speak=speak)
    presenter.show_final(response)
    conflicting = CanonicalResponse(
        **{**response.__dict__, "display_text": "Different answer."}
    )

    with pytest.raises(BrokerError, match="conflicts"):
        presenter.show_final(conflicting)


def test_unrelated_spoken_answer_is_rejected_before_any_output(tmp_path):
    displayed = []

    async def speak(_text):
        return None

    runtime, response = prepared_turn(tmp_path, spoken_text="A second answer")
    presenter = Presenter(runtime, display=displayed.append, speak=speak)

    with pytest.raises(BrokerError, match="spoken excerpt is not contained"):
        presenter.show_final(response)
    assert displayed == []


@pytest.mark.asyncio
async def test_presentation_journal_contains_no_response_content(tmp_path):
    journal = TurnJournal(
        tmp_path,
        "broker-session",
        retention_scheduler=immediate,
    )

    async def speak(_text):
        return None

    runtime, response = prepared_turn(tmp_path, journal=journal)
    presenter = Presenter(runtime, display=lambda _text: None, speak=speak)
    await presenter.present(response)

    payload = journal.path.read_text()
    assert "Complete answer" not in payload
    assert "Short answer" not in payload
    assert [record.event for record in journal.read()] == [
        "turn_accepted",
        "dispatch_claimed",
        "dispatch_confirmed",
        "host_completed",
        "visible_presented",
        "tts_started",
        "tts_completed",
    ]
