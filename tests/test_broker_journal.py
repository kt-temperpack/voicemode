import json
import os
import stat
import subprocess
import sys
import threading
from datetime import datetime, timezone

import pytest

from voice_mode.broker.journal import (
    JournalCorruption,
    JournalEvent,
    JournalError,
    TurnJournal,
    _advisory_process_lock,
    _current_uid,
    read_journal,
    stable_journal_path,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def immediate(task):
    task()


def journal_at(tmp_path, session="broker-session", **kwargs):
    ticks = iter([10.0, 10.125, 10.250, 10.375, 10.5])
    return TurnJournal(
        tmp_path,
        session,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: next(ticks),
        retention_scheduler=immediate,
        **kwargs,
    )


def complete_event(**kwargs):
    return JournalEvent(
        event="state_transition",
        request_id="request-1",
        utterance_id="utterance-1",
        broker_session_id="broker-session",
        repo_root="/synthetic/repo",
        adapter="app-server",
        codex_thread_id="thread-1",
        from_state="accepted",
        to_state="dispatch_requested",
        provider="local-whisper",
        transcript="private spoken words",
        **kwargs,
    )


def test_default_append_is_deterministic_and_transcript_free(tmp_path):
    journal = journal_at(tmp_path)

    first = journal.append(complete_event())
    second = journal.append(
        JournalEvent(
            event="dispatch_confirmed",
            request_id="request-1",
            broker_session_id="broker-session",
            adapter="app-server",
            codex_thread_id="thread-1",
            to_state="dispatched",
        )
    )

    raw = journal.path.read_text(encoding="utf-8")
    assert "private spoken words" not in raw
    assert '"transcript"' not in raw
    assert "audio" not in raw
    assert first.sequence == 1
    assert first.monotonic_duration_ms == 125
    assert second.sequence == 2
    assert second.monotonic_duration_ms == 250
    assert journal.read() == read_journal(journal.path)
    assert [record.event for record in journal.read()] == [
        "state_transition",
        "dispatch_confirmed",
    ]


def test_transcript_requires_explicit_independent_opt_in(tmp_path):
    journal = journal_at(tmp_path, include_transcript=True)
    journal.append(complete_event())

    assert "private spoken words" in journal.path.read_text(encoding="utf-8")
    assert journal.read()[0].transcript == "private spoken words"


def test_realtime_fields_round_trip_without_changing_schema(tmp_path):
    journal = journal_at(tmp_path)

    record = journal.append(
        JournalEvent(
            event="realtime.response_claimed",
            mode="realtime",
            request_id="request-1",
            job_id="job_fixture_0001",
            realtime_item_id="item_fixture_0001",
            response_id="response_fixture_0001",
            function_call_id="call_fixture_0001",
            worker_delivery_id="delivery_fixture_0001",
            transcript="hidden transcript",
        )
    )

    assert record.schema_version == 1
    loaded = journal.read()[0]
    assert loaded.mode == "realtime"
    assert loaded.job_id == "job_fixture_0001"
    assert loaded.realtime_item_id == "item_fixture_0001"
    assert loaded.response_id == "response_fixture_0001"
    assert loaded.function_call_id == "call_fixture_0001"
    assert loaded.worker_delivery_id == "delivery_fixture_0001"
    assert loaded.transcript is None
    assert "hidden transcript" not in journal.path.read_text(encoding="utf-8")


def test_journal_directory_and_file_are_private(tmp_path):
    directory = tmp_path / "journal"
    journal = journal_at(directory)
    journal.append(JournalEvent(event="accepted"))

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600


def test_torn_final_line_is_skipped_without_losing_complete_history(tmp_path):
    journal = journal_at(tmp_path)
    journal.append(complete_event())
    with journal.path.open("ab") as stream:
        stream.write(b'{"schema_version":1,"sequence":2')

    records = journal.read()

    assert len(records) == 1
    assert records[0].request_id == "request-1"


def test_corruption_before_tail_is_reported(tmp_path):
    path = stable_journal_path(tmp_path, "session")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"broken":true}\n{"also":"torn"')

    with pytest.raises(JournalCorruption, match="record 1"):
        read_journal(path)


def test_unsupported_schema_and_unknown_fields_fail_closed(tmp_path):
    path = stable_journal_path(tmp_path, "session")
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "schema_version": 2,
        "sequence": 1,
        "occurred_at": NOW.isoformat(),
        "monotonic_duration_ms": 0,
        "event": "accepted",
    }
    path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruption, match="unsupported schema"):
        read_journal(path)

    base["schema_version"] = 1
    base["secret"] = "unexpected"
    path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruption, match="unknown fields"):
        read_journal(path)


