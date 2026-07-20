# VoiceMode Daily-Driver Design

**Status:** Approved direction; pending written-spec review

**Date:** 2026-07-19

**Scope:** Deterministic conversation kernel, unambiguous Codex attachment,
self-healing operation, and quiet visible feedback. Reusable SDK extraction,
repository memory, remote clients, and personality features are deferred.

## Product Contract

VoiceMode should let a user speak to the coding agent they are already using,
receive exactly one response, and continue working without managing a microphone
or hunting for a session. At every moment the user must be able to tell whether
VoiceMode is asleep, listening, submitting, waiting on Codex, speaking, or
recovering from a failure.

The daily-driver acceptance test is an eight-hour coding session in which the
user never has to restart audio manually, never loses track of the active Codex
thread, never hears duplicate speech, and can diagnose any failed turn from one
command without reading raw logs.

## Goals

- Guarantee at-most-once model dispatch, exactly one visible final for every
  completed response, and at-most-once TTS playback.
- Route speech into an explicit Codex thread and make thread ownership visible.
- Support interruption, deterministic push-to-talk, and natural end-of-speech
  detection without repeated chimes or microphone cycling.
- Start automatically, recover from expected process and device failures, and
  detect incompatible plugin, CLI, broker, and Codex versions before a turn.
- Preserve a complete visible transcript while keeping spoken responses brief.
- Produce privacy-safe event traces that explain the last failed or slow turn.

## Non-goals

- A general-purpose VoiceMode SDK or third-party plugin API in this delivery.
- Long-term agent memory or automatic extraction of repository decisions.
- Remote/mobile audio, multi-user routing, or simultaneous agents.
- Personalities, background music, or other presentation features.
- Perfect far-field wake recognition without a push-to-talk fallback.

## Architecture

The broker remains the single owner of microphone and speaker devices. Four
bounded components communicate through typed events:

```text
activation/audio -> conversation kernel -> Codex host adapter
       ^                    |                    |
       |                    v                    v
       +------------ presentation <------ canonical response
                            |
                            v
                   journal + diagnostics
```

The conversation kernel is authoritative. Audio code detects physical events,
the Codex adapter translates semantic turns, and presentation renders state;
none of those components may mutate conversation state independently.

### 1. Deterministic Conversation Kernel

Every captured turn becomes a `TurnEnvelope` containing:

- `utterance_id`, created when speech capture begins;
- `request_id`, created once when the transcript is accepted;
- broker session, repository, and Codex thread identifiers;
- normalized transcript and named control intent, when applicable;
- current lifecycle state and monotonic timestamps.

The turn lifecycle is:

```text
capturing -> accepted -> dispatched -> response_completed
          -> presentation_started -> presentation_completed
```

Control intents such as sleep, stop, repeat, and acknowledgment take a separate
local path and never enter `dispatched`. Invalid transitions fail closed and are
recorded as structured errors.

The following invariants are enforced in the runtime rather than by prompt text:

- A `request_id` can be dispatched to the agent at most once.
- A completed response can be printed at most once and played at most once.
- Terminal and TTS output derive from the same immutable canonical response.
- TTS failure cannot replay or regenerate the model response.
- A retry checks the journal and host thread before deciding whether work is
  safe to resume; an uncertain dispatch is never submitted again automatically.
- Only the broker owns audio. Agent children receive no voice tools.

The existing in-memory state machine becomes a pure transition reducer backed
by an append-only, privacy-safe session journal. The journal stores identifiers,
states, timings, provider names, and error codes; transcript and audio retention
continue to follow their existing explicit opt-in settings.

### 2. Unambiguous Codex Attachment

VoiceMode adds a `CodexHostAdapter` interface with two implementations.

`AppServerCodexAdapter` is preferred when the installed Codex exposes the
app-server protocol. It uses capability discovery rather than version guessing,
then relies on thread list/read/resume plus turn start, steering, interruption,
and completion notifications. The broker supplies `request_id` as the host's
client user-message identifier so reconnection and deduplication share one key.

