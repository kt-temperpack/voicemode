# VoiceMode Daily-Driver Implementation Plan

**Status:** Ready for task conversion and implementation

**Design source:** `docs/superpowers/specs/2026-07-19-voice-daily-driver-design.md`

**Objective:** Make VoiceMode a reliable daily interface to Codex: one accepted
utterance goes to one explicit Codex thread, yields one visible final, plays at
most once, survives routine failures, and always exposes its current state.

**In scope:** Deterministic conversation kernel, unambiguous Codex attachment,
self-healing operation, and quiet visible interaction.

**Deferred:** General SDK extraction, repository memory, remote/mobile audio,
multi-user routing, personalities, and music.

## 1. Constraints and invariants

These are implementation rules, not aspirations. Every task and review must
preserve them.

1. The broker is the only process allowed to own microphone and speaker state.
2. `request_id` is created once, before agent dispatch, and is the idempotency
   key across the journal, broker protocol, Codex adapter, terminal, and TTS.
3. A request is dispatched at most once. Recovery never guesses that an
   uncertain request is safe to submit again.
4. Each completed response has exactly one visible final and at most one TTS
   playback attempt. TTS failure cannot trigger model regeneration or replay.
5. Terminal and TTS derive from one immutable `CanonicalResponse`.
6. Local control intents do not create Codex turns.
7. A voice session has one explicit Codex thread. Thread changes are explicit.
8. Unsupported Codex app-server methods downgrade before accepting a request,
   not halfway through a turn.
9. Runtime journals store identifiers, transitions, timings, provider names,
   and errors. Transcript/audio storage remains separately opt-in.
10. Human output stays readable; JSON output is deterministic data on stdout;
    diagnostics go to stderr.
11. Existing one-shot `converse` behavior and existing `broker` protocol v1
    clients remain compatible until a separately announced migration.
12. Existing user changes to `CLAUDE.md` and `.pi-flywheel/` are out of scope.

## 2. Target architecture

The implementation converges on these boundaries:

```text
ActivationAdapter ----+
EndpointDetector -----+--> ConversationKernel --> CodexHostAdapter
AudioSession ---------+             |                    |
                                    v                    v
                             TurnJournal         CanonicalResponse
                                    |                    |
                                    +--> Presenter <-----+
                                           |
                                terminal + TTS + cues
```

`ConversationKernel` is a pure transition authority wrapped by a thread-safe
runtime. `TurnJournal` persists privacy-safe state. `CodexHostAdapter` owns host
semantics but no audio. `AudioSession` owns device I/O but no turn state.
`Presenter` performs deduplicated visible and spoken output.

The preferred Codex adapter speaks JSON-RPC to a long-running Codex app server.
The compatibility adapter keeps `codex exec`, but it is labeled as a separate
broker-owned thread and never masquerades as the current UI session.

## 3. Dependency graph

```text
DD-001 baseline fixtures
  |
  +--> DD-010 turn model/reducer --> DD-011 journal --> DD-012 idempotent runtime
  |                                      |                    |
  |                                      |                    +--> DD-013 presenter
  |                                      |                    +--> DD-014 protocol v2
  |                                      |
  |                                      +------------------------> DD-024 recovery
  |
  +--> DD-020 host contract --> DD-021 JSON-RPC transport
                                |
                                +--> DD-022 discovery/selection
                                      |
                                      +--> DD-023 turns/steer/interrupt
                                            |
                                            +--> DD-024 recovery/deduplication
                                            +--> DD-025 exec fallback parity
                                                  |
                                                  +--> DD-026 loop integration
  
DD-010 --> DD-030 endpoint detector --> DD-031 audio cancellation
                                      |       |
                                      |       +--> DD-033 barge-in
                                      +--> DD-032 activation/hotkey
                                      +--> DD-034 cue/presentation policy
                                              |
                                              +--> DD-035 hardware benchmarks

DD-014 --> DD-040 supervisor lifecycle --> DD-041 intuitive CLI
DD-014 --> DD-042 compatibility handshake
DD-011 --> DD-043 diagnostic projections --> DD-044 doctor/explain-last
DD-024 --> DD-045 recovery coordinator
DD-040 + DD-041 + DD-042 + DD-044 + DD-045 --> DD-046 docs/migration

DD-013 + DD-026 + DD-035 + DD-046 --> DD-050 conformance/fault/endurance
DD-050 --> DD-051 live release gate
```

The critical path is kernel → app-server integration → foreground loop → fault
and endurance validation. Audio and operations can proceed after their shared
contracts land, but default-on release waits for all tracks.

## 4. Work package 1: deterministic conversation kernel

### DD-001 — Capture behavioral baselines and protocol fixtures

**Purpose:** Preserve today’s working contracts before foundational changes so
refactors cannot accidentally erase wake parsing, socket safety, or the
single-response fixes already proven live.

**Files:**

- `tests/fixtures/broker/` (new fixture directory)
- `tests/test_broker_codex.py`
- `tests/test_broker_handsfree.py`
- `tests/test_broker_protocol.py`
- `tests/test_broker_runtime.py`
- `tests/test_broker_socket.py`

**Implementation:**

1. Capture representative protocol-v1 request/response JSON, Codex JSONL event
   streams, structured final messages, malformed events, and empty output.
2. Add characterization tests for wake → request → response → follow-up → sleep,
   acknowledgment suppression, one terminal final, one TTS call, and child MCP
   voice disablement.
3. Add a fixture scrubber assertion that rejects secrets, absolute home paths,
   transcripts not explicitly designated as synthetic, and unstable timestamps.
4. Record focused baseline commands and expected live state transitions in the
   fixture README.

**Acceptance:** Existing behavior is pinned without changing production code;
all broker tests pass; fixtures are deterministic across two consecutive runs.

**Depends on:** none.

**Unblocks:** every kernel and adapter task.

### DD-010 — Introduce turn envelopes and a pure reducer

**Purpose:** Move correctness from orchestration order and prompt wording into a
small state model that can be exhaustively tested.

**Files:**

- `voice_mode/broker/types.py`
- `voice_mode/broker/state.py`
- `voice_mode/broker/turns.py` (new)
- `tests/test_broker_state.py`
- `tests/test_broker_turns.py` (new)

**Implementation:**

1. Add frozen `TurnEnvelope`, `CanonicalResponse`, `TurnState`, and
   `PresentationState` types. Use opaque strings for host identifiers so the
   kernel remains host-independent.
2. Separate session phase from turn state. A session may be listening while the
   previous response’s durable presentation state is already complete.
3. Add events for capture start, transcript accepted, dispatch requested,
   dispatch confirmed, host completion, visible presentation, TTS start,
   TTS completion/failure, cancellation, and recovery uncertainty.
4. Implement a pure reducer returning a new state plus named side-effect intents.
   It must not perform I/O, create timestamps, or generate identifiers.
5. Preserve current protocol-v1 phase projections so old status clients continue
   to see `asleep`, `engaged`, `listening`, `thinking`, and `speaking`.
6. Generate event-sequence tests covering all valid edges and assert every
   invalid edge fails closed with the prior state unchanged.

