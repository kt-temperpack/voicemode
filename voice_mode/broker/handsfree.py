"""Foreground wake/listen/Codex/speak loop."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Callable

from .audio import PersistentVoiceAudio
from .activation import (
    ActivationBus,
    ActivationEvent,
    ActivationKind,
    ActivationState,
    reduce_activation,
)
from .barge_in import BargeInCoordinator
from .codex import CodexAdapter, CodexTurnError
from .compatibility import probe_startup_compatibility
from .cues import CueKind, CuePolicy
from .hosts import (
    AppServerHostAdapter,
    AppServerTransport,
    ExecCodexAdapter,
    HostAdapter,
    HostAdapterError,
    select_thread,
)
from .journal import TurnJournal
from .hotkey import HotkeyRegistrationError, PlatformHotkeyAdapter, TerminalKeyAdapter
from .recovery import RecoveryAction, RecoveryCoordinator
from .runtime import BrokerRuntime
from .server import create_broker
from .presentation import Presenter, TerminalMode, TerminalPresenter
from .types import BrokerEvent, CanonicalResponse, HostEvent, HostEventKind


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

    def steer_request(self, request_id: str, prompt: str) -> None:
        if self._active_request_id != request_id or self._active_turn_id is None:
            raise CodexTurnError("Codex has no matching active turn to steer")
        self.steer(prompt)

    def interrupt(self) -> None:
        if self._active_turn_id is None:
            return
        self.adapter.interrupt_turn(
            request_id=self._active_request_id or str(uuid.uuid4()),
            thread_id=self.thread_id,
            host_turn_id=self._active_turn_id,
        )

    def interrupt_request(self, request_id: str) -> None:
        if self._active_request_id != request_id or self._active_turn_id is None:
            raise CodexTurnError("Codex has no matching active turn to interrupt")
        self.interrupt()

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
        activation_bus: ActivationBus | None = None,
        display: Callable[[str], None] = print,
        terminal: TerminalPresenter | None = None,
        tts_enabled: bool = True,
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
        self.terminal = terminal
        self._tts_enabled = tts_enabled
        self._tts_failure_shown = False
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._cue_sequence = 0
        cue_players = {
            CueKind.RISING: audio.cue_listening,
            CueKind.FALLING: audio.cue_submitted,
        }
        if hasattr(audio, "cue_interruption"):
            cue_players[CueKind.INTERRUPTION] = audio.cue_interruption
        if hasattr(audio, "cue_failure"):
            cue_players[CueKind.FAILURE] = audio.cue_failure
        self._cues = CuePolicy(cue_players)
        self._runner = HostTurnRunner(host_adapter, thread_id, display=display)
        self._runner.should_stop = lambda: self.runtime.snapshot().shutting_down
        self._shown_codex_thread: str | None = thread_id
        self._presenter = Presenter(
            runtime,
            display=lambda text: self.display(f"\nCodex:\n{text}\n"),
            speak=self._speak,
        )
        self._activation_lock = threading.Lock()
        self._activation_state = ActivationState()
        self._barge_in = BargeInCoordinator(
            runtime=runtime,
            audio=audio,
            host_probe=self.host_probe,
            interrupt_host=self._runner.interrupt_request,
            steer_host=self._runner.steer_request,
            result_sink=self._show_barge_result,
        )
        self._activation_unsubscribe = (
            activation_bus.subscribe(self._on_activation)
            if activation_bus is not None
            else lambda: None
        )

    def _on_activation(self, event: ActivationEvent) -> None:
        with self._activation_lock:
            previous = self._activation_state
            self._activation_state = reduce_activation(previous, event)
            current = self._activation_state
        if event.kind is ActivationKind.PUSH_TO_TALK_PRESS:
            cancelled, latency_ms = self._barge_in.cancel_audio(event.timestamp)
            self.audio.begin_push_to_talk()
            self._barge_in.activate(
                activated_at=event.timestamp,
                playback_cancelled=cancelled,
                cancellation_latency_ms=latency_ms,
            )
        elif event.kind is ActivationKind.INTERRUPT:
            self._barge_in.activate(activated_at=event.timestamp)
            if self._event_loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._cue(BrokerEvent.BARGE_IN, f"interrupt:{event.timestamp}"),
                    self._event_loop,
                )
        if current.endpoint_requested and not previous.endpoint_requested:
            self.audio.release_push_to_talk()

    def _show_barge_result(self, result) -> None:
        if result.playback_cancelled or result.request_id is not None:
            self.display(
                "Interruption: "
                f"{result.action.value} in {result.cancellation_latency_ms:.1f} ms; "
                f"{result.rationale}"
            )

    async def _cue(self, event: BrokerEvent, event_id: str | None = None) -> None:
        if event_id is None:
            self._cue_sequence += 1
            event_id = f"{event.value}:{self._cue_sequence}"
        await self._cues.emit(event, event_id)

    def _state(
        self, event: BrokerEvent, state: str, *, transcript: str | None = None
    ) -> None:
        if self.terminal is not None:
            self.terminal.state(event.value, state, transcript=transcript)

    async def _capture_redirect(self, request_id: str) -> str | None:
        pending = await self._listen_safely()
        self._consume_direct_capture()
        self._barge_in.finish(request_id)
        if pending:
            self.display(f"You: {pending}")
        return pending

    async def _speak(self, message: str) -> None:
        if not self._tts_enabled:
            raise RuntimeError("voice output is unavailable")
        try:
            await self.audio.speak(message)
        except Exception as error:
            self._tts_enabled = False
            if not self._tts_failure_shown:
                self._tts_failure_shown = True
                self.display(
                    f"Voice output unavailable; continuing with visible responses: {error}"
                )
            raise

    async def _speak_optional(self, message: str) -> None:
        try:
            await self._speak(message)
        except Exception:
            pass

    def _consume_direct_capture(self) -> bool:
        with self._activation_lock:
            direct = self._activation_state.direct_capture
            self._activation_state = ActivationState(
                toggle_active=self._activation_state.toggle_active,
                push_to_talk_held=self._activation_state.push_to_talk_held,
            )
        return direct

    def _close_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        try:
            self.runtime.close_session(session_id)
        except Exception:
            pass

    async def run(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self.display(f"Codex adapter: {self.host_probe.adapter}")
        if self.host_probe.reason:
            self.display(f"Adapter note: {self.host_probe.reason}")
        self.display(f"Repository: {self.repo_root}")
        self.display(f"Codex thread: {self.thread_id}")
        if self.thread_id != EXEC_NEW_THREAD_ID:
            self.display(f"Open it later: codex resume {self.thread_id}")
        self.display(f"Wake phrase: {self.wake_phrase}")
        self.display("Hands-free Codex ready")
        self._state(BrokerEvent.RESET, "asleep")
        await self._speak_optional(
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
                pending = (
                    heard
                    if self._consume_direct_capture()
                    else wake_command(heard, self.wake_phrase)
                )
                if pending is None:
                    continue
                if not pending:
                    self.display("Wake accepted; listening for your request…")
                    self._state(BrokerEvent.LISTEN_STARTED, "listening")
                    await self._cue(BrokerEvent.LISTEN_STARTED)
                    pending = await self._listen_safely()
                    if not pending:
                        continue
                    self.display(f"You: {pending}")
                intent = control_intent(pending)
                if intent == "ack":
                    pending = None
                    self.display(f"Acknowledged. Say {self.wake_phrase} for another request.")
                    continue
                if intent == "sleep":
                    pending = None
                    await self._speak_optional(
                        f"Going to sleep. Say {self.wake_phrase} when you need me."
                    )
                    continue
                if intent == "exit":
                    await self._speak_optional("Hands-free Codex is stopping.")
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
                await self._speak_optional(f"Going to sleep. Say {self.wake_phrase} when you need me.")
                continue
            if intent == "exit":
                self._close_session(session_id)
                await self._speak_optional("Hands-free Codex is stopping.")
                self.runtime.begin_shutdown()
                return

            self.display("Request accepted; submitting to Codex…")
            self._state(
                BrokerEvent.UTTERANCE_ENQUEUED,
                "thinking",
                transcript=pending,
            )
            await self._cue(BrokerEvent.UTTERANCE_ENQUEUED)

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
                if self._barge_in.host_was_interrupted(envelope.request_id):
                    pending = await self._capture_redirect(envelope.request_id)
                    if pending:
                        continue
                    self._close_session(session_id)
                    session_id = None
                    continue
                self.runtime.mark_dispatch_uncertain(envelope.request_id)
                self.display(f"Codex error: {error}")
                self._state(BrokerEvent.FAULT, "asleep")
                await self._cue(BrokerEvent.FAULT, f"failure:{envelope.request_id}")
                self._close_session(session_id)
                session_id = None
                pending = None
                await self._speak_optional("Codex hit an error. I went back to sleep.")
                continue

            if self.runtime.snapshot().shutting_down:
                return

            if self._barge_in.host_was_interrupted(envelope.request_id):
                pending = await self._capture_redirect(envelope.request_id)
                if pending:
                    continue
                self._close_session(session_id)
                session_id = None
                continue

            self.thread_id = response.thread_id
            self._runner.thread_id = response.thread_id
            self.runtime.attach_codex_thread(session_id, response.thread_id)
            if response.thread_id != self._shown_codex_thread:
                self._shown_codex_thread = response.thread_id
                self.display(f"Codex thread: {response.thread_id}")
                self.display(f"Open it later: codex resume {response.thread_id}")
            self.runtime.confirm_dispatch(envelope.request_id)
            redirected = self._barge_in.redirect_pending(envelope.request_id)
            if redirected:
                self._presenter.show_final(response)
                if self.runtime.mark_tts_started(envelope.request_id):
                    self.runtime.finish_tts(envelope.request_id, failed=True)
            else:
                self.runtime.accept_summary(session_id, response.spoken_text)
                self._state(BrokerEvent.SUMMARY_ACCEPTED, "speaking")
                await self._presenter.present(response)
                redirected = self._barge_in.redirect_pending(envelope.request_id)
            if not redirected:
                self._state(BrokerEvent.LISTEN_STARTED, "listening")
                await self._cue(
                    BrokerEvent.LISTEN_STARTED,
                    f"followup:{envelope.request_id}",
                )
            pending = await self._listen_safely()
            if redirected:
                self._consume_direct_capture()
            if self.runtime.snapshot().shutting_down:
                return
            if redirected:
                self._barge_in.finish(envelope.request_id)
            else:
                self.runtime.finish_playback(session_id)
            if not pending:
                self._close_session(session_id)
                session_id = None
                self.display(f"Follow-up window closed. Say {self.wake_phrase} to wake me.")
                self._state(BrokerEvent.FOLLOWUP_EXPIRED, "asleep")
                continue
            self.display(f"You: {pending}")
            if not redirected:
                self.runtime.start_listening(session_id)

        self._close_session(session_id)

    async def _recover_turn(
        self, request_id: str, error: HostTransportLost
    ) -> CanonicalResponse | None:
        if self.recovery is None:
            self.runtime.mark_dispatch_uncertain(request_id)
            self.display(f"Codex connection lost: {error}")
            await self._speak_optional("Codex disconnected. I stopped without retrying the request.")
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
        await self._speak_optional("Codex recovery could not prove a final answer, so I did not retry it.")
        return None

    async def _listen_safely(self) -> str | None:
        try:
            return await self.audio.listen()
        except Exception as error:
            self.display(f"Voice input error: {error}")
            return None

    def close(self) -> None:
        self._event_loop = None
        self._activation_unsubscribe()
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
    hotkey: str | None = None,
    terminal_keys: bool = True,
) -> None:
    from voice_mode.config import (
        BROKER_JOURNAL_DIR,
        BROKER_JOURNAL_INCLUDE_TRANSCRIPT,
        BROKER_JOURNAL_MAX_BYTES,
        BROKER_JOURNAL_MAX_FILES,
        BROKER_OUTPUT_INCLUDE_TRANSCRIPT,
        BROKER_OUTPUT_MODE,
        BROKER_OUTPUT_TRANSCRIPT_AUTHORIZED,
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

    assert host_adapter is not None
    try:
        compatibility = probe_startup_compatibility(host_adapter.probe())
        compatibility.require_supported_input()
    except Exception:
        host_adapter.close()
        raise
    for issue in compatibility.issues:
        print(
            f"Compatibility {issue.severity.value}: {issue.message}. "
            f"Run: {issue.recovery_command}"
        )

    runtime = None
    server = None
    server_thread = None
    audio = None
    loop = None
    activation_adapters = []
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
            compatibility=compatibility.projection(),
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
        activation_bus = ActivationBus()
        terminal = TerminalPresenter(
            sys.stdout,
            mode=TerminalMode(BROKER_OUTPUT_MODE),
            include_transcript=BROKER_OUTPUT_INCLUDE_TRANSCRIPT,
            transcript_authorized=BROKER_OUTPUT_TRANSCRIPT_AUTHORIZED,
        )
        loop = HandsFreeLoop(
            repo_root=repo_root,
            runtime=runtime,
            audio=audio,
            wake_phrase=wake_phrase,
            host_adapter=host_adapter,
            thread_id=selected_thread_id,
            recovery=recovery,
            activation_bus=activation_bus,
            terminal=terminal,
            display=terminal.line,
            tts_enabled=any(
                provider.service == "tts" and provider.available
                for provider in compatibility.providers
            ),
        )
        if hotkey:
            hotkey_adapter = PlatformHotkeyAdapter(hotkey, activation_bus)
            try:
                hotkey_adapter.start()
            except HotkeyRegistrationError as error:
                terminal.line(f"Hotkey unavailable: {error}")
                terminal.line(f"Wake phrase remains available: {wake_phrase}")
            else:
                activation_adapters.append(hotkey_adapter)
                terminal.line(f"Push-to-talk hotkey: {hotkey}")
        if terminal_keys and sys.stdin.isatty():
            terminal_adapter = TerminalKeyAdapter(activation_bus)
            terminal_adapter.start()
            activation_adapters.append(terminal_adapter)
            terminal.line(
                "Terminal controls: space toggles push-to-talk; s sleeps; i interrupts"
            )
        dispatcher.interrupt_callback = loop.interrupt
        asyncio.run(loop.run())
    finally:
        for activation_adapter in activation_adapters:
            activation_adapter.close()
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