`ExecCodexAdapter` preserves compatibility with current `codex exec` behavior.
It owns a separate resumable thread, disables child voice tools, and parses one
schema-constrained canonical response. It cannot claim to be the user's existing
interactive session.

Thread selection is deterministic:

1. An explicitly supplied thread identifier wins.
2. A host-native registration from the current Codex client wins next.
3. A single active thread for the canonical repository may be resumed.
4. If selection remains ambiguous, VoiceMode creates a broker-owned thread and
   announces that fact before accepting the request.

The terminal header and `broker status` always show adapter kind, complete
thread ID, repository, turn state, and the exact resume/focus command. Changing
threads is an explicit operation; VoiceMode never silently follows “most recent”
while a session is open.

### 3. Self-Healing Everyday Operation

The foreground development loop remains available, but daily use runs under the
platform supervisor: launchd on macOS and systemd on Linux. The supervised
process owns the broker socket and audio devices; CLI commands are clients.

The intuitive lifecycle surface is:

```text
voicemode start
voicemode stop
voicemode restart
voicemode status [--json]
voicemode doctor [--json]
voicemode explain-last [--json]
```

Existing `broker run/status/stop` commands remain compatible and teach the new
canonical forms in help output. `start` is idempotent, and a second invocation
reports the healthy existing owner instead of opening another microphone.

Startup performs a compatibility handshake across the installed CLI, MCP
plugin, broker protocol, local STT/TTS endpoints, and Codex host capabilities.
An incompatible installation fails before microphone capture with the exact
upgrade or restart command. This directly prevents an older installed binary
from producing errors such as a newly documented subcommand being unavailable.

Expected failures recover as follows:

- Audio-device change: reopen the selected device with bounded backoff and keep
  the agent thread attached.
- STT/TTS endpoint failure: retry locally, then follow configured failover while
  preserving the turn identifier and privacy policy.
- Broker crash: the supervisor restarts it; the journal restores safe state and
  marks uncertain in-flight presentation for inspection rather than replay.
- Codex disconnect: retain the thread identifier, reconnect through the adapter,
  and query turn state before accepting another request.
- Stale socket or lock: validate ownership and liveness, replace only proven
  stale artifacts, and explain any refusal.
- Repeated failure: open the circuit temporarily, keep push-to-talk/status
  available, and stop producing recurring sounds.

### 4. Quiet, Visible Interaction

Audio feedback has stable semantics:

- one rising cue: wake accepted and request capture is open;
- one falling cue: speech ended and the request was accepted;
- immediate playback stop: interruption accepted;
- one low failure cue: the turn could not proceed and visual diagnostics exist;
- silence: idle polling, ambient speech, blank STT, and routine timeout.

No state transition repeats its cue, and cues never play over captured speech or
TTS. Acknowledgments such as “nice” and “thanks” close the follow-up window
locally without a model call or spoken reply.

Endpointing uses a hybrid detector instead of a reset-on-any-noise timer:

- adaptive input noise floor and WebRTC VAD voting;
- a mostly-silent trailing window tolerant of isolated false-positive frames;
- minimum speech duration and configurable maximum utterance duration;
- push-to-talk release as an exact endpoint;
- optional partial-STT punctuation/linguistic evidence as a secondary signal,
  never the sole reason to cut off speech.

Push-to-talk and a global hotkey are required fallbacks. Acoustic barge-in stops
playback when the input path can distinguish live speech from speaker output;
the hotkey always provides a deterministic interruption path. The broker keeps
one input stream alive across states, but captured frames are discarded while
policy says they are not eligible for activation.

The visible transcript uses one compact state line and complete turn records:

```text
[listening] -> [submitted 7c91] -> [Codex thread 019f… working]
You: …
Codex: …
```

State updates are rewritten in an interactive terminal and emitted as normal
lines in non-TTY output. Machine-readable mode sends deterministic JSONL to
stdout and diagnostics to stderr.

## Diagnostics and Agent Ergonomics

`voicemode status --json` and `voicemode capabilities --json` expose protocol
versions, active adapter, thread, phase, device and provider health, configured
latency thresholds, supervisor state, and the documented exit-code dictionary.
They never expose transcript content or secrets.