**Acceptance:** Reducer tests cover every state/event pair; no event sequence can
produce two dispatch intents or two visible/TTS presentation intents.

**Depends on:** DD-001.

**Unblocks:** DD-011, DD-030.

### DD-011 — Add the privacy-safe append-only turn journal

**Purpose:** Make recovery evidence-based without turning operational logging
into an ambient transcript store.

**Files:**

- `voice_mode/broker/journal.py` (new)
- `voice_mode/config.py`
- `tests/test_broker_journal.py` (new)
- `tests/conftest.py`

**Implementation:**

1. Store JSONL under `~/.voicemode/broker/journal/` with one schema-versioned
   event per line and a stable session file name.
2. Include request, utterance, broker-session, repository, adapter, and Codex
   thread identifiers; state transition; monotonic duration; provider; and error
   code. Exclude transcript and audio unless their existing independent opt-ins
   authorize content persistence.
3. Write through a temporary record buffer, append atomically, flush, and make a
   torn final line recoverable. Never rewrite earlier records.
4. Add bounded retention by file count and total bytes. Retention runs only
   after a successful append and never blocks a live turn.
5. Provide a reader that validates schema/version, skips one torn tail, and
   reports earlier corruption rather than silently discarding history.
6. Inject path, clock, and writer dependencies for isolated tests.

**Acceptance:** Crash/torn-tail tests recover all complete records; default
journal output contains no transcript; two readers produce identical projections.

**Depends on:** DD-010.

**Unblocks:** DD-012, DD-024, DD-043.

### DD-012 — Make runtime dispatch idempotent

**Purpose:** Enforce the “one request, one dispatch” rule across retries,
reconnection, and process restart.

**Files:**

- `voice_mode/broker/runtime.py`
- `voice_mode/broker/types.py`
- `voice_mode/broker/journal.py`
- `tests/test_broker_runtime.py`
- `tests/test_broker_idempotency.py` (new)

**Implementation:**

1. Move identifier generation to capture/accept boundaries and retain IDs for
   the entire turn.
2. Replace `enqueue_utterance`/`wait_for_turn`’s implicit dispatch transition
   with explicit `accept_turn`, `claim_dispatch`, and `confirm_dispatch` calls.
3. Make `claim_dispatch(request_id)` atomic under the runtime lock. Repeated
   claims return the existing disposition and never emit another host action.
4. Persist the claim before invoking the host adapter. This creates an honest
   uncertain state if the process dies after claim but before confirmation.
5. On recovery, distinguish safe-to-present completed output, safe-to-cancel,
   and unknown host state. Unknown never means safe-to-resubmit.
6. Keep the one-pending-turn limit and explicit queue-full error for this release.

**Acceptance:** Race tests with concurrent claimers produce one dispatch; fault
tests at every persistence boundary produce zero automatic redispatches.

**Depends on:** DD-011.

**Unblocks:** DD-013, DD-014, DD-024.

### DD-013 — Centralize canonical presentation and deduplication

**Purpose:** Eliminate duplicate terminal and audio responses by construction.

**Files:**

- `voice_mode/broker/presentation.py` (new)
- `voice_mode/broker/audio.py`
- `voice_mode/broker/handsfree.py`
- `voice_mode/broker/codex.py`
- `tests/test_broker_presentation.py` (new)
- `tests/test_broker_handsfree.py`

**Implementation:**

1. Create `Presenter` with `show_final(response)` and `speak_final(response)`;
   both require the request ID and consult runtime presentation state.
2. Generate the short spoken excerpt deterministically from `display_text`, or
   accept a host-provided excerpt only when it is contained in the canonical
   response contract. Never ask the model for a second answer.
3. Mark visible presentation immediately before writing and TTS presentation
   immediately before playback. A failed write/play remains at-most-once and is
   exposed to diagnostics rather than retried invisibly.
4. Keep progress/status rendering outside final-response methods.
5. Delete direct final printing and speaking from `HandsFreeLoop`; it submits
   presentation intents to the presenter.
6. Retain child voice-tool disablement in the exec adapter as defense in depth.

**Acceptance:** Injected retries, duplicated completion notifications, TTS
exceptions, and loop cancellation yield exactly one visible final and no more
than one playback call.

**Depends on:** DD-012.

**Unblocks:** DD-050.

### DD-014 — Version broker protocol and expose turn identity

**Purpose:** Let CLI clients and diagnostics observe the stronger runtime without
breaking protocol-v1 consumers.

**Files:**

- `voice_mode/broker/protocol.py`
- `voice_mode/broker/server.py`
- `voice_mode/broker/client.py`
- `voice_mode/broker/types.py`
- `tests/test_broker_protocol.py`
- `tests/test_broker_socket.py`

**Implementation:**

1. Add protocol v2 negotiation while continuing to accept v1 requests.
2. Extend v2 status with request ID, adapter kind, complete Codex thread ID,
   repository, detailed turn state, last recoverable error, and capabilities.
3. Keep v1’s exact field set and phase projection unchanged.
4. Add named operations for interrupt and diagnostic projection; reject arbitrary
   shell commands or transcript injection over the socket.
5. Return stable error codes plus one recommended CLI command where recovery is
   known.
6. Pin v1 and v2 schemas with golden fixtures.

**Acceptance:** Old fixtures remain byte-compatible; v2 round trips all new
states; unknown versions fail with `unsupported_version` and a recovery command.

**Depends on:** DD-012.

**Unblocks:** DD-040, DD-042, DD-043.

## 5. Work package 2: unambiguous Codex attachment

### DD-020 — Define the Codex host-adapter contract

**Purpose:** Isolate volatile Codex integration from durable broker semantics.

**Files:**

- `voice_mode/broker/hosts/__init__.py` (new)
- `voice_mode/broker/hosts/base.py` (new)
- `voice_mode/broker/types.py`
- `tests/test_broker_host_contract.py` (new)

**Implementation:**

1. Define capability, thread summary, host turn, completion, approval, and error
   types without importing app-server or subprocess details.
2. Define methods for probe, list/read/attach/create thread, start/steer/interrupt
   turn, subscribe to events, query request disposition, and close.
3. Require every dispatch method to accept the broker `request_id`.
4. Classify failures as unsupported, unavailable, ambiguous, retryable transport,
   host rejection, approval required, or terminal agent failure.
5. Build an adapter conformance harness using a reusable abstract test suite.

**Acceptance:** A fake adapter demonstrates every capability and failure branch;
the kernel depends only on the base contract.

**Depends on:** DD-001.

**Unblocks:** DD-021.

### DD-021 — Build a bounded Codex app-server JSON-RPC transport

**Purpose:** Maintain one efficient Codex connection with cancellation,
notifications, and explicit request correlation.

**Files:**

- `voice_mode/broker/hosts/app_server_transport.py` (new)
- `tests/test_broker_app_server_transport.py` (new)
- `tests/fixtures/codex_app_server/` (new)

**Implementation:**

1. Start or connect to `codex app-server` over stdio or a user-owned Unix socket;
   never expose an unauthenticated non-loopback listener.
2. Implement initialize handshake, monotonically increasing JSON-RPC IDs,
   concurrent notification routing, bounded message size, deadlines, and clean
   cancellation.
