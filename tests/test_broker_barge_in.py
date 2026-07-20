from datetime import datetime, timezone

import pytest

from voice_mode.broker import (
    BrokerPhase,
    CanonicalResponse,
    HostCapability,
    HostProbe,
    TurnState,
)
from voice_mode.broker.barge_in import (
    AcousticTopology,
    BargeInAction,
    BargeInCoordinator,
    acoustic_barge_in_supported,
)
from voice_mode.broker.runtime import BrokerRuntime


class FakeAudio:
    def __init__(self, cancelled=True):
        self.cancelled = cancelled
        self.cancels = 0

    def cancel_playback(self):
        self.cancels += 1
        return self.cancelled


def thinking_runtime(tmp_path):
    identities = iter(["session-1", "utterance-1", "request-1"])
    runtime = BrokerRuntime(
        uuid_factory=lambda: next(identities),
        utc_now=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    session = runtime.open_session("thread-1", str(tmp_path))
    runtime.activate(session.session_id)
    envelope = runtime.accept_turn(
        session.session_id,
        "original prompt",
        host_adapter="app-server",
        host_thread_id="thread-1",
    )
    runtime.claim_dispatch(envelope.request_id)
    runtime.confirm_dispatch(envelope.request_id)
    return runtime, session, envelope


def coordinator(runtime, audio, capabilities, *, interrupts=None, steers=None):
    interrupts = interrupts if interrupts is not None else []
    steers = steers if steers is not None else []
    return BargeInCoordinator(
        runtime=runtime,
        audio=audio,
        host_probe=HostProbe("fake", True, frozenset(capabilities)),
        interrupt_host=interrupts.append,
        steer_host=lambda request_id, prompt: steers.append((request_id, prompt)),
    )


def test_hot_interruption_cancels_audio_and_exact_correlated_host_turn(tmp_path):
    runtime, _session, envelope = thinking_runtime(tmp_path)
    audio = FakeAudio()
    interrupts = []
    barge = coordinator(
        runtime,
        audio,
        {HostCapability.INTERRUPT_TURN},
        interrupts=interrupts,
    )

    result = barge.activate(activated_at=0)

    assert result.action is BargeInAction.HOST_INTERRUPTED
    assert result.request_id == envelope.request_id
    assert interrupts == [envelope.request_id]
    assert audio.cancels == 1
    assert runtime.turn_diagnostic()["state"] == TurnState.CANCELLED.value
    assert runtime.snapshot().phase is BrokerPhase.LISTENING


def test_unsupported_host_keeps_one_safe_redirect_without_cancelling_turn(tmp_path):
    runtime, _session, envelope = thinking_runtime(tmp_path)
    barge = coordinator(runtime, FakeAudio(), set())

    first = barge.activate(activated_at=0)
    second = barge.activate(activated_at=0)

    assert first.action is BargeInAction.QUEUED_FOR_SAFE_TURN
    assert second.request_id == envelope.request_id
    assert barge.redirect_pending(envelope.request_id) is True
    assert runtime.turn_diagnostic()["state"] == TurnState.DISPATCHED.value
    barge.finish(envelope.request_id)
    assert barge.redirect_pending(envelope.request_id) is False


def test_prompt_steers_only_when_the_host_advertises_steering(tmp_path):
    runtime, _session, envelope = thinking_runtime(tmp_path)
    steers = []
    barge = coordinator(
        runtime,
        FakeAudio(False),
        {HostCapability.STEER_TURN},
        steers=steers,
    )

    result = barge.activate(activated_at=0, prompt="redirected prompt")

    assert result.action is BargeInAction.HOST_STEERED
    assert steers == [(envelope.request_id, "redirected prompt")]
    assert runtime.turn_diagnostic()["state"] == TurnState.DISPATCHED.value


def test_completion_wins_race_by_request_id_and_is_not_interrupted(tmp_path):
    runtime, _session, envelope = thinking_runtime(tmp_path)
    response = CanonicalResponse(
        1,
        envelope.request_id,
        "thread-1",
        "Finished response.",
        "Finished response.",
        "host-turn-1",
        datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    runtime.accept_host_completion(response)
    interrupts = []
    barge = coordinator(
        runtime,
        FakeAudio(),
        {HostCapability.INTERRUPT_TURN},
        interrupts=interrupts,
    )

    result = barge.activate(activated_at=0)

    assert result.action is BargeInAction.QUEUED_FOR_SAFE_TURN
    assert interrupts == []
    assert runtime.turn_diagnostic()["state"] == TurnState.HOST_COMPLETED.value


def test_playback_cancellation_latency_is_measured_at_the_audio_boundary(tmp_path):
    runtime = BrokerRuntime()
    audio = FakeAudio()
    clock = iter([10.025])
    barge = BargeInCoordinator(
        runtime=runtime,
        audio=audio,
        host_probe=HostProbe("fake", True, frozenset()),
        interrupt_host=lambda _request: None,
        steer_host=lambda _request, _prompt: None,
        monotonic=lambda: next(clock),
    )

    result = barge.activate(activated_at=10.0)

    assert result.action is BargeInAction.PLAYBACK_CANCELLED
    assert result.cancellation_latency_ms == pytest.approx(25.0)


def test_acoustic_barge_in_requires_conservative_headphone_topology():
    qualified = AcousticTopology(True, True, echo_score=0.1, noise_floor=0.2)
    assert acoustic_barge_in_supported(qualified) is True
    assert acoustic_barge_in_supported(
        AcousticTopology(False, True, echo_score=0.1, noise_floor=0.2)
    ) is False
    assert acoustic_barge_in_supported(
        AcousticTopology(True, False, echo_score=0.1, noise_floor=0.2)
    ) is False
    assert acoustic_barge_in_supported(
        AcousticTopology(True, True, echo_score=0.2, noise_floor=0.2)
    ) is False
