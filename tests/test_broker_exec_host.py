import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from voice_mode.broker import (
    HostCapability,
    HostDisposition,
    HostErrorKind,
    HostEventKind,
)
from voice_mode.broker.codex import CodexTurn
from voice_mode.broker.hosts import ExecCodexAdapter, HostAdapterError


class FakeCodex:
    def __init__(self, root):
        self.repo_root = Path(root)
        self.thread_id = None
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancelled = False

    def run_turn(self, prompt, *, request_id=None):
        self.started.set()
        self.release.wait(1)
        return CodexTurn(
            f"answer:{prompt}",
            f"answer:{prompt}",
            self.thread_id,
            request_id,
            f"exec-{request_id}",
            datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

    def cancel_active(self):
        self.cancelled = True
        self.release.set()
        return True


def test_exec_probe_advertises_only_supported_fallback_capabilities(tmp_path):
    adapter = ExecCodexAdapter(FakeCodex(tmp_path))

    probe = adapter.probe()

    assert probe.adapter == "exec"
    assert probe.capabilities == frozenset(
        {
            HostCapability.ATTACH_THREAD,
            HostCapability.START_TURN,
            HostCapability.INTERRUPT_TURN,
            HostCapability.SUBSCRIBE_EVENTS,
            HostCapability.QUERY_DISPOSITION,
        }
    )
    assert "separate Codex child" in probe.reason


@pytest.mark.parametrize("operation", ["list_threads", "read_thread", "create_thread"])
def test_unsupported_operations_fail_before_dispatch(tmp_path, operation):
    codex = FakeCodex(tmp_path)
    adapter = ExecCodexAdapter(codex)

    with pytest.raises(HostAdapterError) as caught:
        if operation == "list_threads":
            adapter.list_threads()
        elif operation == "read_thread":
            adapter.read_thread("thread-1")
        else:
            adapter.create_thread(str(tmp_path), "new")

    assert caught.value.kind is HostErrorKind.UNSUPPORTED
    assert codex.started.is_set() is False


def test_exec_turn_emits_one_correlated_completion(tmp_path):
    codex = FakeCodex(tmp_path)
    adapter = ExecCodexAdapter(codex)
    adapter.attach_thread("thread-1")
    events = []
    adapter.subscribe(events.append)

    turn = adapter.start_turn(
        request_id="request-1", thread_id="thread-1", prompt="inspect"
    )
    assert codex.started.wait(1)
    codex.release.set()
    adapter._worker.join(1)

    assert turn.host_turn_id == "exec-request-1"
    assert [event.kind for event in events] == [HostEventKind.TURN_COMPLETED]
    assert events[0].completion.display_text == "answer:inspect"
    assert adapter.query_disposition(
        request_id="request-1", thread_id="thread-1"
    ) is HostDisposition.COMPLETED


def test_exec_interrupt_terminates_active_process_group(tmp_path):
    codex = FakeCodex(tmp_path)
    adapter = ExecCodexAdapter(codex)
    adapter.attach_thread("thread-1")
    turn = adapter.start_turn(
        request_id="request-1", thread_id="thread-1", prompt="long work"
    )
    assert codex.started.wait(1)

    interrupted = adapter.interrupt_turn(
        request_id="request-1",
        thread_id="thread-1",
        host_turn_id=turn.host_turn_id,
    )

    assert interrupted.state.value == "cancelled"
    assert codex.cancelled is True
    adapter._worker.join(1)


def test_exec_rejects_thread_mismatch_before_start(tmp_path):
    codex = FakeCodex(tmp_path)
    adapter = ExecCodexAdapter(codex)
    adapter.attach_thread("thread-1")

    with pytest.raises(HostAdapterError) as caught:
        adapter.start_turn(
            request_id="request-1", thread_id="thread-2", prompt="inspect"
        )

    assert caught.value.kind is HostErrorKind.AMBIGUOUS
    assert codex.started.is_set() is False
