"""Optional global hotkey and dependency-free terminal activation adapters."""

from __future__ import annotations

import os
import select
import sys
import threading
from collections.abc import Callable

from .activation import ActivationBus, ActivationEvent, ActivationKind


class HotkeyRegistrationError(RuntimeError):
    def __init__(self, binding: str, reason: str) -> None:
        super().__init__(
            f"could not register broker hotkey {binding!r}: {reason}. "
            "Choose another binding with --hotkey or "
            "VOICEMODE_BROKER_HOTKEY='<ctrl>+<alt>+space'."
        )


class PlatformHotkeyAdapter:
    """Publish press/release events through pynput when it is available."""

    def __init__(
        self,
        binding: str,
        bus: ActivationBus,
        *,
        keyboard_module=None,
    ) -> None:
        self.binding = binding
        self.bus = bus
        self._keyboard = keyboard_module
        self._listener = None
        self._required = frozenset()
        self._pressed = set()
        self._held = False

    def start(self) -> None:
        try:
            keyboard = self._keyboard
            if keyboard is None:
                from pynput import keyboard
            self._keyboard = keyboard
            self._required = frozenset(keyboard.HotKey.parse(self.binding))
            if not self._required:
                raise ValueError("binding contains no keys")
            self._listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            self._listener.start()
            wait_until_ready = getattr(self._listener, "wait", None)
            if wait_until_ready is not None:
                wait_until_ready()
        except Exception as error:
            self.close()
            raise HotkeyRegistrationError(self.binding, str(error)) from error

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if self._held:
            self._held = False
            self.bus.publish(
                ActivationEvent.now(ActivationKind.PUSH_TO_TALK_RELEASE, "hotkey")
            )
        if listener is not None:
            listener.stop()

    def _canonical(self, key):
        listener = self._listener
        canonical = getattr(listener, "canonical", None)
        return canonical(key) if canonical is not None else key

    def _on_press(self, key) -> None:
        self._pressed.add(self._canonical(key))
        if not self._held and self._required.issubset(self._pressed):
            self._held = True
            self.bus.publish(
                ActivationEvent.now(ActivationKind.PUSH_TO_TALK_PRESS, "hotkey")
            )

    def _on_release(self, key) -> None:
        key = self._canonical(key)
        if self._held and key in self._required:
            self._held = False
            self.bus.publish(
                ActivationEvent.now(ActivationKind.PUSH_TO_TALK_RELEASE, "hotkey")
            )
        self._pressed.discard(key)


class TerminalKeyAdapter:
    """Use space as a toggle, with s for sleep and i for interrupt."""

    def __init__(
        self,
        bus: ActivationBus,
        *,
        read_key: Callable[[], str] | None = None,
    ) -> None:
        self.bus = bus
        self._read_key = read_key
        self._stop = threading.Event()
        self._thread = None
        self._active = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="voicemode-terminal-activation",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)

    def feed_key(self, key: str) -> None:
        if key == " ":
            self._active = not self._active
            kind = (
                ActivationKind.PUSH_TO_TALK_PRESS
                if self._active
                else ActivationKind.PUSH_TO_TALK_RELEASE
            )
        elif key.casefold() == "s":
            kind = ActivationKind.SLEEP
        elif key.casefold() == "i":
            kind = ActivationKind.INTERRUPT
        else:
            return
        self.bus.publish(ActivationEvent.now(kind, "terminal"))

    def _run(self) -> None:
        if self._read_key is None:
            self._run_terminal()
            return
        while not self._stop.is_set():
            key = self._read_key()
            if not key:
                return
            self.feed_key(key)

    def _run_terminal(self) -> None:
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            while not self._stop.is_set():
                readable, _writable, _errors = select.select(
                    [descriptor], [], [], 0.1
                )
                if readable:
                    key = os.read(descriptor, 1).decode(errors="ignore")
                    if not key:
                        return
                    self.feed_key(key)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
