"""Deterministic repository-aware host thread selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..types import HostErrorKind, HostThreadSummary
from .base import HostAdapter, HostAdapterError


class ThreadSelectionSource(str, Enum):
    EXPLICIT = "explicit"
    REGISTERED = "registered"
    AUTODETECTED = "autodetected"
    CREATED = "created"


@dataclass(frozen=True)
class ThreadSelection:
    adapter: str
    thread: HostThreadSummary
    source: ThreadSelectionSource
    alternatives: tuple[str, ...] = ()
    reason: str | None = None


def canonical_repo_root(repo_root: str | Path) -> str:
    return str(Path(repo_root).resolve(strict=False))


def select_thread(
    adapter: HostAdapter,
    repo_root: str | Path,
    *,
    explicit_thread_id: str | None = None,
    registered_thread_id: str | None = None,
    new_thread: bool = False,
    label: str | None = None,
) -> ThreadSelection:
    """Select an explicit thread or safely create one instead of guessing."""

    if new_thread and (explicit_thread_id or registered_thread_id):
        raise HostAdapterError(
            HostErrorKind.AMBIGUOUS,
            "select_thread",
            "new thread cannot be combined with an explicit or registered thread",
        )
    probe = adapter.probe()
    if not probe.available:
        raise HostAdapterError(
            HostErrorKind.UNAVAILABLE,
            "select_thread",
            probe.reason or f"host adapter {probe.adapter} is unavailable",
        )
    canonical_root = canonical_repo_root(repo_root)

    if explicit_thread_id:
        return ThreadSelection(
            probe.adapter,
            adapter.attach_thread(explicit_thread_id),
            ThreadSelectionSource.EXPLICIT,
        )
    if registered_thread_id:
        return ThreadSelection(
            probe.adapter,
            adapter.attach_thread(registered_thread_id),
            ThreadSelectionSource.REGISTERED,
        )
    if new_thread:
        return ThreadSelection(
            probe.adapter,
            adapter.create_thread(canonical_root, label or f"VoiceMode: {Path(canonical_root).name}"),
            ThreadSelectionSource.CREATED,
            reason="new thread requested",
        )

    matching = tuple(
        thread
        for thread in adapter.list_threads(canonical_root)
        if canonical_repo_root(thread.repo_root) == canonical_root
    )
    active = tuple(thread for thread in matching if thread.active)
    if len(active) == 1:
        return ThreadSelection(
            probe.adapter,
            adapter.attach_thread(active[0].thread_id),
            ThreadSelectionSource.AUTODETECTED,
        )

    alternatives = tuple(sorted(thread.thread_id for thread in matching))
    if not active:
        reason = "no active repository thread was safe to attach"
    else:
        reason = "multiple active repository threads were found; none was guessed"
    created = adapter.create_thread(
        canonical_root, label or f"VoiceMode: {Path(canonical_root).name}"
    )
    return ThreadSelection(
        probe.adapter,
        created,
        ThreadSelectionSource.CREATED,
        alternatives,
        reason,
    )