3. Separate stdout protocol bytes from stderr diagnostics and retain only a
   bounded diagnostic tail.
4. Treat malformed JSON, duplicate response IDs, and unknown response IDs as
   protocol faults that close the connection.
5. Generate current Codex schemas in a developer command, but commit only small
   hand-curated fixtures needed by conformance tests.
6. Make executable and process factory injectable so unit tests spawn no Codex.

**Acceptance:** Fragmented messages, delayed notifications, cancellation,
malformed frames, process death, and restart are deterministic in tests.

**Depends on:** DD-020.

**Unblocks:** DD-022.

### DD-022 — Implement capability discovery and deterministic thread selection

**Purpose:** Put voice into a known thread and never silently select the wrong
conversation.

**Files:**

- `voice_mode/broker/hosts/app_server.py` (new)
- `voice_mode/broker/hosts/selection.py` (new)
- `voice_mode/config.py`
- `tests/test_broker_host_selection.py` (new)
- `tests/test_broker_app_server.py` (new)

**Implementation:**

1. Probe supported app-server methods at startup and construct a capability set.
2. Normalize repository roots before comparing threads.
3. Apply selection order: explicit thread; host registration; single matching
   active repository thread; otherwise create a broker-owned thread.
4. If multiple matching threads exist and no explicit selection is available,
   do not guess. Create a clearly labeled broker-owned thread for unattended
   voice, and print the alternatives plus exact attach command.
5. Persist adapter kind and selected thread in runtime/journal before capture is
   opened.
6. Add `--thread`, `--new-thread`, and `--adapter auto|app-server|exec` options;
   `auto` is the default and reports its resolved choice.

**Acceptance:** Selection tests cover zero, one, and many matching threads;
status always names the exact adapter and thread before dispatch.

**Depends on:** DD-021.

**Unblocks:** DD-023.

### DD-023 — Implement app-server turn, steering, interruption, and approvals

**Purpose:** Replace one-subprocess-per-turn behavior with host-native lifecycle
events and make real interruption possible.

**Files:**

- `voice_mode/broker/hosts/app_server.py`
- `voice_mode/broker/hosts/events.py` (new)
- `voice_mode/broker/handsfree.py`
- `tests/test_broker_app_server.py`
- `tests/test_broker_app_server_events.py` (new)

**Implementation:**

1. Map broker request IDs to app-server client user-message IDs.
2. Use turn start while idle and turn steer only when host capability and current
   turn state allow it. A rejected steer remains pending for the next turn; it
   is not silently converted into a new unrelated turn.
3. Convert completion notifications into one `CanonicalResponse` and ignore
   duplicate terminal notifications for the same request.
4. Map broker interruption to host turn interrupt and wait for terminal
   cancellation evidence with a bounded deadline.
5. Surface approval requests visually with thread/request identity. Do not speak
   command text or auto-approve through VoiceMode.
6. Keep model, reasoning, sandbox, permissions, and approval policy owned by the
   selected Codex thread unless the user explicitly overrides them.

**Acceptance:** Conformance tests cover start, steer, unsupported steer,
interrupt, approval, completion, duplicated events, and out-of-order events.

**Depends on:** DD-022.

**Unblocks:** DD-024, DD-025.

### DD-024 — Add host reconnection and disposition-based recovery

**Purpose:** Reconnect without losing the thread or duplicating work.

**Files:**

- `voice_mode/broker/hosts/app_server.py`
- `voice_mode/broker/recovery.py` (new)
- `voice_mode/broker/journal.py`
- `voice_mode/broker/runtime.py`
- `tests/test_broker_recovery.py` (new)

**Implementation:**

1. On transport loss, freeze new dispatches but keep local stop/status available.
2. Reconnect with bounded exponential backoff and jitter supplied by an injected
   scheduler for deterministic tests.
3. Reattach the explicit thread, query its recent turns, and correlate the
   broker request ID/client user-message ID.
4. If the host proves completion, resume canonical presentation once. If it
   proves absence before dispatch confirmation, allow an explicit retry action.
   If evidence is ambiguous, mark `recovery_uncertain` and require user action.
5. Persist every recovery decision and expose its exact rationale through
   diagnostics.
6. Open a circuit after repeated failures and stop recurring audio feedback.

**Acceptance:** Fault injection before/after dispatch and completion produces no
duplicate host turns or presentations and always yields a diagnosable state.

**Depends on:** DD-011, DD-012, DD-023.

**Unblocks:** DD-026, DD-045.

### DD-025 — Bring the exec adapter under the host contract

**Purpose:** Preserve compatibility while making its separate-session semantics
and weaker capabilities explicit.

**Files:**

- `voice_mode/broker/codex.py`
- `voice_mode/broker/hosts/exec.py` (new)
- `tests/test_broker_codex.py`
- `tests/test_broker_exec_host.py` (new)

**Implementation:**

1. Wrap current command construction and JSONL parsing in `ExecCodexAdapter`.
2. Advertise create/resume/completion capabilities but not active-session attach,
   same-turn steer, or reliable remote interruption.
3. Continue schema-constrained one-response output and `mcp_servers={}`.
4. Attach the returned complete thread ID before presentation.
5. Ensure cancellation terminates the process group without presenting partial
   output as a final.
6. Improve all errors to name the executable, repository, exit status, bounded
   stderr tail, and exact resume or configuration command.

**Acceptance:** The shared conformance suite passes for supported capabilities;
unsupported methods fail before dispatch with explicit alternatives.

**Depends on:** DD-020, DD-023.

**Unblocks:** DD-026.

### DD-026 — Integrate host adapters into the hands-free loop

**Purpose:** Remove the current context split and make adapter/thread identity
part of the normal user experience.

**Files:**

- `voice_mode/broker/handsfree.py`
- `voice_mode/cli_commands/broker.py`
- `voice_mode/config.py`
- `tests/test_broker_handsfree.py`
- `tests/test_broker_cli.py`

**Implementation:**

1. Replace the concrete `CodexAdapter` factory with host selection and one
   lifecycle-managed adapter instance.
2. Resolve and display adapter/thread/repository before announcing readiness.
3. Route accepted requests through runtime claim → journal → host dispatch →
   canonical presenter.
4. Keep control intents local and preserve follow-up thread affinity.
5. Handle host events asynchronously so remote stop and interruption remain
   responsive during Codex work.
6. Preserve `--daemon-only` and the protocol-only testing surface.

**Acceptance:** A live app-server session retains context across voice turns;
exec fallback is explicitly labeled; the previously observed “separate child
does not know this conversation” failure cannot occur silently.

**Depends on:** DD-013, DD-024, DD-025.

**Unblocks:** DD-050.

## 6. Work package 3: quiet realtime audio interaction

### DD-030 — Extract a configurable hybrid endpoint detector

**Purpose:** Recognize natural completion promptly without noise keeping capture
open or short pauses truncating speech.

**Files:**

- `voice_mode/broker/endpointing.py` (new)
- `voice_mode/broker/audio.py`
- `voice_mode/config.py`
- `tests/test_broker_endpointing.py` (new)
- `tests/test_broker_audio.py`

**Implementation:**

