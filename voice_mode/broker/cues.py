"""Deterministic audio-cue policy for broker lifecycle events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum

from .types import BrokerEvent


class CueKind(str, Enum):
    RISING = "rising"
    FALLING = "falling"
    INTERRUPTION = "interruption"
    FAILURE = "failure"


class CueDisposition(str, Enum):
    PLAYED = "played"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class CueRecord:
    event_id: str
    event: BrokerEvent
    cue: CueKind | None
    disposition: CueDisposition


CuePlayer = Callable[[], Awaitable[object]]


_CUE_BY_EVENT: dict[BrokerEvent, CueKind] = {
    BrokerEvent.LISTEN_STARTED: CueKind.RISING,
    BrokerEvent.UTTERANCE_ENQUEUED: CueKind.FALLING,
    BrokerEvent.BARGE_IN: CueKind.INTERRUPTION,
    BrokerEvent.FAULT: CueKind.FAILURE,
}


def cue_for_event(event: BrokerEvent) -> CueKind | None:
    """Return the sole audible cue authorized for a reducer event."""

    return _CUE_BY_EVENT.get(event)


class CuePolicy:
    """Serialize cues and record a terminal disposition before allowing retries."""

    def __init__(self, players: Mapping[CueKind, CuePlayer]) -> None:
        self._players = dict(players)
        self._records: dict[str, CueRecord] = {}
        self._lock = asyncio.Lock()

    def record(self, event_id: str) -> CueRecord | None:
        return self._records.get(event_id)

    async def emit(self, event: BrokerEvent, event_id: str) -> CueRecord:
        if not event_id:
            raise ValueError("event_id must not be empty")
        async with self._lock:
            previous = self._records.get(event_id)
            if previous is not None:
                return previous

            cue = cue_for_event(event)
            player = self._players.get(cue) if cue is not None else None
            if player is None:
                record = CueRecord(
                    event_id, event, cue, CueDisposition.SUPPRESSED
                )
                self._records[event_id] = record
                return record

            # Reserve the event before external audio I/O. A cancellation or
            # failure is terminal because replaying a cue is more confusing
            # than losing one during device recovery.
            record = CueRecord(event_id, event, cue, CueDisposition.FAILED)
            self._records[event_id] = record
            try:
                await player()
            except asyncio.CancelledError:
                raise
            except Exception:
                return record

            record = CueRecord(event_id, event, cue, CueDisposition.PLAYED)
            self._records[event_id] = record
            return record
