"""Foreground wake/listen/Codex/speak loop."""

from __future__ import annotations

import asyncio
import re
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Callable

from .audio import PersistentVoiceAudio
from .codex import CodexAdapter, CodexTurn, CodexTurnError, _fallback_summary
from .hosts import (
    AppServerHostAdapter,
    AppServerTransport,
    HostAdapterError,
    select_thread,
)
from .runtime import BrokerRuntime
from .server import create_broker
from .types import HostEvent, HostEventKind


SLEEP_PHRASES = {"go to sleep", "stop listening", "sleep"}
EXIT_PHRASES = {"exit voice mode", "quit voice mode", "goodbye", "shut down"}
ACK_PHRASES = {"nice", "thanks", "thank you", "okay", "ok", "got it", "cool"}


class AppServerCodexRunner:
    """Blocking compatibility bridge over the host-native event lifecycle."""

    def __init__(
        self,
        adapter: AppServerHostAdapter,
        thread_id: str,
        *,
        display: Callable[[str], None] = print,
        turn_timeout: float = 30 * 60,
    ) -> None:
        self.adapter = adapter
        self.thread_id = thread_id
        self.display = display
        self.turn_timeout = turn_timeout
        self._condition = threading.Condition()
        self._terminal: dict[str, HostEvent] = {}
        self._active_turn_id: str | None = None
        self._unsubscribe = adapter.subscribe(self._on_event)

    def _on_event(self, event: HostEvent) -> None:
        if event.kind is HostEventKind.APPROVAL_REQUIRED and event.approval is not None:
            approval = event.approval
            self.display(
                "Codex approval required: "
                f"thread={approval.thread_id} request={approval.request_id} "
                f"approval={approval.approval_id} reason={approval.reason}"
            )
            return
        if event.kind not in {HostEventKind.TURN_COMPLETED, HostEventKind.TURN_CANCELLED}:
            return
        if event.request_id is None:
            return
        with self._condition:
            self._terminal.setdefault(event.request_id, event)
            self._condition.notify_all()

    def run_turn(self, prompt: str) -> CodexTurn:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        request_id = str(uuid.uuid4())
        try:
            started = self.adapter.start_turn(
                request_id=request_id,
                thread_id=self.thread_id,
                prompt=prompt,
            )
        except HostAdapterError as error:
            raise CodexTurnError(str(error)) from error
        self._active_turn_id = started.host_turn_id
        with self._condition:
            ready = self._condition.wait_for(
                lambda: request_id in self._terminal,
                timeout=self.turn_timeout,
            )
            event = self._terminal.pop(request_id, None)
        self._active_turn_id = None
        if not ready or event is None:
            raise CodexTurnError("Codex app-server turn exceeded its deadline")
        if event.kind is HostEventKind.TURN_CANCELLED:
            raise CodexTurnError("Codex turn was interrupted")
        if event.error:
            raise CodexTurnError(event.error)
        if event.completion is None:
            raise CodexTurnError("Codex completed without a final response")
        text = event.completion.display_text.strip()
        if not text:
            raise CodexTurnError("Codex returned an empty final response")
        return CodexTurn(text, _fallback_summary(text), self.thread_id)

    def steer(self, prompt: str) -> None:
        if self._active_turn_id is None:
            raise CodexTurnError("Codex has no active turn to steer")
        self.adapter.steer_turn(
            request_id=str(uuid.uuid4()),
            thread_id=self.thread_id,
            host_turn_id=self._active_turn_id,
            prompt=prompt,
        )

    def interrupt(self) -> None:
        if self._active_turn_id is None:
            return
        self.adapter.interrupt_turn(
            request_id=str(uuid.uuid4()),
            thread_id=self.thread_id,
            host_turn_id=self._active_turn_id,
        )

    def close(self) -> None:
        self._unsubscribe()


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
    if codex_adapter not in {"auto", "app-server", "exec"}:
        raise ValueError(f"unknown Codex adapter: {codex_adapter}")
    resolved_adapter = "app-server" if codex_adapter == "auto" else codex_adapter
    selected_thread_id = None if new_thread else codex_thread_id
    host_adapter: AppServerHostAdapter | None = None
    app_runner: AppServerCodexRunner | None = None

    if resolved_adapter == "app-server":
        transport = AppServerTransport.start_process(executable=codex_executable)
        try:
            host_adapter = AppServerHostAdapter.connect(transport)
            selection = select_thread(
                host_adapter,
                repo_root,
                explicit_thread_id=selected_thread_id,
                new_thread=new_thread,
            )
            selected_thread_id = selection.thread.thread_id
            app_runner = AppServerCodexRunner(host_adapter, selected_thread_id)
        except BaseException:
            if host_adapter is not None:
                host_adapter.close()
            else:
                transport.close()
            raise

        def codex_factory(_root: Path):
            assert app_runner is not None
            return app_runner

    else:
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

    runtime = None
    server = None
    server_thread = None
    audio = None
    try:
        runtime, _dispatcher, server = create_broker(socket_path, audio_enabled=True)
        server.start()
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="voicemode-broker",
            daemon=True,
        )
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
        asyncio.run(loop.run())
    finally:
        if audio is not None:
            audio.close()
        if runtime is not None:
            runtime.begin_shutdown()
        if server is not None:
            server.stop()
        if server_thread is not None:
            server_thread.join(timeout=2)
        if app_runner is not None:
            app_runner.close()
        if host_adapter is not None:
            host_adapter.close()