1. Move trailing-window VAD logic into a pure detector consuming frame metadata.
2. Track an adaptive noise floor, VAD votes, voiced/silent durations, minimum and
   maximum utterance duration, and explicit push-to-talk release.
3. Keep the mostly-silent vote window so isolated false speech cannot reset the
   entire endpoint timer.
4. Expose a secondary linguistic-completion input for future partial STT; it may
   shorten a sufficiently silent window but never end speech on its own.
5. Emit endpoint reason and timing for diagnostics.
6. Create synthetic tests for fan noise, intermittent false positives, long
   pauses, clipped word endings, and continuous speech.

**Acceptance:** Target fixtures endpoint within the configured p95 budget with no
known sentence truncation; all decisions are deterministic from frame inputs.

**Depends on:** DD-010.

**Unblocks:** DD-031, DD-032, DD-034.

### DD-031 — Add cancellation-aware audio playback and device recovery

**Purpose:** Stop playback immediately and survive routine device changes while
retaining one audio owner.

**Files:**

- `voice_mode/broker/audio.py`
- `voice_mode/audio_player.py`
- `voice_mode/broker/audio_session.py` (new)
- `tests/test_broker_audio.py`
- `tests/test_broker_audio_session.py` (new)

**Implementation:**

1. Wrap persistent input and output in `AudioSession` with explicit start, mute,
   listen, play, cancel playback, reopen device, and close methods.
2. Preserve single buffered playback while adding a cancellation token checked
   by the player at bounded intervals.
3. Ensure stopping playback cannot replay buffered audio or leave output handles
   alive.
4. Detect device disappearance/default-device change, reopen with bounded
   backoff, and retain broker/host session state.
5. Serialize device reconfiguration through the audio owner; no callback may
   independently recreate streams.
6. Add fake-device tests for loss during listen/play, reopen failure, cancel race,
   and final close.

**Acceptance:** Hot cancellation stops the fake player once; device rotations
leak no streams and never create a second owner.

**Depends on:** DD-030.

**Unblocks:** DD-033, DD-035, DD-045.

### DD-032 — Add activation and push-to-talk adapters

**Purpose:** Provide a deterministic control when wake recognition is imperfect.

**Files:**

- `voice_mode/broker/activation.py` (new)
- `voice_mode/broker/hotkey.py` (new)
- `voice_mode/broker/audio.py`
- `voice_mode/cli_commands/broker.py`
- `voice_mode/config.py`
- `tests/test_broker_activation.py` (new)

**Implementation:**

1. Define activation events for wake, push-to-talk press/release, toggle, sleep,
   and interrupt.
2. Keep current local STT wake matching as the compatibility adapter.
3. Add a platform hotkey adapter behind an optional dependency and a terminal
   key fallback for foreground development.
4. Make press open capture and release force an exact endpoint.
5. Detect hotkey registration conflicts at startup and print the exact config
   needed to choose another binding.
6. Ensure activation adapters never access Codex or presentation directly.

**Acceptance:** Fake activation tests drive the kernel deterministically; hotkey
failure leaves wake and foreground key controls usable.

**Depends on:** DD-030.

**Unblocks:** DD-033, DD-035.

### DD-033 — Implement interruption and capability-aware barge-in

**Purpose:** Let the user stop speech and redirect Codex without waiting for TTS.

**Files:**

- `voice_mode/broker/barge_in.py` (new)
- `voice_mode/broker/audio_session.py`
- `voice_mode/broker/handsfree.py`
- `voice_mode/broker/hosts/base.py`
- `tests/test_broker_barge_in.py` (new)

**Implementation:**

1. Hotkey interruption immediately cancels playback and emits a kernel barge-in
   event before opening capture.
2. Acoustic barge-in is enabled only when device topology and echo/noise checks
   meet a conservative capability threshold; headphones are the first supported
   reliable path.
3. For an active host turn, call host interrupt or steer according to declared
   capabilities and user intent. Unsupported host interruption still stops TTS
   and queues the new utterance for the next safe turn.
4. Resolve stop-vs-completion races by request ID and reducer order.
5. Emit latency from activation to playback cancellation.

**Acceptance:** Race tests prove no old audio resumes and no redirected utterance
is dispatched twice; hotkey interruption meets the p95 target in benchmarks.

**Depends on:** DD-023, DD-031, DD-032.

**Unblocks:** DD-035, DD-050.

### DD-034 — Formalize cue and transcript presentation policy

**Purpose:** Make audio feedback informative once, then quiet.

**Files:**

- `voice_mode/broker/presentation.py`
- `voice_mode/broker/cues.py` (new)
- `voice_mode/broker/handsfree.py`
- `voice_mode/config.py`
- `tests/test_broker_cues.py` (new)

**Implementation:**

1. Map rising, falling, interruption, and failure cues to named reducer events.
2. Record cue disposition per event so retries cannot replay it.
3. Suppress cues for idle, blank transcript, ambient speech, polling, and routine
   follow-up expiration.
4. Render one TTY state line and append normal lines in non-TTY mode.
5. Add deterministic JSONL state output with no ANSI and no transcript unless
   explicitly requested and authorized.
6. Keep acknowledgment phrases local and silent.

**Acceptance:** Event-sequence golden tests produce the exact expected cue and
terminal stream with zero duplicate or overlapping cues.

**Depends on:** DD-013, DD-030.

**Unblocks:** DD-035, DD-043.

### DD-035 — Establish audio latency and hardware qualification

**Purpose:** Turn subjective “chunky” feedback into repeatable release evidence.

**Files:**

- `scripts/benchmark-broker-audio.py` (new)
- `tests/manual/test_broker_hardware.py` (new)
- `docs/maintainers/broker-hardware-matrix.md` (new)
- `docs/reference/broker.md`

**Implementation:**

1. Benchmark wake acknowledgment, endpoint delay, submission state, playback
   cancel, device reopen, and first TTS audio with monotonic clocks.
2. Record p50/p95/max and environment metadata as JSON; do not record audio.
3. Define manual matrices for built-in, USB, wired headphones, Bluetooth,
   device rotation, fan noise, music, and speaker playback.
4. Add a 100-turn automated synthetic soak for audio resource leaks.
5. Fail the qualification command when published budgets regress.

**Acceptance:** The reference macOS setup meets design budgets; unsupported
acoustic barge-in environments clearly fall back to the hotkey.

**Depends on:** DD-031, DD-032, DD-033, DD-034.

**Unblocks:** DD-050.

## 7. Work package 4: self-healing operations and visibility

### DD-040 — Add supervised broker lifecycle

**Purpose:** Replace manual foreground babysitting with one idempotent service.

**Files:**

- `voice_mode/broker/supervisor.py` (new)
- `voice_mode/templates/launchd/com.voicemode.broker.plist` (new)
- `voice_mode/templates/systemd/voicemode-broker.service` (new)
- `voice_mode/templates/scripts/start-voicemode-broker.sh` (new)
- `tests/test_broker_supervisor.py` (new)
- `tests/test_service_file_updates.py`

**Implementation:**

1. Reuse existing service rendering conventions rather than creating another
   installer framework.
2. Run the broker as the only socket/audio owner with restart backoff and stable
   log locations.
