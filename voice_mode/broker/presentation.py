"""At-most-once visible and spoken presentation for canonical responses."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .runtime import BrokerRuntime
from .types import CanonicalResponse


Display = Callable[[str], None]
Speak = Callable[[str], Awaitable[None]]


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
