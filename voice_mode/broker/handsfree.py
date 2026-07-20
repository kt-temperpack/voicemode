"""Foreground wake/listen/Codex/speak loop."""

from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
from pathlib import Path
from typing import Callable

from .audio import PersistentVoiceAudio
from .codex import CodexAdapter, CodexTurnError
from .runtime import BrokerRuntime
from .server import create_broker


SLEEP_PHRASES = {"go to sleep", "stop listening", "sleep"}
EXIT_PHRASES = {"exit voice mode", "quit voice mode", "goodbye", "shut down"}
ACK_PHRASES = {"nice", "thanks", "thank you", "okay", "ok", "got it", "cool"}


def wake_command(text: str, wake_phrase: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text).strip().lstrip("\ufeff\u200b")
    hey_match = re.match(r"^hey\b", normalized, flags=re.IGNORECASE)
    if hey_match:
        normalized = normalized[hey_match.end() :].lstrip(" \t,;:.!?…—–-")

    if not normalized.casefold().startswith(wake_phrase.casefold()):
        return None
    remainder = normalized[len(wake_phrase) :]
    if remainder and remainder[0].isalnum():
        return None
    return remainder.lstrip(" \t,;:.!?…—–-").strip()


def control_intent(text: str) -> str | None:
    normalized = " ".join(text.lower().strip().rstrip(".!?").split())
    if normalized in SLEEP_PHRASES:
        return "sleep"
    if normalized in EXIT_PHRASES:
        return "exit"
    if normalized in ACK_PHRASES:
        return "ack"
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
        initial_thread_id: str | None = None,
        adapter_kind: str = "exec",
        display: Callable[[str], None] = print,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.runtime = runtime
        self.audio = audio
        self.wake_phrase = wake_phrase
        self.codex_factory = codex_factory
        self.initial_thread_id = initial_thread_id
        self.adapter_kind = adapter_kind
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
        self.display(f"Codex adapter: {self.adapter_kind}")
        if self.initial_thread_id:
            self.display(f"Codex thread: {self.initial_thread_id}")
            self.display(f"Open it later: codex resume {self.initial_thread_id}")
            self._shown_codex_thread = self.initial_thread_id
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
                    self.display("Wake accepted; listening for your request…")
                    await self.audio.cue_listening()
                    pending = await self._listen_safely()
                    if not pending:
                        continue
                    self.display(f"You: {pending}")
                self.display("Request accepted; submitting to Codex…")
                await self.audio.cue_submitted()
                if control_intent(pending) == "exit":
                    await self.audio.speak("Hands-free Codex is stopping.")
                    self.runtime.begin_shutdown()
                    return
                session = self.runtime.open_session(
                    self.initial_thread_id or "handsfree", str(self.repo_root)
                )
                session_id = session.session_id
                self.runtime.activate(session_id)
                if self._codex is None:
                    self._codex = self.codex_factory(self.repo_root)

            assert pending is not None and session_id is not None and self._codex is not None
            intent = control_intent(pending)
            if intent == "ack":
                self._close_session(session_id)
                session_id = None
                pending = None
                self.display(f"Acknowledged. Say {self.wake_phrase} for another request.")
                continue
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
            await self.audio.cue_listening()
            pending = await self._listen_safely()
            if self.runtime.snapshot().shutting_down:
                return
            self.runtime.finish_playback(session_id)
            if not pending:
                self._close_session(session_id)
                session_id = None
                self.display(f"Follow-up window closed. Say {self.wake_phrase} to wake me.")
                continue
            await self.audio.cue_submitted()
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
    voice_speed: float,
    listen_duration: float,
    min_duration: float,
    codex_executable: str,
    codex_sandbox: str,
    codex_model: str,
    codex_reasoning_effort: str,
    silence_threshold_ms: int,
    codex_adapter: str = "auto",
    codex_thread_id: str | None = None,
    new_thread: bool = False,
) -> None:
    resolved_adapter = "exec" if codex_adapter == "auto" else codex_adapter
    if resolved_adapter != "exec":
        raise RuntimeError(
            "app-server turn execution is not available yet; use --adapter exec"
        )
    selected_thread_id = None if new_thread else codex_thread_id

    def codex_factory(root: Path) -> CodexAdapter:
        adapter = CodexAdapter(
            root,
            executable=codex_executable,
            sandbox=codex_sandbox,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
        )
        adapter.thread_id = selected_thread_id
        return adapter

    runtime, _dispatcher, server = create_broker(socket_path, audio_enabled=True)
    server.start()
    server_thread = threading.Thread(target=server.serve_forever, name="voicemode-broker", daemon=True)
    server_thread.start()
    audio = PersistentVoiceAudio(
        voice=voice,
        speed=voice_speed,
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
        codex_factory=codex_factory,
        initial_thread_id=selected_thread_id,
        adapter_kind=resolved_adapter,
    )
    try:
        asyncio.run(loop.run())
    finally:
        audio.close()
        runtime.begin_shutdown()
        server.stop()
        server_thread.join(timeout=2)