3. Add install/start/stop/restart/status operations with injected platform
   command runners.
4. Make start idempotent and reject a second owner after liveness validation.
5. Keep foreground `broker run` for development and hardware testing.
6. Isolate all tests under the existing fake home fixture.

**Acceptance:** launchd/systemd golden tests are deterministic; repeated start
does not spawn another broker; stop removes no user data.

**Depends on:** DD-014.

**Unblocks:** DD-041, DD-046.

### DD-041 — Add intuitive top-level lifecycle commands

**Purpose:** Make the first command a user or agent guesses work.

**Files:**

- `voice_mode/cli.py`
- `voice_mode/cli_commands/broker.py`
- `voice_mode/cli_commands/lifecycle.py` (new)
- `tests/test_broker_cli.py`
- `tests/test_cli_lifecycle.py` (new)

**Implementation:**

1. Add `voicemode start`, `stop`, and `restart` backed by the supervisor client.
2. Preserve `broker run/status/stop`; help teaches canonical top-level commands.
3. Add typo/obsolete-command suggestions, including the earlier missing broker
   command scenario when installed CLI and docs disagree.
4. Document exit codes for success, user input, safety refusal, environment
   failure, upstream failure, and conflict.
5. Honor non-TTY, `NO_COLOR`, `CI`, and JSON stdout/stderr discipline.

**Acceptance:** Bare and mistaken lifecycle invocations produce a useful result
or exact correction; CLI golden tests pin help and exit behavior.

**Depends on:** DD-040.

**Unblocks:** DD-046.

### DD-042 — Implement startup compatibility handshake

**Purpose:** Detect installation drift before opening the microphone.

**Files:**

- `voice_mode/broker/compatibility.py` (new)
- `voice_mode/broker/server.py`
- `voice_mode/broker/hosts/app_server.py`
- `voice_mode/provider_discovery.py`
- `tests/test_broker_compatibility.py` (new)

**Implementation:**

1. Collect CLI/package version, broker protocol range, plugin metadata when
   available, local provider health/capabilities, and Codex host methods.
2. Compare protocol ranges and required feature capabilities, not package
   version strings alone.
3. Classify hard blockers, degraded capabilities, and advisory updates.
4. Run the hard-blocker check before readiness speech or capture.
5. Return one exact update/reinstall/restart command per blocker.
6. Expose the result in status/capabilities without secrets.

**Acceptance:** A stale editable install, old plugin, unavailable STT, and Codex
without app-server each produce deterministic supported/fallback/block results.

**Depends on:** DD-014, DD-022.

**Unblocks:** DD-046, DD-050.

### DD-043 — Build diagnostic projections and machine-readable capabilities

**Purpose:** Answer what the system is doing without requiring log archaeology.

**Files:**

- `voice_mode/broker/diagnostics.py` (new)
- `voice_mode/cli_commands/status.py`
- `voice_mode/cli_commands/broker.py`
- `voice_mode/broker/client.py`
- `tests/test_broker_diagnostics.py` (new)
- `tests/test_broker_cli.py`

**Implementation:**

1. Project journal/runtime state into current health and last-turn summaries.
2. Extend `status --json` with supervisor, broker, audio, providers, adapter,
   thread, request, phase, queue, latency, and recoverable error fields.
3. Add `capabilities --json` with schema version, feature flags, command list,
   environment keys, and exit-code dictionary.
4. Sort keys/lists deterministically and honor `SOURCE_DATE_EPOCH` in fixtures.
5. Keep human status short and actionable; detailed data belongs in JSON.
6. Redact transcript, credentials, endpoint tokens, and home-path details.

**Acceptance:** JSON validates against pinned schemas, is byte-identical across
repeated fixture runs, and pipes directly into `jq` without diagnostics.

**Depends on:** DD-011, DD-014, DD-034.

**Unblocks:** DD-044.

### DD-044 — Add doctor and explain-last commands

**Purpose:** Turn failures into a diagnosis and exact next action.

**Files:**

- `voice_mode/cli_commands/doctor.py` (new)
- `voice_mode/cli_commands/explain_last.py` (new)
- `voice_mode/broker/diagnostics.py`
- `voice_mode/cli.py`
- `tests/test_broker_doctor.py` (new)

**Implementation:**

1. `doctor` checks ownership, supervisor, socket, audio device, STT/TTS,
   compatibility, Codex transport, journal integrity, and configured fallbacks.
2. Each failed check includes component, evidence, severity, retryability, and
   one exact remediation command.
3. `explain-last` reconstructs capture, endpoint, transcription, dispatch,
   completion, visible presentation, and speech disposition with durations.
4. Transcript text appears only when persistence is enabled and the user asks
   for content; otherwise report length/hash-safe metadata.
5. Both commands support deterministic JSON and useful non-zero exit categories.

**Acceptance:** Fixture failures each lead to the expected single remediation;
healthy output is concise and exits zero.

**Depends on:** DD-043.

**Unblocks:** DD-046, DD-050.

### DD-045 — Coordinate recovery and circuit breaking

**Purpose:** Ensure local subsystem failures degrade predictably instead of
creating loops, duplicate cues, or stuck phases.

**Files:**

- `voice_mode/broker/recovery.py`
- `voice_mode/broker/handsfree.py`
- `voice_mode/broker/audio_session.py`
- `voice_mode/broker/server.py`
- `tests/test_broker_fault_matrix.py` (new)

**Implementation:**

1. Centralize retry budgets and circuit state by audio, STT, TTS, Codex, journal,
   and socket subsystem.
2. Define retryable vs terminal failures and the safe broker phase after each.
3. Stop recurring cues after a circuit opens; status and hotkey remain usable.
4. Never let TTS or journal failure alter host dispatch disposition.
5. Restore a subsystem only after a successful health probe and journal the
   transition.
6. Use injected clocks/backoff to keep fault tests fast.

**Acceptance:** The fault matrix reaches a stable diagnosable state for every
subsystem failure with no unbounded retry, duplicate dispatch, or stale lock.

**Depends on:** DD-024, DD-031.

**Unblocks:** DD-046, DD-050.

### DD-046 — Update installation, migration, and user documentation

**Purpose:** Make the reliable path the obvious installed path without breaking
existing one-shot users.

**Files:**

- `README.md`
- `docs/reference/broker.md`
- `docs/reference/cli.md`
- `docs/guides/configuration.md`
- `docs/tutorials/getting-started.md`
- `CHANGELOG.md`
- installer/plugin metadata and templates selected during implementation

**Implementation:**

1. Make `voicemode start/status/doctor` the daily workflow and retain foreground
   commands as development tools.
2. Explain app-server attachment, exec fallback, explicit thread identity, and
   how to resume/focus a thread.
3. Document cue meanings, push-to-talk, interruption, transcript/audio privacy,
   and explain-last.
4. Add an idempotent migration from foreground-only configuration to supervised
   service without overwriting user settings.
5. Keep the Unreleased changelog accurate as each user-facing slice lands.
6. Add install verification that checks CLI/plugin/broker compatibility.

**Acceptance:** A fresh install and an upgrade both reach healthy doctor output;
the README command exists in the installed binary used by the test.