`voicemode explain-last` reconstructs the most recent turn from its events and
answers concrete questions: what was heard, whether endpointing completed,
whether the turn was dispatched, which thread received it, whether a response
completed, whether it was printed and spoken, and which exact recovery command
applies. Transcript text appears only when transcript persistence is enabled.

Errors name the failed component and give one copyable correction. A reasonable
but obsolete command is redirected or receives a precise “did you mean” hint;
it never ends with only generic usage text.

## Performance Budgets

Measured on a warmed local macOS development setup, excluding model reasoning:

- wake or hotkey acknowledgment: p95 under 250 ms;
- end-of-speech recognition: p95 under 900 ms after natural speech stops;
- submission cue and visible state: under 100 ms after endpointing;
- host dispatch overhead: p95 under 200 ms;
- hotkey interruption to playback stop: p95 under 150 ms;
- acoustic barge-in to playback stop: p95 under 400 ms where supported;
- first TTS audio after the canonical response: p95 under 600 ms;
- duplicate dispatches, terminal finals, or TTS plays: zero.

Each budget is emitted as an event duration and exercised by a reproducible
benchmark. Performance work optimizes the measured critical path, not aggregate
test runtime.

## Security and Privacy

The socket remains user-owned and local. Wake processing and endpointing are
local by default. Cloud providers receive only an activated utterance and only
when configured. The journal contains no ambient audio; recordings and
transcripts remain independently visible, opt-in settings. Diagnostic JSON
redacts credentials, provider tokens, and transcript content by default.

Host attachment never bypasses the Codex thread's sandbox or approval policy.
VoiceMode forwards approval events to the owning host surface and speaks only a
short notification that visual input is required.

## Verification

### Kernel and property tests

- Generate valid and invalid event sequences and prove transition determinism.
- Assert at-most-once dispatch, exactly one visible final, and at-most-once TTS
  playback under retries, cancellation, TTS failure, reconnect, and restart.
- Fuzz wake, control-intent, and endpoint-window inputs, including Unicode and
  noisy VAD sequences.

### Adapter conformance

Every host adapter runs the same suite for create, attach, dispatch, steer,
interrupt, completion, reconnect, ambiguous-thread handling, and capability
downgrade. App-server schemas are captured as test fixtures, and unsupported
methods trigger the exec fallback rather than failing mid-turn.

### Integration and fault injection

- Fake audio, STT, Codex, and TTS services exercise complete sessions.
- Kill each process at every lifecycle state and verify safe recovery.
- Disconnect providers, rotate devices, corrupt a journal tail, fill the queue,
  and race stop/barge-in against response completion.
- Verify structured stdout, diagnostic stderr, stable exit codes, and
  deterministic `--json` output.

### Hardware and endurance

- Quiet room, fan noise, music, speaker output, headphones, and Bluetooth.
- Built-in, USB, and Bluetooth device changes during capture and playback.
- One hundred consecutive turns and an eight-hour soak with no duplicate
  response, leaked device, stuck phase, or growing queue.

## Delivery Boundaries

The implementation should land in four independently verifiable work packages:

1. **Kernel:** turn envelopes, journal, idempotency, canonical presentation, and
   property/fault tests.
2. **Codex attachment:** host-adapter contract, app-server implementation,
   exec fallback, thread-selection UX, and adapter conformance tests.
3. **Audio interaction:** hybrid endpointing, hotkey/push-to-talk, interruption,
   cue contract, and hardware benchmarks.
4. **Operations and visibility:** supervisor lifecycle, compatibility handshake,
   status/capabilities/doctor/explain-last, recovery, and endurance tests.

Work package 1 lands first because the other three depend on its identifiers and
state guarantees. Package 2 follows so all later live tests exercise the real
thread path. Packages 3 and 4 can then proceed against stable contracts, with
operations completing the release gate.

## Release Gate

The feature is ready for default-on daily use only when all four work packages
pass their focused suites, the eight-hour soak passes, the current Codex thread
is always identifiable, and fault injection produces no duplicate dispatch or
presentation. Until then, the existing one-shot `converse` path remains the
documented safe fallback.