def test_invalid_types_and_non_increasing_sequence_fail_closed(tmp_path):
    path = stable_journal_path(tmp_path, "session")
    base = {
        "schema_version": 1,
        "sequence": "one",
        "occurred_at": NOW.isoformat(),
        "monotonic_duration_ms": 0,
        "event": "accepted",
    }
    path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruption, match="invalid sequence"):
        read_journal(path)

    base["sequence"] = 1
    path.write_text((json.dumps(base) + "\n") * 2, encoding="utf-8")
    with pytest.raises(JournalCorruption, match="non-increasing sequence"):
        read_journal(path)


def test_failed_append_does_not_schedule_retention_or_consume_sequence(tmp_path):
    scheduled = []
    writes = []

    def writer(_path, payload):
        writes.append(payload)
        if len(writes) == 1:
            raise OSError("disk full")

    journal = TurnJournal(
        tmp_path,
        "session",
        wall_clock=lambda: NOW,
        monotonic_clock=iter([0.0, 0.1, 0.2]).__next__,
        writer=writer,
        retention_scheduler=scheduled.append,
    )
    with pytest.raises(OSError, match="disk full"):
        journal.append(JournalEvent(event="accepted"))

    record = journal.append(JournalEvent(event="accepted"))
    assert record.sequence == 1
    assert len(scheduled) == 1


def test_retention_scheduler_failure_does_not_invalidate_successful_append(tmp_path):
    journal = TurnJournal(
        tmp_path,
        "session",
        wall_clock=lambda: NOW,
        monotonic_clock=iter([0.0, 0.1]).__next__,
        retention_scheduler=lambda _task: (_ for _ in ()).throw(
            RuntimeError("scheduler unavailable")
        ),
    )

    record = journal.append(JournalEvent(event="accepted"))

    assert record.sequence == 1
    assert journal.read()[0].event == "accepted"


def test_record_size_is_bounded_before_writer_is_called(tmp_path):
    writes = []
    journal = journal_at(
        tmp_path,
        include_transcript=True,
        writer=lambda path, payload: writes.append((path, payload)),
    )

    with pytest.raises(JournalError, match="size limit"):
        journal.append(JournalEvent(event="accepted", transcript="x" * 70_000))
    assert writes == []


def test_retention_bounds_file_count_and_keeps_current_session(tmp_path):
    old_one = stable_journal_path(tmp_path, "old-one")
    old_two = stable_journal_path(tmp_path, "old-two")
    old_one.parent.mkdir(parents=True, exist_ok=True)
    old_one.write_bytes(b"old\n")
    old_two.write_bytes(b"older\n")
    journal = journal_at(tmp_path, session="current", max_files=2)

    journal.append(JournalEvent(event="accepted"))

    remaining = tuple(tmp_path.glob("session-*.jsonl"))
    assert len(remaining) == 2
    assert journal.path in remaining


def test_retention_skips_a_locked_peer_journal(tmp_path):
    old = stable_journal_path(tmp_path, "old")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b'{"event":"accepted","monotonic_duration_ms":0,"occurred_at":"2026-07-19T12:00:00+00:00","schema_version":1,"sequence":1}\n')
    journal = journal_at(tmp_path, session="current", max_files=1)

    with _advisory_process_lock(old):
        journal.append(JournalEvent(event="accepted"))

    remaining = tuple(sorted(path.name for path in tmp_path.glob("session-*.jsonl")))
    assert len(remaining) == 2
    assert old.name in remaining
    assert journal.path.name in remaining


