"""Single-owner audio lifecycle with cancellable playback and device recovery."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np


logger = logging.getLogger("voicemode.broker.audio_session")


def _default_player_factory():
    from voice_mode.audio_player import NonBlockingAudioPlayer

    return NonBlockingAudioPlayer()


class AudioSession:
    """Own exactly one input stream and at most one output player."""

    def __init__(
        self,
        *,
        input_factory: Callable[..., Any],
        input_kwargs: dict[str, Any],
        player_factory: Callable[[], Any] = _default_player_factory,
        device_probe: Callable[[], object] | None = None,
        reopen_delays: tuple[float, ...] = (0.0, 0.05, 0.2),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._input_factory = input_factory
        self._input_kwargs = dict(input_kwargs)
        self._player_factory = player_factory
        self._device_probe = device_probe
        self._reopen_delays = reopen_delays
        self._sleep = sleep
        self._lock = threading.RLock()
        self._input_stream = None
        self._input_device = None
        self._player = None
        self._playback_cancel: threading.Event | None = None
        self._closed = False
        self.muted = threading.Event()
        self.muted.set()

    @property
    def input_stream(self):
        with self._lock:
            return self._input_stream

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def start(self) -> None:
        with self._lock:
            self._require_open()
            if self._input_stream is not None:
                return
            stream = self._input_factory(**self._input_kwargs)
            try:
                stream.start()
            except Exception:
                self._close_stream(stream)
                raise
            self._input_stream = stream
            self._input_device = self._probe_device()

    def mute(self) -> None:
        self.muted.set()

    def unmute(self) -> None:
        with self._lock:
            self._require_open()
            self.muted.clear()

    async def play(self, samples: np.ndarray, sample_rate: int) -> None:
        self.cancel_playback()
        with self._lock:
            self._require_open()
            player = self._player_factory()
            cancel = threading.Event()
            self._player = player
            self._playback_cancel = cancel
        try:
            await asyncio.to_thread(
                player.play,
                samples,
                sample_rate,
                blocking=True,
                cancellation_event=cancel,
            )
        finally:
            with self._lock:
                if self._player is player:
                    self._player = None
                    self._playback_cancel = None

    def cancel_playback(self) -> bool:
        with self._lock:
            player = self._player
            cancel = self._playback_cancel
            if player is None or cancel is None or cancel.is_set():
                return False
            cancel.set()
        player.stop()
        return True

    def ensure_device(self) -> bool:
        """Reopen when the default device rotates or the stream becomes inactive."""
        with self._lock:
            self._require_open()
            stream = self._input_stream
            expected = self._input_device
            current = self._probe_device()
            active = stream is not None and getattr(stream, "active", True)
            changed = (
                self._device_probe is not None
                and expected is not None
                and current != expected
            )
        if stream is None or not active or changed:
            self.reopen()
            return True
        return False

    def reopen(self) -> None:
        """Serialize input rotation and retry without ever retaining two streams."""
        with self._lock:
            self._require_open()
            old_stream, self._input_stream = self._input_stream, None
            self._input_device = None
            self.muted.set()
            self._close_stream(old_stream)

            last_error: Exception | None = None
            for delay in self._reopen_delays:
                if delay:
                    self._sleep(delay)
                self._require_open()
                stream = None
                try:
                    stream = self._input_factory(**self._input_kwargs)
                    stream.start()
                except Exception as error:
                    last_error = error
                    self._close_stream(stream)
                    continue
                self._input_stream = stream
                self._input_device = self._probe_device()
                return
            if last_error is not None:
                raise last_error
            raise RuntimeError("audio input reopen has no configured attempts")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.muted.set()
            stream, self._input_stream = self._input_stream, None
            self._input_device = None
            player = self._player
            cancel = self._playback_cancel
            should_stop = player is not None and cancel is not None and not cancel.is_set()
            if should_stop:
                cancel.set()
        if should_stop:
            player.stop()
        self._close_stream(stream)

    def _probe_device(self):
        return self._device_probe() if self._device_probe is not None else None

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("audio session is closed")

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception as error:
            logger.debug("audio input stop failed during cleanup: %s", error)
        try:
            stream.close()
        except Exception as error:
            logger.debug("audio input close failed during cleanup: %s", error)
