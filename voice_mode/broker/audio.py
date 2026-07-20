"""VoiceMode audio adapter used by the foreground broker loop."""

from __future__ import annotations

from typing import Awaitable, Callable


ConverseCallable = Callable[..., Awaitable[str]]


def _voice_text(result: str) -> str | None:
    marker = "Voice response:"
    if marker not in result:
        return None
    text = result.split(marker, 1)[1]
    if " | Timing:" in text:
        text = text.split(" | Timing:", 1)[0]
    return text.strip() or None


class VoiceAudio:
    """Thin, testable facade over the existing converse audio pipeline."""

    def __init__(
        self,
        *,
        voice: str,
        listen_duration: float,
        min_duration: float,
        converse_callable: ConverseCallable | None = None,
    ) -> None:
        self.voice = voice
        self.listen_duration = listen_duration
        self.min_duration = min_duration
        self._converse_callable = converse_callable

    def _converse(self) -> ConverseCallable:
        if self._converse_callable is None:
            from voice_mode.tools.converse import converse

            self._converse_callable = getattr(converse, "fn", converse)
        return self._converse_callable

    async def listen(self) -> str | None:
        result = await self._converse()(
            message="",
            wait_for_response=True,
            listen_duration_max=self.listen_duration,
            listen_duration_min=self.min_duration,
            voice=self.voice,
            skip_tts=True,
            skip_conch=True,
            metrics_level="minimal",
        )
        return _voice_text(result)
    async def speak(self, message: str) -> None:
        await self._converse()(
            message=message,
            wait_for_response=False,
            voice=self.voice,
            skip_conch=True,
            metrics_level="minimal",
        )

    async def exchange(self, message: str) -> str | None:
        result = await self._converse()(
            message=message,
            wait_for_response=True,
            listen_duration_max=self.listen_duration,
            listen_duration_min=self.min_duration,
            voice=self.voice,
            skip_conch=True,
            metrics_level="minimal",
        )
        return _voice_text(result)
