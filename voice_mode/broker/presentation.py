"""At-most-once visible and spoken presentation for canonical responses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TextIO

from .runtime import BrokerRuntime
from .types import CanonicalResponse


Display = Callable[[str], None]
Speak = Callable[[str], Awaitable[None]]


class TerminalMode(str, Enum):
    AUTO = "auto"
    TTY = "tty"
    LINES = "lines"
    JSONL = "jsonl"


class TerminalPresenter:
    """Render broker state without turning transcripts into an output side channel."""

    def __init__(
        self,
        stream: TextIO,
        *,
        mode: TerminalMode = TerminalMode.AUTO,
        include_transcript: bool = False,
        transcript_authorized: bool = False,
    ) -> None:
        self.stream = stream
        self.mode = (
            TerminalMode.TTY
            if mode is TerminalMode.AUTO and stream.isatty()
            else TerminalMode.LINES
            if mode is TerminalMode.AUTO
            else mode
        )
        self.include_transcript = include_transcript and transcript_authorized

    def _write_json(self, payload: dict[str, object]) -> None:
        self.stream.write(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        )

    def line(self, message: str) -> None:
        """Append one ordinary line without corrupting the live TTY state row."""

        if self.mode is TerminalMode.TTY:
            self.stream.write(f"\r\x1b[2K{message}\n")
        elif self.mode is TerminalMode.JSONL:
            self._write_json({"event": "message", "message": message, "version": 1})
        else:
            self.stream.write(f"{message}\n")
        self.stream.flush()

    def state(
        self,
        event: str,
        state: str,
        *,
        transcript: str | None = None,
    ) -> None:
        if self.mode is TerminalMode.TTY:
            self.stream.write(f"\r\x1b[2KVoiceMode: {state}")
        elif self.mode is TerminalMode.JSONL:
            payload = {"event": event, "state": state, "version": 1}
            if self.include_transcript and transcript:
                payload["transcript"] = transcript
            self._write_json(payload)
        else:
            self.stream.write(f"VoiceMode event={event} state={state}\n")
        self.stream.flush()


class Presenter:
    """Own the only paths which may render or speak a final response."""

    def __init__(self, runtime: BrokerRuntime, *, display: Display, speak: Speak) -> None:
        self.runtime = runtime
        self.display = display
        self.speak = speak

    def show_final(self, response: CanonicalResponse) -> bool:
        self.runtime.accept_host_completion(response)
        if not self.runtime.mark_visible_presented(response.request_id):
            return False
        self.display(response.display_text)
        return True

    async def speak_final(self, response: CanonicalResponse) -> bool:
        self.runtime.accept_host_completion(response)
        if not response.spoken_text.strip():
            return False
        if not self.runtime.mark_tts_started(response.request_id):
            return False
        try:
            await self.speak(response.spoken_text)
        except asyncio.CancelledError:
            self.runtime.finish_tts(response.request_id, failed=True)
            raise
        except Exception:
            self.runtime.finish_tts(response.request_id, failed=True)
            return False
        self.runtime.finish_tts(response.request_id)
        return True

    async def present(self, response: CanonicalResponse) -> tuple[bool, bool]:
        visible = self.show_final(response)
        spoken = await self.speak_final(response)
        return visible, spoken
