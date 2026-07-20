# Broker Hardware Qualification

This matrix is the release evidence for the hands-free broker's audio path. Run
it on a warmed local broker after changes to capture, endpointing, cues, device
ownership, interruption, or TTS startup. Record durations from monotonic clocks
as JSONL; never save microphone audio or transcript text in the benchmark file.

Each observation has the form
`{"metric":"wake_ack","duration_ms":123.4}`. Supported metrics are
`wake_ack`, `endpoint_delay`, `submission_state`, `host_dispatch`,
`playback_cancel`, `acoustic_barge_in`, `device_reopen`, and `first_tts_audio`.
When the input/output topology cannot safely distinguish user speech from
speaker playback, record
`{"metric":"acoustic_barge_in","supported":false}` and verify the push-to-talk
hotkey instead.

Run at least 20 warmed observations for every supported latency metric, then
qualify them with:

```bash
uv run python scripts/benchmark-broker-audio.py \
  --input /tmp/broker-latency.jsonl \
  --output /tmp/broker-latency-report.json
```

The command reports p50, p95, and maximum durations plus operating-system,
architecture, and Python metadata. It exits 1 for a missing or regressed
published budget and also runs a 100-turn synthetic audio-owner soak, which must
finish with zero live streams and never observe more than one live stream.

| Scenario | Required check | Pass condition |
| --- | --- | --- |
| Built-in microphone and speakers | Wake, natural endpoint, submission, dispatch, cancel, TTS | All published p95 budgets pass; acoustic barge-in passes or is explicitly unsupported |
| USB microphone | Capture and unplug/replug while idle | Reopen succeeds with one input owner and the next wake works |
| Wired headphones | Push-to-talk during TTS | Playback stops under 150 ms and one redirect is submitted |
| Bluetooth headset | Warm cue, capture, TTS, profile rotation | No repeated cue, no lost input owner, and measured budgets pass after warmup |
| Device rotation | Switch input while idle and while listening | Reopen is bounded, old stream closes, and no prompt is duplicated |
| Fan noise | Pause naturally after a sentence | Endpoint p95 stays under 900 ms without clipped endings |
| Music or television | Leave broker asleep for five minutes | No cue, transcript, or dispatch occurs |
| Speaker playback | Attempt acoustic barge-in during TTS | Under 400 ms only when topology qualification supports it; otherwise hotkey fallback is explicit |

Attach the JSON report to the release evidence. A service-health check is not a
substitute: qualification proves the user-visible boundaries and single-owner
resource invariant.
