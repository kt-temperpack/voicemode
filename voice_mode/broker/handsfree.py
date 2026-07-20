"""Foreground wake/listen/Codex/speak loop."""

from __future__ import annotations

import asyncio
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Callable

from .audio import PersistentVoiceAudio
from .codex import CodexAdapter, CodexTurnError
from .hosts import (
    AppServerHostAdapter,
    AppServerTransport,
    ExecCodexAdapter,
    HostAdapter,
    HostAdapterError,
    select_thread,
)
from .journal import TurnJournal
from .recovery import RecoveryAction, RecoveryCoordinator
from .runtime import BrokerRuntime
from .server import create_broker
from .presentation import Presenter
from .types import CanonicalResponse, HostEvent, HostEventKind


EXEC_NEW_THREAD_ID = "exec:new-separate-thread"


SLEEP_PHRASES = {"go to sleep", "stop listening", "sleep"}
EXIT_PHRASES = {"exit voice mode", "quit voice mode", "goodbye", "shut down"}
ACK_PHRASES = {"nice", "thanks", "thank you", "okay", "ok", "got it", "cool"}


class HostTransportLost(CodexTurnError):
    """The host connection disappeared after a dispatch boundary."""


class HostTurnRunner:
    """Wait for one correlated completion from any lifecycle-managed host."""

    def __init__(
        self,
        adapter: HostAdapter,
        thread_id: str,
        *,
        display: Callable[[str], None] = print,
        turn_timeout: float = 30 * 60,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self.adapter = adapter
        self.thread_id = thread_id
        self.display = display
        self.turn_timeout = turn_timeout
        self.should_stop = should_stop
        self._condition = threading.Condition()
        self._terminal: dict[str, HostEvent] = {}
        self._active_turn_id: str | None = None
        self._active_request_id: str | None = None
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
        if event.kind is HostEventKind.TRANSPORT_LOST:
            with self._condition:
                if self._active_request_id is not None:
                    self._terminal.setdefault(self._active_request_id, event)
                self._condition.notify_all()
            return
        if event.kind not in {HostEventKind.TURN_COMPLETED, HostEventKind.TURN_CANCELLED}:
            return
        if event.request_id is None:
            return
        with self._condition:
            self._terminal.setdefault(event.request_id, event)
            self._condition.notify_all()

    def run_turn(
        self,
        prompt: str,
        *,
        request_id: str | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> CanonicalResponse:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        request_id = request_id or str(uuid.uuid4())
        self._active_request_id = request_id
        try:
            started = self.adapter.start_turn(
                request_id=request_id,
                thread_id=self.thread_id,
                prompt=prompt,
            )
        except HostAdapterError as error:
            self._active_request_id = None
            raise CodexTurnError(str(error)) from error
        self._active_turn_id = started.host_turn_id
        if on_started is not None:
            on_started()
        deadline = time.monotonic() + self.turn_timeout
        stopped = False
        with self._condition:
            while request_id not in self._terminal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(0.2, remaining))
                if self.should_stop():
                    stopped = True
                    try:
                        self.adapter.interrupt_turn(
                            request_id=request_id,
                            thread_id=self.thread_id,
                            host_turn_id=started.host_turn_id,
                        )
                    except HostAdapterError:
                        pass
                    break
            event = self._terminal.pop(request_id, None)
        self._active_turn_id = None
        self._active_request_id = None
        if event is None:
            if stopped:
                raise CodexTurnError("Codex turn stopped with the broker")
            raise CodexTurnError("Codex host turn exceeded its deadline")
        if event.kind is HostEventKind.TRANSPORT_LOST:
            raise HostTransportLost(event.error or "Codex host transport was lost")
        if event.kind is HostEventKind.TURN_CANCELLED:
            raise CodexTurnError("Codex turn was interrupted")
        if event.error:
            raise CodexTurnError(event.error)
        if event.completion is None:
            raise CodexTurnError("Codex completed without a final response")
        text = event.completion.display_text.strip()
        if not text:
            raise CodexTurnError("Codex returned an empty final response")
        self.thread_id = event.completion.thread_id
        return event.completion.canonical_response()

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
            request_id=self._active_request_id or str(uuid.uuid4()),
            thread_id=self.thread_id,
            host_turn_id=self._active_turn_id,
        )

    def close(self) -> None:
        self._unsubscribe()

    def replace_adapter(self, adapter: HostAdapter) -> None:
        self._unsubscribe()
        self.adapter = adapter
        self._unsubscribe = adapter.subscribe(self._on_event)


