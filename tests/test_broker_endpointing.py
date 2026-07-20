from voice_mode.broker.endpointing import (
    EndpointDetector,
    EndpointReason,
    FrameMetadata,
)


def frame(*, speech=False, rms=100.0, duration=30, complete=False, released=False):
    return FrameMetadata(duration, rms, speech, complete, released)


def feed(detector, frames):
    decision = None
    for item in frames:
        decision = detector.feed(item)
        if decision.ended:
            return decision
    return decision


def test_fan_noise_never_starts_an_utterance():
    detector = EndpointDetector(silence_ms=900, min_utterance_ms=300)
    decision = feed(
        detector,
        [frame(speech=False, rms=500 + index % 30) for index in range(100)],
    )

    assert decision.ended is False
    assert detector.speech_started is False
    assert detector.noise_floor > 450


def test_intermittent_false_vad_does_not_hold_endpoint_open():
    detector = EndpointDetector(silence_ms=900, min_utterance_ms=300)
    frames = [frame(speech=True, rms=1200) for _ in range(15)]
    frames += [
        frame(speech=index in {8, 21}, rms=150 if index not in {8, 21} else 500)
        for index in range(30)
    ]

    decision = feed(detector, frames)

    assert decision.ended is True
    assert decision.reason is EndpointReason.SILENCE
    assert decision.elapsed_ms <= 1_350


def test_natural_pause_does_not_truncate_following_words():
    detector = EndpointDetector(silence_ms=900, min_utterance_ms=300)
    first_clause = [frame(speech=True, rms=1200) for _ in range(12)]
    pause = [frame(speech=False, rms=80) for _ in range(20)]
    final_words = [frame(speech=True, rms=1000) for _ in range(8)]

    assert feed(detector, first_clause + pause).ended is False
    assert feed(detector, final_words).ended is False
    decision = feed(detector, [frame(speech=False, rms=70) for _ in range(30)])

    assert decision.reason is EndpointReason.SILENCE


def test_clipped_word_ending_resets_tail_without_full_timeout_penalty():
    detector = EndpointDetector(silence_ms=900, min_utterance_ms=300)
    frames = [frame(speech=True, rms=1100) for _ in range(20)]
    frames += [frame(speech=False, rms=70) for _ in range(10)]
    frames += [frame(speech=True, rms=900)]
    frames += [frame(speech=False, rms=70) for _ in range(29)]

    decision = feed(detector, frames)

    assert decision.reason is EndpointReason.SILENCE
    assert decision.trailing_silence_ms >= 810


def test_continuous_speech_stops_at_hard_maximum():
    detector = EndpointDetector(
        silence_ms=900,
        min_utterance_ms=300,
        max_utterance_ms=1_500,
    )
    decision = feed(detector, [frame(speech=True, rms=1200) for _ in range(60)])

    assert decision.reason is EndpointReason.MAX_DURATION
    assert decision.utterance_ms == 1_500


def test_push_to_talk_release_is_exact_even_for_short_audio():
    detector = EndpointDetector(silence_ms=900, min_utterance_ms=1_000)
    frames = [frame(speech=True, rms=1200) for _ in range(2)]
    frames.append(frame(speech=False, rms=80, released=True))

    decision = feed(detector, frames)

    assert decision.reason is EndpointReason.PUSH_TO_TALK_RELEASE
    assert decision.elapsed_ms == 90


def test_linguistic_completion_only_shortens_an_actual_silent_tail():
    detector = EndpointDetector(silence_ms=900, min_utterance_ms=300)
    spoken = [frame(speech=True, rms=1000, complete=True) for _ in range(12)]
    assert feed(detector, spoken).ended is False
    assert feed(detector, [frame(speech=False, rms=80) for _ in range(15)]).ended is False

    decision = feed(detector, [frame(speech=False, rms=80) for _ in range(5)])

    assert decision.reason is EndpointReason.LINGUISTIC_SILENCE
    assert decision.trailing_silence_ms >= 540