def test_concurrent_append_and_read_produce_stable_complete_records(tmp_path):
    counter = iter(float(index) / 100 for index in range(100))
    journal = TurnJournal(
        tmp_path,
        "session",
        wall_clock=lambda: NOW,
        monotonic_clock=counter.__next__,
        retention_scheduler=lambda _task: None,
    )

    threads = [
        threading.Thread(
            target=journal.append,
            args=(JournalEvent(event="accepted", request_id=f"request-{index}"),),
        )
        for index in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    first = journal.read()
    second = journal.read()
    assert first == second
    assert len(first) == 20
    assert [record.sequence for record in first] == list(range(1, 21))


def test_reopened_session_continues_sequence_and_duration(tmp_path):
    first = TurnJournal(
        tmp_path,
        "session",
        wall_clock=lambda: NOW,
        monotonic_clock=iter([10.0, 10.5]).__next__,
        retention_scheduler=lambda _task: None,
    )
    first.append(JournalEvent(event="accepted"))
    reopened = TurnJournal(
        tmp_path,
        "session",
        wall_clock=lambda: NOW,
        monotonic_clock=iter([20.0, 20.25]).__next__,
        retention_scheduler=lambda _task: None,
    )

    record = reopened.append(JournalEvent(event="dispatch_claimed"))

    assert record.sequence == 2
    assert record.monotonic_duration_ms == 750
    assert [item.sequence for item in reopened.read()] == [1, 2]


def test_parallel_processes_allocate_unique_sequences(tmp_path):
    script = """
from datetime import datetime, timezone
from pathlib import Path

from voice_mode.broker.journal import JournalEvent, TurnJournal

directory = Path(sys.argv[1])
session = sys.argv[2]
request_id = sys.argv[3]
journal = TurnJournal(
    directory,
    session,
    wall_clock=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    retention_scheduler=lambda _task: None,
)
journal.append(JournalEvent(event="accepted", request_id=request_id))
"""
    for index in range(4):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys\n" + script,
                str(tmp_path),
                "shared",
                f"request-{index}",
            ],
            check=True,
            cwd=tmp_path,
        )

    path = stable_journal_path(tmp_path, "shared")
    assert [record.sequence for record in read_journal(path)] == [1, 2, 3, 4]


def test_symlink_target_is_refused(tmp_path):
    target = stable_journal_path(tmp_path, "session")
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    target.symlink_to(real)
    with pytest.raises(JournalError, match="regular file"):
        journal_at(tmp_path, session="session")


def test_symlink_target_is_refused_on_read(tmp_path):
    journal = journal_at(tmp_path, session="session")
    journal.append(JournalEvent(event="accepted"))
    target = stable_journal_path(tmp_path, "session")
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("", encoding="utf-8")
    target.unlink()
    target.symlink_to(replacement)

    with pytest.raises(JournalError, match="regular file"):
        journal.read()


def test_over_permissive_directory_and_file_are_refused(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o755)
    journal = journal_at(directory)

    with pytest.raises(JournalError, match="directory permissions are too broad"):
        journal.append(JournalEvent(event="accepted"))

    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    path = stable_journal_path(secure, "session")
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(JournalError, match="permissions are too broad"):
        journal_at(secure, session="session")


def test_wrong_owner_target_is_refused(tmp_path, monkeypatch):
    fake_uid = (_current_uid() or 0) + 1
    monkeypatch.setattr("voice_mode.broker.journal._current_uid", lambda: fake_uid)
    journal = journal_at(tmp_path)

    with pytest.raises(JournalError, match="owned by the current user"):
        journal.append(JournalEvent(event="accepted"))