**Depends on:** DD-040, DD-041, DD-042, DD-044, DD-045.

**Unblocks:** DD-050.

## 8. Cross-package verification and release

### DD-050 — Build conformance, fault-injection, and endurance gates

**Purpose:** Prove the four packages work together under the failures that make a
hands-free system frustrating in real life.

**Files:**

- `tests/integration/test_broker_daily_driver.py` (new)
- `tests/integration/test_broker_faults.py` (new)
- `tests/integration/test_broker_app_server.py` (new)
- `scripts/soak-broker.py` (new)
- focused CI workflow configuration if runtime permits

**Implementation:**

1. Run complete fake-service sessions across app-server and exec adapters.
2. Inject process death and disconnect at every turn/presentation boundary.
3. Race stop, barge-in, remote shutdown, host completion, and device loss.
4. Assert one dispatch, one visible final, at-most-one TTS play, one explicit
   thread, bounded queue, closed resources, and diagnosable final state.
5. Run 100 synthetic turns automatically and provide an opt-in eight-hour soak.
6. Add JSON schema, stdout/stderr, determinism, and non-TTY checks.
7. Run existing converse/control/service regressions to protect one-shot mode.

**Acceptance:** All focused and regression suites pass; the synthetic soak has
zero duplicate, leaked stream, stuck phase, or growing queue.

**Depends on:** DD-013, DD-026, DD-035, DD-042, DD-044, DD-045, DD-046.

**Unblocks:** DD-051.

### DD-051 — Perform live qualification and default-on decision

**Purpose:** Require real audio and real Codex evidence before declaring the
daily-driver experience complete.

**Files:**

- `docs/maintainers/broker-hardware-matrix.md`
- `docs/reference/broker.md`
- `CHANGELOG.md`
- release evidence artifact chosen by repository convention

**Implementation:**

1. Install the editable package exactly as a user would and validate installed
   help, capabilities, supervisor, status, and doctor outside the source shell.
2. Attach to a real Codex app-server thread, prove context continuity, then run
   exec fallback and prove its separate-thread labeling.
3. Exercise wake, push-to-talk, natural endpoint, follow-up, acknowledgment,
   approval, hotkey interruption, device rotation, provider restart, broker
   restart, and explain-last.
4. Run the eight-hour soak on the reference machine and record privacy-safe
   aggregate results.
5. Keep the feature opt-in if any hard invariant or performance budget fails;
   list the exact blocker rather than weakening the gate.
6. When green, update docs/changelog to make supervised mode the recommended
   default while retaining one-shot `converse` as fallback.

**Acceptance:** All design performance budgets and correctness invariants pass
on real hardware; the active thread is always visible; recovery needs no manual
process hunting; no duplicate response occurs.

**Depends on:** DD-050.

**Unblocks:** default-on daily-driver release.

## 9. Verification commands

Each task runs its narrow tests first. Milestones run these gates from the repo
root; implementation may add narrower commands as files land.

```bash
uv run ruff check voice_mode/broker voice_mode/cli_commands tests/test_broker_*.py
uv run pytest tests/test_broker_*.py -q
uv run pytest tests/test_converse_*.py tests/test_control_status.py -q
uv run pytest tests/test_service_file_updates.py tests/test_service_health_checks.py -q
git diff --check
```

App-server tasks also generate the installed Codex schema into a temporary
directory and compare only the capability methods used by VoiceMode. The full
generated schema is not committed because it is large and version-specific.

Milestone 4 adds integration, fault, benchmark, and soak commands introduced by
DD-035 and DD-050. Hardware tests remain opt-in and must never run in normal CI.

## 10. Milestones and stopping rules

### Milestone A — Kernel certainty

Complete DD-001 through DD-014. Stop if any failure path can emit two dispatch
or presentation intents. Do not begin host integration on an ambiguous state
model.

### Milestone B — Correct Codex session

Complete DD-020 through DD-026. Stop if app-server capability detection relies
only on a version string, thread selection can silently choose among multiple
threads, or reconnect can redispatch an uncertain request.

### Milestone C — Natural interaction

Complete DD-030 through DD-035. Stop if push-to-talk is unavailable when wake or
acoustic barge-in fails, if playback cannot be cancelled, or if device recovery
can create a second audio owner.

### Milestone D — Boring operations

Complete DD-040 through DD-046. Stop if install drift is detected only after
capture begins, JSON output mixes diagnostics, or doctor cannot name an exact
recovery command.

### Milestone E — Release evidence

Complete DD-050 and DD-051. Default-on status requires both automated invariants
and live hardware evidence. A partially green soak is not a release pass.

## 11. Commit and ownership strategy

Use one commit per task or cohesive subtask, always naming the task ID. Stage
only the exact files owned by that task; never stage unrelated `CLAUDE.md` or
`.pi-flywheel/` changes. Tasks that touch shared hubs (`types.py`, `runtime.py`,
`handsfree.py`, `cli.py`, `config.py`) run serially unless exact line/file
ownership is coordinated.

Recommended commit shapes:

```text
test(broker): pin daily-driver baselines (DD-001)
feat(broker): add deterministic turn reducer (DD-010)
feat(broker): add privacy-safe turn journal (DD-011)
feat(codex): attach broker through app server (DD-023)
feat(audio): add deterministic interruption (DD-033)
feat(cli): add broker doctor and explain-last (DD-044)
test(broker): certify daily-driver fault matrix (DD-050)
```

After every task: focused tests, `git diff --check`, exact-file commit, task
closeout, dependency sync, and a clean check showing only pre-existing unrelated
work.

## 12. Task conversion map

Create one Beads epic for the daily-driver release and one task per `DD-*`
heading. Preserve the dependency graph from section 3. Suggested priorities:

- P0: DD-010, DD-011, DD-012, DD-013, DD-020, DD-021, DD-022, DD-023,
  DD-024, DD-026, DD-050, DD-051.
- P1: DD-001, DD-014, DD-025, DD-030, DD-031, DD-032, DD-033, DD-034,
  DD-040, DD-042, DD-043, DD-044, DD-045.
- P2: DD-035, DD-041, DD-046.

Every task description copies its purpose, files, implementation steps,
acceptance criteria, dependencies, and verification command from this plan so a
fresh implementation agent can execute it without reopening the design chat.

## 13. Definition of done

The project is complete only when:

- every DD task is closed with a commit and verification evidence;
- dependency-cycle check is empty;
- all automated gates and the eight-hour live qualification pass;
- app-server context continuity and explicit exec fallback are demonstrated;
- there are zero duplicate dispatches, visible finals, or TTS plays in fault and
  endurance testing;
- hotkey push-to-talk and interruption remain available when acoustic behavior
  is unsupported;
- supervisor restart, provider restart, and device rotation recover without
  losing thread identity;
- status, capabilities, doctor, and explain-last are deterministic and useful;
- one-shot `converse` and protocol-v1 compatibility tests remain green;
- docs and installed CLI agree on every recommended command.

## 14. Normative data contracts

These field sets prevent parallel tasks from inventing incompatible shapes.
Implementation may add optional fields, but required fields and meanings change
only through a protocol-versioned migration.

### TurnEnvelope

