import asyncio
import threading

import numpy as np
import pytest

from voice_mode.broker.audio_session import AudioSession
from voice_mode.audio_player import NonBlockingAudioPlayer


class TrackedStream:
    active_count = 0
    peak_active = 0

    def __init__(self, *, fail_start=False, fail_stop=False, **kwargs):
        self.kwargs = kwargs
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.active = False
        self.starts = 0
        self.stops = 0
        self.closes = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise OSError("device unavailable")
        self.active = True
        type(self).active_count += 1
        type(self).peak_active = max(type(self).peak_active, type(self).active_count)

    def stop(self):
        self.stops += 1
        if self.active:
            self.active = False
            type(self).active_count -= 1
        if self.fail_stop:
            raise OSError("device disappeared")

    def close(self):
        self.closes += 1


class BlockingPlayer:
    def __init__(self):
        self.started = threading.Event()
        self.cancel = None
        self.play_calls = 0
        self.stop_calls = 0

    def play(self, _samples, _sample_rate, *, blocking, cancellation_event):
        assert blocking is True
        self.play_calls += 1
        self.cancel = cancellation_event
        self.started.set()
        cancellation_event.wait(1)

    def stop(self):
        self.stop_calls += 1


class FakeOutputStream:
    def __init__(self):
        self.stops = 0
        self.closes = 0

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


@pytest.fixture(autouse=True)
def reset_stream_counts():
    TrackedStream.active_count = 0
    TrackedStream.peak_active = 0


def test_start_and_close_are_idempotent_single_owner_operations():
    streams = []

    def factory(**kwargs):
        stream = TrackedStream(**kwargs)
        streams.append(stream)
        return stream

    session = AudioSession(input_factory=factory, input_kwargs={"callback": object()})
    session.start()
    session.start()
    session.close()
    session.close()

    assert len(streams) == 1
    assert streams[0].starts == streams[0].stops == streams[0].closes == 1
    assert TrackedStream.active_count == 0


def test_default_device_rotation_closes_old_stream_before_reopen():
    streams = []
    device = {"id": 1}

    def factory(**kwargs):
        stream = TrackedStream(**kwargs)
        streams.append(stream)
        return stream

    session = AudioSession(
        input_factory=factory,
        input_kwargs={},
        device_probe=lambda: device["id"],
        reopen_delays=(0,),
    )
    session.start()
    assert session.ensure_device() is False

    device["id"] = 2
    assert session.ensure_device() is True

    assert len(streams) == 2
    assert streams[0].stops == streams[0].closes == 1
    assert streams[1].active is True
    assert TrackedStream.peak_active == 1
    session.close()


def test_reopen_failure_leaves_no_live_stream_or_hidden_owner():
    calls = 0
    streams = []

    def factory(**kwargs):
        nonlocal calls
        calls += 1
        stream = TrackedStream(fail_start=calls > 1, **kwargs)
        streams.append(stream)
        return stream

    session = AudioSession(
        input_factory=factory,
        input_kwargs={},
        reopen_delays=(0, 0),
    )
    session.start()

    with pytest.raises(OSError, match="device unavailable"):
        session.reopen()

    assert session.input_stream is None
    assert TrackedStream.active_count == 0
    assert all(stream.closes == 1 for stream in streams)
    session.close()


def test_disappeared_device_cleanup_cannot_block_recovery():
    streams = []

    def factory(**kwargs):
        stream = TrackedStream(fail_stop=not streams, **kwargs)
        streams.append(stream)
        return stream

    session = AudioSession(
        input_factory=factory,
        input_kwargs={},
        reopen_delays=(0,),
    )
    session.start()
    streams[0].active = False
    TrackedStream.active_count = 0

    assert session.ensure_device() is True
    assert streams[0].closes == 1
    assert streams[1].active is True
    assert TrackedStream.peak_active == 1
    session.close()


@pytest.mark.asyncio
async def test_cancel_race_stops_current_playback_once_and_never_replays():
    player = BlockingPlayer()
    session = AudioSession(
        input_factory=TrackedStream,
        input_kwargs={},
        player_factory=lambda: player,
    )
    session.start()
    task = asyncio.create_task(session.play(np.ones(32, dtype=np.float32), 24_000))
    assert await asyncio.to_thread(player.started.wait, 1)

    assert session.cancel_playback() is True
    assert session.cancel_playback() is False
    await task

    assert player.play_calls == 1
    assert player.stop_calls == 1
    session.close()


@pytest.mark.asyncio
async def test_device_reopen_during_playback_keeps_one_input_and_one_player():
    player = BlockingPlayer()
    streams = []

    def factory(**kwargs):
        stream = TrackedStream(**kwargs)
        streams.append(stream)
        return stream

    session = AudioSession(
        input_factory=factory,
        input_kwargs={},
        player_factory=lambda: player,
        reopen_delays=(0,),
    )
    session.start()
    task = asyncio.create_task(session.play(np.ones(32, dtype=np.float32), 24_000))
    assert await asyncio.to_thread(player.started.wait, 1)

    session.reopen()
    assert TrackedStream.peak_active == 1
    assert player.play_calls == 1
    session.close()
    await task

    assert player.stop_calls == 1
    assert TrackedStream.active_count == 0


@pytest.mark.asyncio
async def test_final_close_rejects_stream_recreation_and_playback():
    session = AudioSession(input_factory=TrackedStream, input_kwargs={})
    session.start()
    session.close()

    with pytest.raises(RuntimeError, match="closed"):
        session.start()
    with pytest.raises(RuntimeError, match="closed"):
        session.reopen()
    with pytest.raises(RuntimeError, match="closed"):
        await session.play(np.ones(4, dtype=np.float32), 24_000)


def test_player_wait_observes_cancellation_at_a_bounded_interval():
    player = NonBlockingAudioPlayer()
    stream = FakeOutputStream()
    cancel = threading.Event()
    player.stream = stream
    player.cancellation_event = cancel
    cancel.set()

    player.wait(timeout=1)

    assert stream.stops == stream.closes == 1
    assert player.stream is None


def test_player_stop_and_wait_race_closes_output_once():
    player = NonBlockingAudioPlayer()
    stream = FakeOutputStream()
    player.stream = stream
    waiter = threading.Thread(target=player.wait, kwargs={"timeout": 1})
    waiter.start()

    player.stop()
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert stream.stops == stream.closes == 1
