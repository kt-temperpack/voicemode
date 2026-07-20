"""Privacy-safe append-only evidence journal for broker turns."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024


class JournalError(RuntimeError):
    """Base class for bounded journal failures."""


class JournalCorruption(JournalError):
    """A complete historical record is malformed or unsupported."""


@dataclass(frozen=True)
class JournalEvent:
    event: str
    request_id: str | None = None
    utterance_id: str | None = None
    broker_session_id: str | None = None
    repo_root: str | None = None
    adapter: str | None = None
    codex_thread_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    provider: str | None = None
    error_code: str | None = None
    transcript: str | None = None


@dataclass(frozen=True)
class JournalRecord:
    schema_version: int
    sequence: int
    occurred_at: str
    monotonic_duration_ms: int
    event: str
    request_id: str | None = None
    utterance_id: str | None = None
    broker_session_id: str | None = None
    repo_root: str | None = None
    adapter: str | None = None
    codex_thread_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    provider: str | None = None
    error_code: str | None = None
    transcript: str | None = None

    def payload(self, *, include_transcript: bool) -> dict[str, Any]:
        payload = asdict(self)
        if not include_transcript:
            payload.pop("transcript", None)
        return payload


AppendWriter = Callable[[Path, bytes], None]
WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
RetentionScheduler = Callable[[Callable[[], None]], None]


def stable_journal_path(directory: str | Path, session_id: str) -> Path:
    if not session_id:
        raise ValueError("session_id must not be empty")
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return Path(directory) / f"session-{digest}.jsonl"


def atomic_append(path: Path, payload: bytes) -> None:
    """Append one pre-buffered record with one O_APPEND write and fsync."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise JournalError("journal append was incomplete")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _background(task: Callable[[], None]) -> None:
    threading.Thread(
        target=task,
        name="voicemode-journal-retention",
        daemon=True,
    ).start()


