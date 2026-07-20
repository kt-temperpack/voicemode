"""Foreground wake/listen/Codex/speak loop."""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Callable

from .audio import PersistentVoiceAudio
from .codex import CodexAdapter, CodexTurnError
from .runtime import BrokerRuntime
from .server import create_broker


SLEEP_PHRASES = {"go to sleep", "stop listening", "sleep"}
EXIT_PHRASES = {"exit voice mode", "quit voice mode", "goodbye", "shut down"}


def wake_command(text: str, wake_phrase: str) -> str | None:
    match = re.match(
        rf"^\s*{re.escape(wake_phrase)}(?:\s*[,;:\-]\s*|\s+|$)(.*?)\s*$",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def control_intent(text: str) -> str | None:
    normalized = " ".join(text.lower().strip().rstrip(".!?").split())
    if normalized in SLEEP_PHRASES:
        return "sleep"
    if normalized in EXIT_PHRASES:
        return "exit"
    return None


class HandsFreeLoop:
    def __init__(
        self,
        *,
        repo_root: Path,
        runtime: BrokerRuntime,
        audio: PersistentVoiceAudio,
        wake_phrase: str,
        codex_factory: Callable[[Path], CodexAdapter],
        display: Callable[[str], None] = print,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.runtime = runtime
        self.audio = audio
        self.wake_phrase = wake_phrase
        self.codex_factory = codex_factory
        self.display = display
        self._codex: CodexAdapter | None = None
        self._shown_codex_thread: str | None = None

    def _close_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        try:
            self.runtime.close_session(session_id)
        except Exception:
            pass

    async def run(self) -> None:
        await self.audio.speak(
            f"Hands-free Codex is ready. Say {self.wake_phrase}, followed by your request."
        )
        self.display(f"Hands-free Codex ready in {self.repo_root}")
        self.display(f"Wake phrase: {self.wake_phrase}")
        session_id: str | None = None
        pending: str | None = None

        while not self.runtime.snapshot().shutting_down:
            if session_id is None:
                heard = await self._listen_safely()
                if not heard:
                    continue
                self.display(f"You: {heard}")
                pending = wake_command(heard, self.wake_phrase)
                if pending is None:
                    continue
                if not pending:
                    await self.audio.speak("I'm listening.")
                    pending = await self._listen_safely()
                    if not pending:
                        continue
                    self.display(f"You: {pending}")
                if control_intent(pending) == "exit":
                    await self.audio.speak("Hands-free Codex is stopping.")
                    self.runtime.begin_shutdown()
                    return
                session = self.runtime.open_session("handsfree", str(self.repo_root))
                session_id = session.session_id
                self.runtime.activate(session_id)
                if self._codex is None:
                    self._codex = self.codex_factory(self.repo_root)

            assert pending is not None and session_id is not None and self._codex is not None
            intent = control_intent(pending)
            if intent == "sleep":
                self._close_session(session_id)
                session_id = None
                pending = None
                await self.audio.speak(f"Going to sleep. Say {self.wake_phrase} when you need me.")
                continue
            if intent == "exit":
                self._close_session(session_id)
                await self.audio.speak("Hands-free Codex is stopping.")
                self.runtime.begin_shutdown()
                return

            self.runtime.enqueue_utterance(session_id, pending)
            utterance = self.runtime.wait_for_turn(session_id, 0)
            assert utterance is not None
            self.display("Codex: working…")
            try:
                turn = await asyncio.to_thread(self._codex.run_turn, utterance.text)
            except CodexTurnError as error:
                self.display(f"Codex error: {error}")
                self._close_session(session_id)
                session_id = None
                pending = None
                await self.audio.speak("Codex hit an error. I went back to sleep.")
                continue

            if self.runtime.snapshot().shutting_down:
                return

            self.runtime.attach_codex_thread(session_id, turn.thread_id)
            if turn.thread_id != self._shown_codex_thread:
                self._shown_codex_thread = turn.thread_id
                self.display(f"Codex thread: {turn.thread_id}")
                self.display(f"Open it later: codex resume {turn.thread_id}")
            self.runtime.accept_summary(session_id, turn.spoken_summary)
            self.display(f"\nCodex:\n{turn.display_text}\n")
            await self.audio.speak(turn.spoken_summary)
            pending = await self._listen_safely()
            if self.runtime.snapshot().shutting_down:
                return
            self.runtime.finish_playback(session_id)
            if not pending:
                self._close_session(session_id)
                session_id = None
                self.display(f"Follow-up window closed. Say {self.wake_phrase} to wake me.")
                continue
            self.display(f"You: {pending}")
            self.runtime.start_listening(session_id)

        self._close_session(session_id)

    async def _listen_safely(self) -> str | None:
        try:
            return await self.audio.listen()
        except Exception as error:
            self.display(f"Voice input error: {error}")
            return None


def run_handsfree_broker(
    socket_path: Path,
    *,
    repo_root: Path,
    wake_phrase: str,
    voice: str,
    listen_duration: float,
    min_duration: float,
    codex_executable: str,
    codex_sandbox: str,
    codex_model: str,
    codex_reasoning_effort: str,
    silence_threshold_ms: int,
) -> None:
    runtime, _dispatcher, server = create_broker(socket_path, audio_enabled=True)
    server.start()
    server_thread = threading.Thread(target=server.serve_forever, name="voicemode-broker", daemon=True)
    server_thread.start()
    audio = PersistentVoiceAudio(
        voice=voice,
        listen_duration=listen_duration,
        min_duration=min_duration,
        silence_threshold_ms=silence_threshold_ms,
    )
    audio.start()
    loop = HandsFreeLoop(
        repo_root=repo_root,
        runtime=runtime,
        audio=audio,
        wake_phrase=wake_phrase,
        codex_factory=lambda root: CodexAdapter(
            root,
            executable=codex_executable,
            sandbox=codex_sandbox,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
        ),
    )
    try:
        asyncio.run(loop.run())
    finally:
        audio.close()
        runtime.begin_shutdown()
        server.stop()
        server_thread.join(timeout=2)