# Keep the public name used by early adopters while the implementation is now
# fully host-neutral.
AppServerCodexRunner = HostTurnRunner


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
        host_adapter: HostAdapter,
        thread_id: str,
        recovery: RecoveryCoordinator | None = None,
        display: Callable[[str], None] = print,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.runtime = runtime
        self.audio = audio
        self.wake_phrase = wake_phrase
        self.host_adapter = host_adapter
        self.thread_id = thread_id
        self.recovery = recovery
        self.host_probe = host_adapter.probe()
        self.display = display
        self._runner = HostTurnRunner(host_adapter, thread_id, display=display)
        self._runner.should_stop = lambda: self.runtime.snapshot().shutting_down
        self._shown_codex_thread: str | None = thread_id
        self._presenter = Presenter(
            runtime,
            display=lambda text: self.display(f"\nCodex:\n{text}\n"),
            speak=audio.speak,
        )

    def _close_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        try:
            self.runtime.close_session(session_id)
        except Exception:
            pass

    async def run(self) -> None:
        self.display(f"Codex adapter: {self.host_probe.adapter}")
        if self.host_probe.reason:
            self.display(f"Adapter note: {self.host_probe.reason}")
        self.display(f"Repository: {self.repo_root}")
        self.display(f"Codex thread: {self.thread_id}")
        if self.thread_id != EXEC_NEW_THREAD_ID:
            self.display(f"Open it later: codex resume {self.thread_id}")
        self.display(f"Wake phrase: {self.wake_phrase}")
        self.display("Hands-free Codex ready")
        await self.audio.speak(
            f"Hands-free Codex is ready. Say {self.wake_phrase}, followed by your request."
        )
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
                    self.thread_id, str(self.repo_root)
                )
                session_id = session.session_id
                self.runtime.activate(session_id)

            assert pending is not None and session_id is not None
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

            active_session = self.runtime.snapshot().session
            assert active_session is not None
            envelope = self.runtime.accept_turn(
                session_id,
                pending,
                host_adapter=self.host_probe.adapter,
                host_thread_id=active_session.codex_session_id,
            )
            assert envelope.request_id is not None
            claim = self.runtime.claim_dispatch(envelope.request_id)
            if not claim.should_dispatch:
                raise RuntimeError("accepted broker turn did not receive dispatch authority")
            self.display("Codex: working…")
            try:
                response = await asyncio.to_thread(
                    self._runner.run_turn,
                    envelope.transcript,
                    request_id=envelope.request_id,
                    on_started=lambda: self.runtime.confirm_dispatch(envelope.request_id),
                )
            except HostTransportLost as error:
                response = await self._recover_turn(envelope.request_id, error)
                if response is None:
                    self._close_session(session_id)
                    session_id = None
                    pending = None
                    continue
            except CodexTurnError as error:
                self.runtime.mark_dispatch_uncertain(envelope.request_id)
                self.display(f"Codex error: {error}")
                self._close_session(session_id)
                session_id = None
                pending = None
                await self.audio.speak("Codex hit an error. I went back to sleep.")
                continue

            if self.runtime.snapshot().shutting_down:
                return

            self.thread_id = response.thread_id
            self._runner.thread_id = response.thread_id
            self.runtime.attach_codex_thread(session_id, response.thread_id)
            if response.thread_id != self._shown_codex_thread:
                self._shown_codex_thread = response.thread_id
                self.display(f"Codex thread: {response.thread_id}")
                self.display(f"Open it later: codex resume {response.thread_id}")
            self.runtime.confirm_dispatch(envelope.request_id)
            self.runtime.accept_summary(session_id, response.spoken_text)
            await self._presenter.present(response)
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

    async def _recover_turn(
        self, request_id: str, error: HostTransportLost
    ) -> CanonicalResponse | None:
        if self.recovery is None:
            self.runtime.mark_dispatch_uncertain(request_id)
            self.display(f"Codex connection lost: {error}")
            await self.audio.speak("Codex disconnected. I stopped without retrying the request.")
            return None
        self.display("Codex connection lost; checking the exact turn before continuing…")
        decision = await asyncio.to_thread(
            self.recovery.recover,
            request_id=request_id,
            thread_id=self.thread_id,
            dispatch_confirmed=True,
        )
        if self.recovery.adapter is not None:
            self.host_adapter = self.recovery.adapter
            self._runner.replace_adapter(self.host_adapter)
        if decision.action is RecoveryAction.PRESENT:
            return self.runtime.canonical_response(request_id)
        self.display(f"Codex recovery stopped: {decision.rationale}")
        await self.audio.speak("Codex recovery could not prove a final answer, so I did not retry it.")
        return None

    async def _listen_safely(self) -> str | None:
        try:
            return await self.audio.listen()
        except Exception as error:
            self.display(f"Voice input error: {error}")
            return None

    def close(self) -> None:
        self._runner.close()
        self.host_adapter.close()

    def interrupt(self) -> None:
        self._runner.interrupt()


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
    from voice_mode.config import (
        BROKER_JOURNAL_DIR,
        BROKER_JOURNAL_INCLUDE_TRANSCRIPT,
        BROKER_JOURNAL_MAX_BYTES,
        BROKER_JOURNAL_MAX_FILES,
    )

    if codex_adapter not in {"auto", "app-server", "exec"}:
        raise ValueError(f"unknown Codex adapter: {codex_adapter}")
    resolved_adapter = "app-server" if codex_adapter == "auto" else codex_adapter
    selected_thread_id = None if new_thread else codex_thread_id
    host_adapter: HostAdapter | None = None
    recovery_factory = None

    if resolved_adapter == "app-server":
        def connect_app_server() -> AppServerHostAdapter:
            transport = AppServerTransport.start_process(executable=codex_executable)
            return AppServerHostAdapter.connect(transport)

        recovery_factory = connect_app_server
        try:
            host_adapter = connect_app_server()
            selection = select_thread(
                host_adapter,
                repo_root,
                explicit_thread_id=selected_thread_id,
                new_thread=new_thread,
            )
            selected_thread_id = selection.thread.thread_id
        except Exception:
            if host_adapter is not None:
                host_adapter.close()
            host_adapter = None
            recovery_factory = None
            if codex_adapter != "auto":
                raise
            resolved_adapter = "exec"

    if resolved_adapter == "exec":
        codex = CodexAdapter(
            repo_root,
            executable=codex_executable,
            sandbox=codex_sandbox,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
        )
        host_adapter = ExecCodexAdapter(codex)
        if selected_thread_id is not None:
            host_adapter.attach_thread(selected_thread_id)
        else:
            selected_thread_id = EXEC_NEW_THREAD_ID

    runtime = None
    server = None
    server_thread = None
    audio = None
    loop = None
    try:
        assert host_adapter is not None and selected_thread_id is not None
        journal = TurnJournal(
            BROKER_JOURNAL_DIR,
            f"{resolved_adapter}:{selected_thread_id}:{Path(repo_root).resolve()}",
            include_transcript=BROKER_JOURNAL_INCLUDE_TRANSCRIPT,
            max_files=BROKER_JOURNAL_MAX_FILES,
            max_total_bytes=BROKER_JOURNAL_MAX_BYTES,
        )
        runtime, dispatcher, server = create_broker(
            socket_path,
            audio_enabled=True,
            journal=journal,
        )
        recovery = (
            RecoveryCoordinator(runtime, journal, recovery_factory)
            if recovery_factory is not None
            else None
        )
        if recovery is not None:
            recovery.adapter = host_adapter
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
            host_adapter=host_adapter,
            thread_id=selected_thread_id,
            recovery=recovery,
        )
        dispatcher.interrupt_callback = loop.interrupt
        asyncio.run(loop.run())
    finally:
        if loop is not None:
            loop.close()
        if audio is not None:
            audio.close()
        if runtime is not None:
            runtime.begin_shutdown()
        if server is not None:
            server.stop()
        if server_thread is not None:
            server_thread.join(timeout=2)
        if host_adapter is not None:
            host_adapter.close()