class TurnJournal:
    """Append immutable records and expose deterministic recovery reads."""

    def __init__(
        self,
        directory: str | Path,
        session_id: str,
        *,
        include_transcript: bool = False,
        max_files: int = 32,
        max_total_bytes: int = 16 * 1024 * 1024,
        wall_clock: WallClock | None = None,
        monotonic_clock: MonotonicClock | None = None,
        writer: AppendWriter = atomic_append,
        retention_scheduler: RetentionScheduler = _background,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        if max_total_bytes < MAX_RECORD_BYTES:
            raise ValueError("max_total_bytes must fit one journal record")
        self.directory = Path(directory)
        self.session_id = session_id
        self.path = stable_journal_path(directory, session_id)
        self.include_transcript = include_transcript
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock or time.monotonic
        self._writer = writer
        self._retention_scheduler = retention_scheduler
        self._started = self._monotonic()
        existing = read_journal(self.path)
        self._sequence = existing[-1].sequence if existing else 0
        self._duration_offset_ms = (
            existing[-1].monotonic_duration_ms if existing else 0
        )
        self._lock = threading.Lock()
        self._retention_lock = threading.Lock()
        self._retention_running = False

    def append(self, event: JournalEvent) -> JournalRecord:
        if not event.event:
            raise ValueError("journal event must not be empty")
        with self._lock:
            self._sequence += 1
            now = self._wall_clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            elapsed_ms = self._duration_offset_ms + max(
                0, round((self._monotonic() - self._started) * 1000)
            )
            record = JournalRecord(
                schema_version=SCHEMA_VERSION,
                sequence=self._sequence,
                occurred_at=now.astimezone(timezone.utc).isoformat(),
                monotonic_duration_ms=elapsed_ms,
                **asdict(event),
            )
            encoded = (
                json.dumps(
                    record.payload(include_transcript=self.include_transcript),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            if len(encoded) > MAX_RECORD_BYTES:
                self._sequence -= 1
                raise JournalError("journal record exceeds the size limit")
            try:
                self._writer(self.path, encoded)
            except BaseException:
                self._sequence -= 1
                raise
        self._schedule_retention()
        return record

    def read(self) -> tuple[JournalRecord, ...]:
        return read_journal(self.path)

    def _schedule_retention(self) -> None:
        with self._retention_lock:
            if self._retention_running:
                return
            self._retention_running = True

        def run() -> None:
            try:
                self._enforce_retention()
            finally:
                with self._retention_lock:
                    self._retention_running = False

        try:
            self._retention_scheduler(run)
        except Exception:
            with self._retention_lock:
                self._retention_running = False

    def _enforce_retention(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            candidates = []
            for path in self.directory.glob("session-*.jsonl"):
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    candidates.append((metadata.st_mtime_ns, path.name, path, metadata.st_size))
            candidates.sort(
                key=lambda item: (item[2] == self.path, item[0], item[1]),
                reverse=True,
            )
            retained_bytes = 0
            for index, (_mtime, _name, path, size) in enumerate(candidates):
                keep = index < self.max_files and retained_bytes + size <= self.max_total_bytes
                if keep or path == self.path:
                    retained_bytes += size
                    continue
                path.unlink(missing_ok=True)
        except OSError:
            return


def read_journal(path: str | Path) -> tuple[JournalRecord, ...]:
    journal_path = Path(path)
    try:
        data = journal_path.read_bytes()
    except FileNotFoundError:
        return ()
    lines = data.splitlines(keepends=True)
    records = []
    previous_sequence = 0
    previous_duration = -1
    for index, line in enumerate(lines):
        is_tail = index == len(lines) - 1
        if is_tail and not line.endswith(b"\n"):
            break
        if len(line) > MAX_RECORD_BYTES:
            raise JournalCorruption(f"journal record {index + 1} exceeds the size limit")
        try:
            payload = json.loads(line)
            record = _parse_record(payload, index + 1)
            if record.sequence <= previous_sequence:
                raise JournalCorruption(
                    f"journal record {index + 1} has a non-increasing sequence"
                )
            if record.monotonic_duration_ms < previous_duration:
                raise JournalCorruption(
                    f"journal record {index + 1} has a decreasing duration"
                )
            records.append(record)
            previous_sequence = record.sequence
            previous_duration = record.monotonic_duration_ms
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as error:
            raise JournalCorruption(f"journal record {index + 1} is corrupt") from error
    return tuple(records)


def _parse_record(payload: Any, line_number: int) -> JournalRecord:
    if not isinstance(payload, dict):
        raise JournalCorruption(f"journal record {line_number} must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise JournalCorruption(f"journal record {line_number} has an unsupported schema")
    required = {
        "schema_version",
        "sequence",
        "occurred_at",
        "monotonic_duration_ms",
        "event",
    }
    if not required.issubset(payload):
        raise JournalCorruption(f"journal record {line_number} is incomplete")
    allowed = set(JournalRecord.__dataclass_fields__)
    if not set(payload).issubset(allowed):
        raise JournalCorruption(f"journal record {line_number} has unknown fields")
    if (
        not isinstance(payload["sequence"], int)
        or isinstance(payload["sequence"], bool)
        or payload["sequence"] < 1
    ):
        raise JournalCorruption(f"journal record {line_number} has an invalid sequence")
    duration = payload["monotonic_duration_ms"]
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        raise JournalCorruption(f"journal record {line_number} has an invalid duration")
    if not isinstance(payload["occurred_at"], str):
        raise JournalCorruption(f"journal record {line_number} has an invalid timestamp")
    try:
        occurred_at = datetime.fromisoformat(payload["occurred_at"])
    except ValueError as error:
        raise JournalCorruption(
            f"journal record {line_number} has an invalid timestamp"
        ) from error
    if occurred_at.tzinfo is None:
        raise JournalCorruption(f"journal record {line_number} has an invalid timestamp")
    if not isinstance(payload["event"], str) or not payload["event"]:
        raise JournalCorruption(f"journal record {line_number} has an invalid event")
    for name in allowed - required:
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            raise JournalCorruption(f"journal record {line_number} has an invalid {name}")
    return JournalRecord(**payload)