```json
{
  "schema_version": 1,
  "utterance_id": "opaque-uuid",
  "request_id": "opaque-uuid",
  "broker_session_id": "opaque-uuid",
  "repo_root": "/canonical/repository",
  "host_adapter": "app-server",
  "host_thread_id": "opaque-host-id",
  "state": "accepted",
  "transcript": "synthetic example",
  "control_intent": null,
  "accepted_at": "2026-07-19T00:00:00Z"
}
```

Rules:

- `utterance_id` exists at capture start; `request_id` exists only after an
  eligible transcript or local control intent is accepted.
- Transcript is present in live memory. Journal serializers remove it unless
  transcript persistence is enabled.
- `repo_root` is canonicalized before the envelope is created and never changes
  during a session.
- `host_thread_id` is required before a normal request may reach `dispatched`.
- A local control intent has a request ID for traceability but no host thread
  dispatch record.

### DispatchClaim

```json
{
  "request_id": "opaque-uuid",
  "disposition": "claimed",
  "adapter": "app-server",
  "thread_id": "opaque-host-id",
  "claimed_at_monotonic_ms": 12345,
  "confirmed": false
}
```

Allowed dispositions are `unclaimed`, `claimed`, `confirmed`, `completed`,
`cancelled`, `recovery_uncertain`, and `terminal_failure`. Only `unclaimed` may
transition to `claimed`. No state transitions back to `unclaimed`.

### CanonicalResponse

```json
{
  "schema_version": 1,
  "request_id": "opaque-uuid",
  "thread_id": "opaque-host-id",
  "display_text": "One complete final response.",
  "spoken_text": "One complete final response.",
  "host_turn_id": "opaque-host-turn-id",
  "completed_at": "2026-07-19T00:00:01Z"
}
```

Rules:

- `display_text` is immutable once accepted.
- `spoken_text` is a deterministic excerpt or validated host field generated
  from the same model completion; it is never a second model call.
- Empty display text is a terminal host error, not a presentable response.
- Markdown and code remain in display text. Spoken text strips presentation
  markup and excludes code/table content.
- Presenter state, rather than response content, decides whether display or TTS
  has already occurred.

### JournalRecord

```json
{
  "schema_version": 1,
  "sequence": 42,
  "event": "dispatch_claimed",
  "broker_session_id": "opaque-uuid",
  "request_id": "opaque-uuid",
  "utterance_id": "opaque-uuid",
  "thread_id": "opaque-host-id",
  "adapter": "app-server",
  "phase_before": "listening",
  "phase_after": "thinking",
  "duration_ms": 81,
  "provider": null,
  "error_code": null,
  "recorded_at": "2026-07-19T00:00:00Z"
}
```

Rules:

- Sequence is monotonic within a journal file and used to detect gaps.
- Wall-clock values are evidence fields, never ordering authorities.
- Monotonic durations cannot be reconstructed across process restart; recovery
  begins a new process epoch recorded in the journal.
- Error details are bounded and redacted before persistence.
- A corrupt record before the final line blocks automatic recovery and routes
  to doctor; only one incomplete tail is safely ignored.

### BrokerStatusV2

```json
{
  "schema_version": 2,
  "running": true,
  "supervisor": {"kind": "launchd", "healthy": true},
  "broker": {"phase": "thinking", "uptime_ms": 120000},
  "session": {
    "id": "opaque-uuid",
    "repo_root": "/canonical/repository",
    "adapter": "app-server",
    "thread_id": "opaque-host-id"
  },
  "turn": {
    "request_id": "opaque-uuid",
    "state": "dispatched",
    "pending_count": 0
  },
  "audio": {"input": "healthy", "output": "healthy"},
  "providers": {"stt": "healthy", "tts": "healthy"},
  "last_error": null,
  "recommended_action": null
}
```

Rules:

- Human status may abbreviate the thread ID; JSON always returns the complete ID.
- Lists and map-derived output are sorted for byte stability.
- Absence is represented with `null` or an empty list, never an omitted field in
  the pinned schema.
- Diagnostic warnings go to stderr and never contaminate JSON stdout.

### CapabilitiesV1

Required sections are `contract_version`, `package_version`, `protocol_range`,
`commands`, `host_adapters`, `audio_features`, `providers`, `environment`, and
`exit_codes`. Each feature carries `supported`, `available`, and a reason when
unavailable, so an agent can distinguish build capability from live health.

## 15. Fault and recovery matrix

This matrix is normative for DD-024, DD-045, and DD-050. “Retry” means bounded
retry under the recovery coordinator, never an independent loop inside a driver.

| Fault | Required state | Automatic action | Forbidden action | User surface |
|------|----------------|------------------|------------------|--------------|
| Input device absent at startup | degraded | probe alternatives | open capture | doctor command |
| Input device disappears asleep | recovering | reopen device | create second stream | status warning |
| Input device disappears capturing | failed turn | end capture, reopen | dispatch partial audio | failure cue once |
| Output device disappears before TTS | visual complete | reopen for future | replay current response | visual notice |
| Output device disappears during TTS | TTS uncertain | stop player, reopen | replay from start | explain-last |
| VAD marks ambient noise as speech | capturing | trailing-window endpoint | repeated cues | no cue until accept |
| STT returns blank marker | listening/asleep | discard turn | dispatch blank prompt | silent |
| STT times out once | recovering | bounded retry | new request ID | state line |
| STT circuit opens | degraded | keep PTT/status | recurring spoken errors | doctor action |
| TTS synthesis fails | visual complete | mark TTS failure | regenerate model response | visual notice |
| TTS playback throws | TTS failed | cancel/close output | replay audio | explain-last |
| Journal append fails before claim | accepted | block dispatch | dispatch without claim | exact disk action |
| Journal append fails after host completion | host complete | retain memory state | duplicate presentation | visual warning |
| Journal has torn final line | recovering | ignore tail only | rewrite history | doctor advisory |
| Journal has interior corruption | blocked | preserve file | guess recovery state | doctor blocker |
| App server unavailable at startup | fallback | select exec if allowed | claim attachment | readiness label |
| App server dies before claim | accepted | reconnect/fallback | mark dispatched | state line |
| App server dies after claim | uncertain | query disposition | redispatch | explain-last action |
| Duplicate host completion | completed | ignore duplicate | present twice | diagnostic counter |
| Completion arrives after interrupt | reducer decides | correlate request/turn | revive old audio | status |
| Approval requested | waiting approval | forward visually | speak command/approve | owning host surface |
| Approval host disappears | paused | reconnect owner | auto-deny/approve silently | exact resume action |
| Exec child exits non-zero | failed turn | close process group | use partial final | stderr tail/action |
| Exec child ignores cancellation | recovering | terminate then kill boundedly | block forever | status warning |
| Broker socket is live | healthy existing | attach client | unlink socket | current owner status |
| Broker socket is stale and owned | startup | replace safely | remove non-socket | startup notice |
| Broker socket ownership differs | blocked | refuse | unlink foreign path | permission action |
| Supervisor restarts broker | recovering | replay journal projection | replay TTS/dispatch | restored status |
| Stop races capture | asleep | cancel capture | dispatch late transcript | one transition |
| Stop races host completion | asleep/complete | persist completion | autoplay after stop | explain-last |
| Barge-in races TTS completion | listening | cancel token wins once | reopen old playback | interrupt state |
| Queue receives second utterance | queue full | preserve first | overwrite first | actionable error |
| Plugin/CLI protocol mismatch | blocked | stop before audio | capture then fail | reinstall command |

## 16. Four-round plan review record

The plan is not ready for task conversion until each round below is executed and
its findings are integrated. A round is complete only when structural findings
are resolved in this document, not merely noted.

### Round 1 — Architecture and dependency review

Checklist:

- Every production responsibility has one owner and no circular dependency.
- Kernel types do not import audio, CLI, subprocess, or app-server modules.
- Host adapters do not import presentation or device modules.
- Presenter cannot dispatch host work.
- Audio callbacks cannot mutate kernel state directly.
- Each task names concrete files, acceptance, dependencies, and an unblock.
- Critical-path tasks form an acyclic graph.
- Shared-file tasks are serialized or explicitly coordinated.

Integrated findings:

- Host contract begins from DD-001 in parallel with the kernel, while actual
  turn integration waits for idempotent runtime and presentation.
- Protocol v2 depends on runtime identity, not the journal reader, preventing
  diagnostics persistence from blocking live status.
- Audio endpointing depends only on the pure reducer’s event vocabulary, so it
  can proceed while host integration is underway.
- Operations consume protocol and journal projections; they do not reach into
  audio or host internals.

### Round 2 — Correctness, crash, and security review

Checklist:

- Every external side effect has a persistence boundary and idempotency rule.
- Every crash point has a recoverable, uncertain, or terminal classification.
- Uncertain never silently becomes retryable.
- Cancellation is correlated by request and host turn ID.
- Journal parsing fails closed on meaningful corruption.
- Socket and app-server transports are bounded and local/authenticated.
- Approval and sandbox ownership stay with Codex.
- Default diagnostics contain no transcript, audio, secret, or provider token.

Integrated findings:

- Dispatch claim persists before host invocation, accepting an uncertain window
  rather than risking duplicate work.
- Visible/TTS presentation state is claimed before side effects, which favors
  at-most-once behavior over invisible replay after a crash.
- The exec fallback terminates a process group on cancellation and never promotes
  partial stdout to a canonical final.
- Interior journal corruption blocks automatic recovery; only one torn tail is
  tolerated.

### Round 3 — User and agent ergonomics review

Checklist:

- The first guessed lifecycle commands work.
- Current adapter, repository, and complete thread ID are visible before work.
- Ambiguous thread selection never guesses.
- Every cue has one meaning and one emitting transition.
- Silence is the behavior for idle and routine timeout.
- Every error gives one exact recovery command.
- JSON is deterministic and diagnostics-free.
- Push-to-talk remains available when wake/barge-in is unreliable.

Integrated findings:

- `voicemode start/stop/restart/status/doctor/explain-last` are canonical, while
  current broker commands remain compatible and teach the new surface.
- Capability output distinguishes “supported by this build” from “available in
  this environment,” preventing agents from retrying impossible actions.
- Exec fallback is labeled before capture; it cannot surprise the user with a
  context-free child after submission.
- Acknowledgments stay local and silent; failure cues open visual diagnostics
  instead of speaking long errors.

### Round 4 — Verification, rollout, and steady-state review

Checklist:

- Every invariant has a unit, integration, fault, or live test.
- Performance budgets have benchmark instrumentation rather than assumed values.
- Existing converse, control, service, and protocol-v1 behavior has regression
  coverage.
- Hardware-only tests are opt-in and cannot hang CI.
- Install verification tests the installed executable, not source imports.
- Rollout can remain opt-in without maintaining a second architecture.
- The final task has objective default-on gates.

Integrated findings:

- DD-050 combines invariants across packages and DD-051 separately requires real
  hardware, preventing mocks from certifying audio quality.
- App-server schemas are generated during verification but only the used method
  fixtures are committed, avoiding version-specific repository bloat.
- Default-on is a configuration/recommendation change after the same runtime
  passes qualification; there is no forked “v2” implementation.
- One-shot converse remains the explicit fallback throughout rollout.

Steady-state result: the fourth review changed release evidence and clarified
boundaries but did not require a new component or reorder the critical path. The
plan is ready for Beads conversion.

## 17. Rollout and compatibility policy

### Stage 0 — Internal contracts

Land DD-001 through DD-014 without changing the recommended user workflow.
Protocol v1 remains default for existing clients; v2 is negotiated by new CLI
code. No supervisor is installed.

Rollback: revert the task commit. Journal files are additive and ignored by old
versions. Never delete a journal during rollback.

### Stage 1 — App-server opt-in

Land DD-020 through DD-026 with adapter default still set to `auto`, resolving to
app-server only when the full required capability set is available. Print the
resolved adapter before capture.

Rollback: configure `VOICEMODE_BROKER_CODEX_ADAPTER=exec`. Existing thread IDs
remain resumable; no migration rewrites Codex history.

### Stage 2 — Interaction qualification

Land DD-030 through DD-035. Hotkey/push-to-talk can be enabled independently of
acoustic barge-in. Acoustic barge-in stays disabled when capability checks fail.

Rollback: disable hotkey or acoustic interruption via documented settings while
retaining the same audio session and endpoint detector.

### Stage 3 — Supervised opt-in

Land DD-040 through DD-046. Installation offers the supervisor but does not
silently enable a login service for an existing user. `voicemode start` installs
or enables it with an explicit visible action.

Rollback: `voicemode stop` disables the supervisor while foreground `broker run`
continues to work. User configuration and journals remain intact.

### Stage 4 — Recommended daily driver

After DD-050 and DD-051 pass, documentation recommends `voicemode start` and the
installer may offer supervised startup during an interactive install. Existing
one-shot converse remains supported.

Rollback: change documentation and installer recommendation back to one-shot or
foreground mode; do not maintain two runtime implementations.

### Compatibility promises

- Protocol v1 remains covered until a future major-version removal process.
- Existing broker environment variables keep their meanings; new structured
  config commands write the same effective configuration layer.
- Existing `broker run`, `broker converse`, `broker status`, and `broker stop`
  commands remain functional aliases or development surfaces.
- Existing Codex exec threads remain resumable by their complete IDs.
- Existing transcript/audio opt-ins are not broadened by journaling.
- No supervisor install overwrites a user-edited service file without detecting
  and explaining the conflict.

## 18. Implementation-agent handoff template

Every Beads task should end with this evidence in its close reason or linked
message:

```text
Task: DD-xxx
Commit: <sha>
Files changed: <exact paths>
Behavior delivered: <one paragraph>
Focused verification: <commands and results>
Invariant evidence: <which numbered invariants were exercised>
Compatibility evidence: <old surface tests retained>
Known limitations: <none or explicit deferred item>
Unblocked tasks: <IDs>
```

An implementation agent starts by reading this plan’s constraints, its task
section, direct dependencies, and exact current files. It does not need to read
the original chat. If live code contradicts the plan, it records the evidence
and updates the plan/task before broadening scope.
