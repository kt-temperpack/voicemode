# GPT-Realtime Codex Operator Implementation Plan

**Status:** Ready for Beads and implementation; four reviews integrated

**Date:** 2026-07-20

**Design source:**
`docs/superpowers/specs/2026-07-20-gpt-realtime-codex-operator-design.md`

**Objective:** Add an opt-in realtime interaction plane in which
`gpt-realtime-2.1` owns conversational speech while the existing Codex
app-server adapter runs one asynchronous coding job. The result must preserve
one microphone owner, one speaking authority, one host dispatch per accepted
tool call, and one integration of each terminal worker result.

**Initial delivery:** macOS-first, loopback browser client, OpenAI WebRTC media,
OpenAI sideband control, one active Codex job, app-server adapter only, explicit
cloud-audio start, visible transcripts and job state, and no changes to classic
`converse` or broker hands-free behavior.

**Deferred:** native Rust/GPUI shell, local duplex model, mobile or remote
clients, multiple simultaneous Codex jobs, wake-to-cloud activation, video,
repository memory, exec-adapter fallback, and default-on installation.

## 1. Delivery rules

These rules are stronger than local implementation convenience. A task is not
complete if it violates one of them even when its focused tests pass.

1. Realtime mode is additive. `voicemode broker run`, `broker converse`, MCP
   `converse`, local Whisper, and Kokoro must work without an OpenAI key and
   without importing the realtime package during ordinary startup.
2. The reference browser carries media only. The Python sideband owns session
   instructions, tools, tool outputs, response creation, Codex control,
   journaling, credentials, and policy.
3. The standard OpenAI API key never appears in HTML, JavaScript, URLs, browser
   storage, status output, logs, exceptions returned to the browser, or test
   fixtures.
4. The local HTTP server binds to `127.0.0.1` on an ephemeral port. Remote binds,
   wildcard binds, forwarded-host operation, permissive CORS, and repository
   file serving are rejected by construction.
5. The browser receives a per-process capability in the URL fragment, removes
   it from browser history immediately, and sends it only in an authorization
   header. It never appears in a query string, access log, or referrer.
6. Realtime cloud audio starts only after the page shows the cloud boundary and
   the user explicitly presses Start. Loading the page cannot request the
   microphone or create an OpenAI call.
7. A process-local and file-backed audio lease prevents classic local capture
   and browser WebRTC capture from being active together. A lease conflict
   fails with the current owner and a recovery command; it never force-kills
   another process.
8. The response arbiter is the only component allowed to emit
   `response.create`. Browser JavaScript does not send that event.
9. User audio item IDs, Realtime function `call_id` values, Codex job IDs,
   Codex request IDs, host turn IDs, response IDs, and worker delivery IDs are
   distinct types in code and never substituted for one another.
10. Every committed user item is claimed before response creation. A claim can
    produce at most one response request, including across duplicate events,
    sideband reconnect, and process recovery.
11. Every mutating tool call is claimed before execution. Repeating the same
    Realtime `call_id` returns or preserves the recorded disposition without
    starting a second Codex turn, steer, or interrupt.
12. Codex dispatch uses a stable request ID. If transport fails after the
    dispatch boundary, recovery queries host evidence and never blindly starts
    the turn again.
13. One Codex job may be active. A second delegation returns `busy` with the
    active job ID; ordinary conversation, status, steering, and interruption
    remain available.
14. A user barge-in cancels Realtime speech but does not cancel the Codex job.
    Job interruption requires an explicit `interrupt_codex` tool call or local
    Cancel job control.
15. A Codex terminal event is journaled before it is shown or injected. Its
    delivery is claimed before network send and is never automatically replayed
    after an uncertain send.
16. Codex workers receive no voice tools and cannot call TTS. Realtime is the
    sole spoken persona; the UI labels Codex text as worker state or result.
17. Worker text is untrusted data. Only a bounded status, bounded summary, job
    ID, and thread ID enter a tool-disabled out-of-band response; neither the
    worker input nor its spoken integration enters the default conversation.
    The dispatcher also rejects any tool call caused by a worker delivery, so
    worker content cannot modify operator policy or authorize tools.
18. Codex sandbox and approval policy remain authoritative. An approval event
    is displayed with the thread and approval identifiers; realtime mode cannot
    answer the approval on the user's behalf.
19. Transcript and recording persistence remain separately opt-in. The default
    operator journal contains identifiers, state, durations, and error codes,
    but no transcript, audio, raw SDP, standard key, capability, or Realtime
    call ID.
20. Python 3.10 remains supported. Production code cannot require
    `asyncio.TaskGroup`, `asyncio.timeout`, `StrEnum`, or another 3.11-only API.
21. All network, clock, ID, browser-open, filesystem, and host-adapter effects
    are injectable. Automated tests never contact OpenAI or start a real Codex
    process merely because `OPENAI_API_KEY` is present in CI.
22. Existing uncommitted work in `voice_mode/broker/audio.py`,
    `voice_mode/broker/handsfree.py`, their tests, `CLAUDE.md`, `.pi-flywheel/`,
    and the untracked audio-ownership spec is user work. Do not stage, rewrite,
    or commit it as part of this plan.

## 2. User workflow

### 2.1 Start and consent

The user runs:

```text
voicemode broker realtime --repo /path/to/repository
```

Startup validates the API key, exact loopback bind, Codex app-server
capabilities, repository, thread selection, journal path, and classic-broker
compatibility guard without opening a microphone, acquiring the audio lease, or
creating a Realtime call. It then serves the reference UI, opens it through an
injected browser opener, and prints the same URL plus the complete Codex thread
ID and resume command.

The page begins in `ready`, with a visible “Cloud audio is off” state. An
authenticated Start control first asks the broker to acquire the audio lease;
only after that succeeds does the page request microphone permission, create
the WebRTC offer, and post it to the authenticated loopback SDP route. Failure
leaves the page stopped and releases every partially acquired resource.

### 2.2 Converse

After the SDP answer and sideband session are both ready, the page shows
`listening`. Browser WebRTC sends audio directly to the Realtime call. Semantic
VAD creates committed user audio items but does not automatically create model
responses. The sideband claims the item and asks the response arbiter to emit
one `response.create`.

Provider input-transcription deltas may update one provisional caption keyed by
`item_id` for low latency. Only sideband-normalized final transcript rows from
the authenticated local stream are authoritative; provider final events seen
on the browser data channel are never appended independently. Completion events
may arrive out of order, so final rows are ordered by conversation item
relationship and local receipt sequence rather than completion time alone.

Output audio plays from the remote WebRTC track. Provider output transcript
deltas may update one provisional operator row, while the sideband-normalized
local final freezes it exactly once. Starting to speak while output is active
lets Realtime cancel and truncate unheard WebRTC audio; the arbiter closes the
prior response as interrupted and claims the new user item normally.

### 2.3 Delegate and continue talking

When the user asks for coding work, the operator calls `delegate_codex`. The
sideband validates its arguments, claims the function `call_id`, creates a job,
starts one app-server turn with a stable request ID, and returns a JSON
`function_call_output` as soon as the host confirms the turn ID. The next
operator response may say the job started, but it cannot invent progress.

The Realtime session remains conversational while app-server events update the
job. `get_codex_job`, `steer_codex`, and `interrupt_codex` act only on the known
job. An approval event moves the job to `waiting_approval`, shows a visible
action with the Codex thread and approval ID, and leaves the voice session
available for unrelated conversation.

### 2.4 Complete and integrate once

The job manager records a terminal completion before publishing it to the UI.
Only a bounded summary enters the operator queue. If user speech or operator
audio is active, the completion waits. At the next idle boundary, the arbiter
claims the worker delivery and emits one out-of-band response with
`conversation: "none"`, response-local delimited worker data, and
`tool_choice: "none"`. This produces a short spoken integration without adding
the worker input or model output to the default conversation. The dispatcher
rejects any tool call correlated to a worker-delivery cause as defense in depth.

If the sideband disconnects after claiming delivery but before confirmation,
the result is visibly `delivery_uncertain` and is not spoken automatically.
The complete Codex result remains in the Codex thread, and the user may ask the
operator to summarize it in a new turn.

### 2.5 Stop, recover, and roll over

Stop session disables the microphone track, closes the peer connection and
sideband, asks the authenticated OpenAI call-hangup endpoint to end the WebRTC
call, releases the audio lease, and keeps a running Codex job alive for the
configured short grace period. Explicit Cancel job interrupts the app-server
turn. Closing the page follows Stop session semantics. Bilateral liveness
watchdogs cover crashes where `pagehide` never arrives: stale broker events make
the page disable its track and close its peer, while stale browser heartbeats
make the broker hang up the call before releasing the lease. Process shutdown
waits for bounded cleanup and never hangs on a remote socket.

At approximately 55 minutes, the session manager requests rollover at the next
idle boundary. The page creates a replacement peer, the broker creates and
configures the replacement call, and the old call closes only after the new
media and sideband are ready. The replacement receives a bounded summary,
active job identity, Codex thread identity, and undelivered event state. A
visible reconnect state is acceptable; cancelling the job or replaying a result
is not.

## 3. Current codebase map

The plan uses the existing seams instead of copying their protocols.

- `voice_mode/cli_commands/broker.py` owns the Click group. The new `realtime`
  command routes to a new runner and does not call `run_handsfree_broker`.
- `voice_mode/broker/hosts/base.py` defines `HostAdapter`, including thread
  selection, `start_turn`, `steer_turn`, `interrupt_turn`, event subscription,
  and disposition queries. RTOP-010 promotes request-recovery evidence to this
  interface so the job manager never downcasts or duck-types an app-server
  method. `HostAdapter` remains the worker boundary.
- `voice_mode/broker/hosts/app_server.py` owns Codex thread and turn semantics.
  `app_server_transport.py` owns JSON-RPC and process I/O, and `events.py` owns
  notification normalization and deduplication. Realtime code reuses all three.
- `voice_mode/broker/hosts/selection.py` already provides repository-aware,
  deterministic explicit, registered, autodetected, or created thread
  selection. Realtime mode calls it with app-server only.
- `HostTurnRunner` in `voice_mode/broker/handsfree.py` is deliberately blocking.
  It waits on a condition until a terminal host event. Realtime mode subscribes
  directly to `HostAdapter`; it does not modify or instantiate this runner.
- `voice_mode/broker/types.py` contains stable host and turn types. Realtime
  types live in their own package so the classic broker does not import
  provider-specific state.
- `voice_mode/broker/journal.py` provides an append-only, privacy-safe JSONL
  journal, bounded retention, torn-tail recovery, and injected clocks/writers.
  It will be extended compatibly with neutral correlation fields and reused.
- `voice_mode/broker/runtime.py`, `turns.py`, and `recovery.py` encode current
  at-most-once dispatch and evidence-based recovery. The new job manager follows
  the same decisions and reuses recovery helpers where their contracts fit.
- `voice_mode/broker/server.py` owns the classic Unix-socket protocol. The
  reference WebRTC HTTP server is separate; legacy protocol messages do not
  gain OpenAI payloads.
- `voice_mode/broker/diagnostics.py` projects stable public status. Realtime
  status will be an optional nested projection and cannot expose secrets or
  change existing keys.
- `aiohttp` and `httpx` are direct dependencies. Use `aiohttp` for the local
  server, multipart SDP request, and sideband WebSocket rather than adding a
  transitive `websockets` dependency.
- Hatch currently includes Python and template resources but not arbitrary
  `.html`, `.js`, or `.css` under the package. Reference assets go under
  `voice_mode/templates/realtime/`, which is already included in wheel and
  sdist rules, and are loaded with `importlib.resources`.
- Pytest uses asyncio auto mode, strict markers, xdist, temporary homes,
  injected transports, and deterministic IDs/clocks. Real service tests stay
  under `tests/manual/`, which ordinary runs ignore.

## 4. Target package and ownership

```text
voice_mode/broker/realtime/
  __init__.py              public runner and version-neutral exports only
  types.py                 IDs, enums, immutable snapshots, public results
  protocol.py              OpenAI event validation and client-event builders
  arbiter.py               pure single-response reducer and runtime wrapper
  jobs.py                  asynchronous one-job Codex manager
  transport.py             OpenAI SDP and sideband I/O adapter
  session.py               call lifecycle, event routing, reconnect, rollover
  security.py              capability, origin/host, limits, redaction helpers
  web.py                   loopback aiohttp application and local event stream
  audio_guard.py           realtime lease lifecycle and classic-owner probe
  status.py                privacy-safe projection and discovery record
  runner.py                composition root and bounded shutdown

voice_mode/broker/audio_lease.py
                            neutral cooperative cross-process capture lease

voice_mode/templates/realtime/
  index.html               semantic, accessible reference shell
  app.js                   WebRTC, captions, controls, local event rendering
  styles.css               compact state-first presentation

tests/fakes/realtime.py     shared clocks, IDs, host, HTTP, and sideband fakes
tests/test_broker_realtime_*.py
tests/manual/test_broker_realtime.py
```

`protocol.py` knows OpenAI's wire vocabulary but no Codex or HTTP server.
`jobs.py` knows `HostAdapter` but no OpenAI events. `arbiter.py` knows semantic
operator events and emits transport-neutral actions. `transport.py` performs
OpenAI I/O but owns no policy. `session.py` is the only orchestrator allowed to
join those planes. `web.py` owns the local trust boundary but never dispatches
Codex directly.

## 5. OpenAI contract pinned for the first slice

The implementation targets the current official contracts and isolates them
behind `OpenAIRealtimeTransport` and pure event builders.

### 5.1 Call creation

The browser posts its SDP offer to the authenticated local `/v1/session` route
with `Content-Type: application/sdp`. The broker sends a multipart form to:

```text
POST https://api.openai.com/v1/realtime/calls
Authorization: Bearer <server-side OPENAI_API_KEY>

part sdp: <offer SDP>
part session: <JSON session object>
```

The broker requires a successful status, non-empty SDP answer, and a `Location`
header ending in a validated `rtc_...` call identifier. Before releasing that
answer to the browser, the session manager attaches the sideband, confirms the
expected session profile, and enables the generation for browser readiness.
This prevents an interval in which the microphone can feed a call without the
server-side policy plane. The call ID remains in memory, is redacted from public
status, and is never persisted raw.

The official unified WebRTC guide uses this multipart server path. The simpler
raw `application/sdp` request shown for an ephemeral client is not the broker's
upstream contract.

### 5.2 Sideband

The broker attaches to the same call at:

```text
wss://api.openai.com/v1/realtime?call_id=<url-encoded call id>
Authorization: Bearer <server-side OPENAI_API_KEY>
```

The WebSocket accepts and emits JSON objects. Client events receive generated
`event_id` values so an OpenAI `error` event can be correlated to the attempted
mutation. Incoming messages larger than the configured bound, non-object JSON,
missing event types, unsupported item shapes, and excessive nesting produce a
typed local error rather than reaching the job manager.

### 5.3 Session profile

The initial call configuration and confirming `session.update` use the same
validated values:

```json
{
  "type": "realtime",
  "model": "gpt-realtime-2.1",
  "output_modalities": ["audio"],
  "audio": {
    "input": {
      "transcription": {
        "model": "gpt-4o-mini-transcribe",
        "language": "en"
      },
      "turn_detection": {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": false,
        "interrupt_response": true
      }
    },
    "output": {
      "voice": "marin",
      "speed": 1.25
    }
  },
  "tools": ["four expanded function definitions"],
  "tool_choice": "auto",
  "instructions": "bounded operator policy"
}
```

`output_modalities` stays `audio`; output audio transcript events provide the
visible operator text. The default voice is `marin`, chosen from the currently
documented high-quality voices, and may be changed through validated config
only before the first audio response. Output speed defaults to `1.25`, is
validated against the documented `0.25`–`1.5` range, and changes only between
responses. Input transcription is a display aid.
`gpt-4o-mini-transcribe` is a documented conversation-session transcription
option and is the lower-cost first default; semantic VAD remains the
conversation chunker. Deltas are opportunistic, completed items are
authoritative for captions, and completion order is reconciled by `item_id`
rather than arrival order. The separate transcription-session path and its
`gpt-realtime-whisper` profile are evaluated later because they have different
turn-detection semantics.

### 5.4 Required server events

The event parser handles these families and ignores unknown additive fields:

- `session.created`, `session.updated`;
- `input_audio_buffer.speech_started`, `speech_stopped`, and `committed`;
- `conversation.item.input_audio_transcription.delta` and `.completed`;
- `conversation.item.added` and `.done` as the primary item lifecycle, plus
  `conversation.item.created` as an explicit compatibility alias while the
  current API reference still exposes it;
- `response.created`, `response.output_audio_transcript.delta` and `.done`;
- `response.function_call_arguments.done` for validation and observability,
  while execution waits for the canonical function item in `response.done`;
- `response.done`, including `function_call` output items and the exact terminal
  statuses `completed`, `cancelled`, `failed`, and `incomplete`;
- `rate_limits.updated` for status only;
- `error` in its `{type, error, event_id}` wrapper, correlated by client
  `event_id` where possible.

Unknown event types are counted and made available in verbose diagnostics, but
they do not fail a healthy session. Known event types with malformed required
fields fail that event closed. The parser never accepts a tool name outside the
four configured functions even if OpenAI emits it.

### 5.5 Function output

A completed `response.done` may contain one or more `function_call` items. Each
item supplies `name`, `call_id`, and JSON-string `arguments`. The sideband
validates argument byte size, JSON object shape, allowed keys, string lengths,
repository and thread scope, then claims `call_id` before execution.

The result returns as:

```json
{
  "type": "conversation.item.create",
  "event_id": "evt_local_...",
  "item": {
    "type": "function_call_output",
    "call_id": "call_...",
    "output": "{...one canonical JSON result...}"
  }
}
```

After the output item is accepted or durably sent under the at-most-once policy,
the response arbiter may create one follow-up response. A duplicate function
call never invokes the host twice.

### 5.6 Push-to-talk fallback

Push-to-talk is a session mode, not a browser-only mute convention. It may be
entered only at an idle boundary: the sideband sends `session.update` with
`audio.input.turn_detection: null` and waits for the matching `session.updated`
before enabling the control. On press, the browser enables its microphone track
and calls the bounded local control; the sideband cancels a known active response
when necessary and sends `input_audio_buffer.clear`. On release, the browser
disables the track and the sideband sends `input_audio_buffer.commit`; the
resulting committed item flows through the response arbiter, which remains the
only writer of `response.create`. Returning to hands-free mode restores the
complete semantic-VAD profile at an idle boundary and waits for confirmation.

### 5.7 Interruption and lifetime

With WebRTC and VAD, OpenAI manages output buffering and truncates unheard audio
on interruption. The browser does not manually splice PCM or send
`conversation.item.truncate`. An explicit Stop speaking control asks the
sideband to send `response.cancel`, including the known active `response_id`
when available; terminal confirmation is `response.done` with status
`cancelled`, not a separate `response.cancelled` event. The arbiter then stops
considering that response active.

Realtime sessions currently have a 60-minute maximum. The implementation uses
a configurable soft rollover threshold defaulting to 55 minutes and a hard
deadline below 60 minutes. All timing is monotonic and injectable in tests.

Official source links, checked 2026-07-20:

- <https://developers.openai.com/api/docs/models/gpt-realtime-2.1>
- <https://developers.openai.com/api/docs/guides/realtime-webrtc>
- <https://developers.openai.com/api/docs/guides/realtime-server-controls>
- <https://developers.openai.com/api/docs/guides/realtime-vad>
- <https://developers.openai.com/api/docs/guides/realtime-conversations>
- <https://developers.openai.com/api/docs/guides/realtime-transcription>
- <https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/hangup>
- <https://developers.openai.com/api/reference/resources/realtime>

## 6. Internal contracts

### 6.1 Identifier wrappers

Use frozen dataclasses with validated non-empty string values for
`RealtimeSessionId`, `RealtimeItemId`, `RealtimeResponseId`,
`RealtimeFunctionCallId`, `CodexJobId`, `CodexRequestId`, `HostThreadId`,
`HostTurnId`, and `WorkerDeliveryId`. Serialization emits strings, but type
checkers and constructors prevent accidental cross-use.

IDs generated locally use an injected factory and a stable prefix. Remote IDs
are validated for length and safe characters but never treated as secrets
unless the contract says so. The OpenAI call ID is held by a separate private
type with no `repr` value and no public serializer.

### 6.2 Job state

```text
accepted -> starting -> running -> waiting_approval -> running
                    |         |                    |
                    |         +--------------------+
                    v
                  uncertain
                    |  \
                    |   -> completed | failed | interrupted
                    +------> running
```

Terminal states never transition. `waiting_approval` is non-terminal and does
not grant approval. `uncertain` is a recoverable, non-terminal safety state that
continues to block a new delegation until host evidence moves it to running or a
known terminal state. A job snapshot contains bounded public fields and a
private terminal completion reference.

### 6.3 Response arbiter state

The pure arbiter projection contains:

- active response and its cause;
- whether user speech is active;
- bounded claimed user item IDs;
- bounded claimed function call IDs and their result disposition;
- FIFO worker deliveries with priority below user items;
- bounded claimed and delivered worker IDs;
- rollover requested/active state;
- last typed error and recovery action.

Events return a new projection plus named actions such as
`CREATE_RESPONSE`, `CANCEL_RESPONSE`, `SEND_FUNCTION_OUTPUT`,
`CREATE_WORKER_RESPONSE`, `START_ROLLOVER`, and `PUBLISH_STATUS`. The reducer
performs no I/O, time lookup, ID generation, logging, or JSON parsing.

### 6.4 Durability decisions

Existing `TurnJournal` gains optional neutral fields for job, realtime item,
response, function call, worker delivery, and mode. Adding optional fields is a
backward-compatible schema extension: old records parse with `None`, unknown
fields remain rejected, and no old required field changes.

Before an irreversible or duplicate-sensitive effect, the runtime appends a
claim record. After a confirmed effect, it appends the outcome. A crash between
claim and outcome reconstructs `uncertain`; it does not repeat the effect.

Journal append serialization covers the sequence decision and durable write,
not only the final `O_APPEND`. A process lock is held while the last sequence is
re-read, the next record is validated, and one line is written and synced, so
two writers cannot create duplicate sequence numbers. File opens reject
symlinks and non-regular or wrongly owned targets before any append or retention
operation.

The replay projection uses event names scoped with `realtime.` and validates
legal transitions. A corrupt historical record before the tail blocks automatic
recovery and points to the exact journal; one torn final line follows existing
recovery behavior.

### 6.5 Local event stream

The authenticated local event route uses newline-delimited JSON over a streaming
`fetch`, not `EventSource`, because browser `EventSource` cannot set the
authorization header. Every record has `schema_version`, monotonically
increasing local `sequence`, `type`, and a bounded `payload`.

The stream contains authoritative final user/operator transcript rows plus
public state, job state, approval-required state, and recoverable errors. The
browser data channel may supply provisional captions only; it never supplies a
second authoritative final row. Persistence policy controls journal content;
the live authenticated page may display transcripts even when they are not
persisted.

## 7. Dependency graph

```text
RTOP-001 -> RTOP-010, RTOP-013, RTOP-040
RTOP-010 -> RTOP-011, RTOP-030, RTOP-040, RTOP-052A
RTOP-011 -> RTOP-012, RTOP-020
RTOP-013 + RTOP-020 -> RTOP-021
RTOP-030 -> RTOP-031, RTOP-041
RTOP-012 + RTOP-021 + RTOP-031 -> RTOP-032
RTOP-040 -> RTOP-041, RTOP-052A, RTOP-050
RTOP-012 + RTOP-021 + RTOP-032 + RTOP-041 + RTOP-052A -> RTOP-050
RTOP-050 -> RTOP-051, RTOP-060
RTOP-051 + RTOP-052A -> RTOP-060
RTOP-052A + cleared audio WIP -> RTOP-052B
RTOP-051 + RTOP-060 -> RTOP-070
RTOP-060 -> RTOP-061
RTOP-052B + RTOP-060 + RTOP-061 + RTOP-070 -> RTOP-071
```

The first useful vertical slice is fixtures → types/journal → arbiter/jobs →
OpenAI protocol/transport → loopback server/UI → composition/CLI. Audio lease
enforcement for the realtime side and a live-classic-broker guard are part of
that slice. Classic hands-free adoption lands only after the current dirty audio
work is committed; it blocks the release gate without blocking the vertical
slice.

## 8. Work package A: foundations

### RTOP-001 — Capture baseline and external contract fixtures

**Purpose:** Freeze current classic behavior and the provider wire assumptions
before new orchestration exists. Characterization tests keep the additive lane
from silently changing commands that users already rely on.

**Files:**

- `tests/fixtures/realtime/` (new)
- `tests/fixtures/realtime/README.md` (new)
- `tests/test_broker_realtime_contract_fixtures.py` (new)
- existing classic tests are executed but not edited

**Implementation:**

1. Add synthetic, scrubbed fixtures for successful multipart SDP metadata,
   missing/invalid `Location`, `session.created`, `session.updated`, semantic
   VAD speech events, committed item, primary item added/done plus the created
   compatibility alias, transcript delta/completion, response lifecycle, one
   completed function call, function output acknowledgement, cancellation,
   call-hangup outcomes, rate limits, structured error correlation, and an
   unknown additive event.
2. Add app-server host fixtures for turn started, approval required, completed,
   failed, interrupted, transport lost, and recovery history. Reuse stable
   shapes from current app-server tests without importing another test module.
3. Document which fields are official API contract, which are synthetic local
   extensions, and the official URL/date checked.
4. Add a scrubber test rejecting API-key patterns, raw `rtc_` call IDs, URL
   fragments, absolute user-home paths, real transcript content, unstable wall
   times, and unbounded payloads.
5. Run the classic broker CLI/socket/compatibility baseline twice and record the
   exact focused command in the fixture README.

**Tests:** Fixture decode/encode stability, scrubber rejection, deterministic
sorting, and two-run byte equality.

**Acceptance:** No production code changes; fixtures contain only synthetic
content; `test_broker_cli.py`, `test_broker_socket.py`, and
`test_broker_compatibility.py` retain their baseline result.

**Depends on:** none.

**Unblocks:** RTOP-010, RTOP-013, and RTOP-040 directly; every later task uses
the baseline fixtures transitively.

### RTOP-010 — Add provider-neutral realtime types and limits

**Purpose:** Give the operator plane a closed vocabulary before network or host
code can accidentally couple remote dictionaries to runtime state.

**Files:**

- `voice_mode/broker/realtime/__init__.py` (new)
- `voice_mode/broker/realtime/types.py` (new)
- `voice_mode/broker/realtime/security.py` (new, validation constants only)
- `tests/test_broker_realtime_types.py` (new)

**Implementation:**

1. Add validated identifier wrappers described in section 6.1. Keep the private
   call identifier non-renderable and absent from dataclass `repr` output.
2. Add enums for transport, session, speech, response, job, delivery, and
   rollover states. Use `class X(str, Enum)` for Python 3.10.
3. Add frozen result types for tool calls, job snapshots, public status,
   transcript events, errors, and arbiter actions.
4. Set named limits for SDP bytes, sideband event bytes/depth, tool argument
   bytes, task/instruction/summary text, thread/repository strings, local control
   payload, local event backlog, remembered IDs, output speed, and shutdown
   grace.
5. Add canonical JSON serialization with stable keys, compact separators, and
   no implicit serialization of private values.
6. Validate repository roots through the existing canonical helper and retain
   an allowlisted root object rather than accepting arbitrary path strings in
   tool handlers.
7. Add a redaction helper that removes bearer tokens, key-like values, URL
   fragments, raw SDP, and private call IDs from bounded public errors.

**Tests:** Every wrapper rejects empty, overlong, and unsafe values; IDs cannot
be mixed by constructors; public serialization never includes private values;
redaction uses table-driven secret variants; limits hold on Unicode byte length,
not only Python character count.

**Acceptance:** Types have no aiohttp, OpenAI, browser, or concrete host import;
all public JSON is deterministic and secret-free.

**Depends on:** RTOP-001.

**Unblocks:** RTOP-011, RTOP-030, RTOP-040, and RTOP-052A.

### RTOP-011 — Extend the journal and build realtime replay

**Purpose:** Make duplicate prevention and recovery durable with the existing
privacy model instead of adding a second weaker log format.

**Files:**

- `voice_mode/broker/journal.py`
- `voice_mode/broker/realtime/journal.py` (new)
- `tests/test_broker_journal.py`
- `tests/test_broker_realtime_journal.py` (new)

**Implementation:**

1. Add optional `mode`, `job_id`, `realtime_item_id`, `response_id`,
   `function_call_id`, and `worker_delivery_id` fields to `JournalEvent` and
   `JournalRecord`. Do not add the OpenAI call ID, capability, raw SDP, or key.
2. Preserve schema version 1 only if old records without new fields and new
   records with the optional fields round-trip in both directions. If any
   required or semantic interpretation changes, introduce a versioned parser
   that reads both versions instead of weakening validation.
3. Add namespaced realtime event names for session, user-item claim, response
   claim/outcome, function-call claim/result-send, job transition, worker
   delivery claim/outcome, reconnect, rollover, and shutdown.
4. Build a pure replay projection that reconstructs claimed IDs, active or
   uncertain response, current job, terminal completion availability, worker
   delivery disposition, and last transport state.
5. Reject illegal transitions such as a delivered worker without a claim, a
   terminal job returning to running without explicit recovery evidence, or two
   response claims for one user item.
6. Bound remembered IDs during live operation while retaining durable history.
   Compaction is a derived checkpoint plus original append-only records; it does
   not rewrite or discard evidence during the initial slice.
7. Preserve transcript opt-in behavior. Realtime events never populate the
   transcript field unless the existing journal transcript authorization is
   enabled.
8. Harden shared append semantics: acquire an advisory process lock, re-read and
   validate the durable tail while holding it, allocate the next sequence, then
   append and sync before unlock. Reject symlink, non-regular, wrong-owner, and
   over-permissive directory/file targets without replacing them. Retention may
   remove only a regular file it can lock and revalidate.

**Tests:** Old fixture compatibility, new round-trip, secret absence, torn-tail
recovery, illegal transition rejection, competing-process sequence allocation,
symlink/ownership/mode refusal, retention-versus-writer contention, replay
determinism, retention behavior, and a crash between every claim/outcome pair.

**Acceptance:** A fresh manager can decide which effects are safe, complete, or
uncertain using only journal records; classic journal tests remain green.

**Depends on:** RTOP-010.

**Unblocks:** RTOP-012 and RTOP-020.

### RTOP-012 — Implement the single-response arbiter

**Purpose:** Put “one response” in a pure deterministic reducer rather than
relying on Realtime defaults, browser timing, or prompt obedience.

**Files:**

- `voice_mode/broker/realtime/arbiter.py` (new)
- `tests/test_broker_realtime_arbiter.py` (new)

**Implementation:**

1. Define semantic events for user speech start/stop, committed user item,
   response created/finished/cancelled/failed, function result ready/sent,
   worker event queued/claimed/sent/uncertain, disconnect, reconnect, rollover,
   and shutdown.
2. Implement a pure reducer returning projection plus ordered actions. It cannot
   send network events, write journals, create IDs, or inspect clocks.
3. Claim a committed item once and emit `CREATE_RESPONSE` only when no response
   is active and no higher-priority user speech is active.
4. Let Realtime interruption close the active response without creating a
   second cancellation loop. A duplicate terminal event becomes a no-op.
5. Prioritize user items over tool acknowledgements and worker completion.
   Preserve FIFO within equal priority and cap queues with a typed overflow that
   keeps already-claimed work safe.
6. Require a durable claim acknowledgement from the runtime before emitting an
   irreversible transport action. Model this as two events so tests can crash at
   the boundary.
7. Track function calls independently of the response that produced them. A
   function response may contain multiple outputs; process supported calls in
   stable output order while refusing an unsupported name.
8. Queue rollover only at idle. If the hard session deadline arrives, stop
   accepting new audio, close or mark any active response, and begin rollover
   without changing job state.
9. Expose a bounded public projection for the UI and status command.
10. Worker delivery emits a distinct `CREATE_WORKER_RESPONSE` action whose cause
    carries the delivery ID. It cannot be reduced into an ordinary conversational
    response or authorize a function-call action.

**Tests:** Exhaustive state/event table, generated duplicate events, user vs
worker priority, barge-in, multi-call output ordering, queue overflow,
disconnect at every durable boundary, rollover while idle/busy, shutdown, and
100-turn no-growth soak.

**Acceptance:** No legal or duplicate event sequence emits two response-create
actions for one cause or two worker responses for one delivery ID, and a worker
cause can never emit a tool-authorizing action.

**Depends on:** RTOP-011.

**Unblocks:** RTOP-032 and RTOP-050.

### RTOP-013 — Promote request recovery to the host contract

**Purpose:** Give every job-recovery caller one typed host boundary instead of
requiring app-server downcasts, optional-method checks, or knowledge of thread
history payloads.

**Files:**

- `voice_mode/broker/hosts/base.py`
- `voice_mode/broker/hosts/app_server.py`
- `tests/test_broker_host_contract.py`
- `tests/test_broker_app_server.py`

**Implementation:**

1. Add `recover_request(*, request_id, thread_id) -> HostRecoveryEvidence` to
   `HostAdapter` as a concrete compatibility method. Its default implementation
   converts `query_disposition` into bounded recovery evidence and never
   redispatches a request.
2. Keep `AppServerHostAdapter.recover_request` as the richer override that
   queries durable thread history. Move no app-server JSON parsing into the
   base interface.
3. Use the existing `HostRecoveryEvidence(disposition, rationale, completion)`
   contract unchanged. Callers branch on the typed disposition, never on adapter
   type or attribute presence.
4. Preserve existing adapter implementers and test fakes by avoiding a new
   abstract method. An adapter with only `query_disposition` remains valid but
   may yield less-complete evidence.
5. Bound and redact the evidence reason before it crosses into public job
   status or journal records.

**Tests:** A minimal adapter inherits the default safely; app-server retains
durable-history recovery; unavailable, absent, in-progress, completed,
cancelled, and uncertain evidence stay typed; no caller uses `hasattr`,
`getattr`, or an `AppServerHostAdapter` downcast.

**Acceptance:** RTOP-021 can call `adapter.recover_request(...)` through a
`HostAdapter` reference for every supported adapter, and recovery never depends
on a private concrete seam.

**Depends on:** RTOP-001.

**Unblocks:** RTOP-021.

## 9. Work package B: asynchronous Codex worker

### RTOP-020 — Implement the one-job asynchronous manager

**Purpose:** Separate host turn acceptance from host completion so the voice
session remains available while Codex works, without altering the blocking
hands-free runner.

**Files:**

- `voice_mode/broker/realtime/jobs.py` (new)
- `tests/fakes/__init__.py` (new)
- `tests/fakes/realtime.py` (new)
- `tests/test_broker_realtime_jobs.py` (new)

**Implementation:**

1. Construct `CodexJobManager` with one `HostAdapter`, one selected thread,
   one `TurnJournal`, an asyncio loop, ID/clock factories, repository allowlist,
   event sink, and bounded summary function.
2. Subscribe once to normalized `HostEvent` values. Host callbacks may arrive on
   the app-server reader thread; copy the immutable event and enter the manager
   loop with `loop.call_soon_threadsafe`. Never mutate asyncio primitives from
   the reader thread.
3. Implement `delegate` as an async operation that validates the task, checks
   the one-job constraint, durably claims the function call and job request,
   invokes synchronous `start_turn` through `asyncio.to_thread`, records the
   returned host turn ID, and returns `accepted` before completion.
4. Use the job ID for user-facing correlation and a separate stable request ID
   for the host adapter. Never use the Realtime `call_id` as the host request ID;
   its namespace and lifetime are provider-owned.
5. If a second job is active, return canonical `busy` with its public job ID and
   do not invoke the host. Terminal jobs remain queryable within bounded session
   history but do not block the next explicit delegation after their delivery
   disposition is safe.
6. Implement `get_job` as a pure snapshot lookup. Unknown and expired IDs return
   `not_found`; they never fall back to “latest.”
7. Implement `steer` only for the active known job and only when the adapter
   advertises `STEER_TURN`. Validate instruction bounds, claim before I/O, use a
   new host request correlation for the steer, and return a typed disposition.
8. Implement `interrupt` only for the active known job and only when the adapter
   advertises `INTERRUPT_TURN`. Claim before I/O and make repeats idempotent.
9. Normalize `TURN_STARTED`, `TURN_COMPLETED`, `TURN_CANCELLED`,
   `APPROVAL_REQUIRED`, and `TRANSPORT_LOST` into legal job transitions.
10. On successful completion, retain the immutable `CanonicalResponse`
    privately, derive a bounded plain-text summary without Markdown/code/table
    content, journal the terminal event, then publish one worker event.
11. On failure or empty completion, publish a bounded typed error without
    fabricating a result. On interruption, distinguish user-requested job
    interruption from unrelated Realtime speech interruption.
12. `close()` unsubscribes idempotently. It does not close the shared host
    adapter unless the composition root explicitly transfers ownership.

**Tests:** Delegation returns before a blocking fake completes; start happens on
a worker thread; event handoff is loop-safe; one active job; duplicate call ID;
busy result; status lookup; steering; interruption; approval; completion;
failure; empty response; transport loss; event after close; bounded summary;
host capability refusal; no TTS/voice call.

**Acceptance:** A long fake Codex turn can remain running while ten independent
operator events are processed; exactly one host `start_turn` and one worker
terminal event occur.

**Depends on:** RTOP-011.

**Unblocks:** RTOP-021.

### RTOP-021 — Add host recovery, approval visibility, and job retention

**Purpose:** Preserve existing evidence-based dispatch semantics when the
app-server connection fails, and expose approval blocking without creating a
voice authorization path.

**Files:**

- `voice_mode/broker/realtime/jobs.py`
- `voice_mode/broker/realtime/journal.py`
- `tests/test_broker_realtime_job_recovery.py` (new)

No edit to `voice_mode/broker/recovery.py` or `tests/test_broker_recovery.py` is
part of this task; the existing recovery suite is run as regression evidence.

**Implementation:**

1. On transport loss, move an accepted/starting/running job to `uncertain`, keep
   its IDs, and publish one recoverable status. Do not start a new turn.
2. Reconnect through an injected app-server adapter factory and call the typed
   `HostAdapter.recover_request` contract introduced by RTOP-013. Do not probe
   optional methods or downcast to app-server internals in the manager.
3. Map evidence: absent before confirmed dispatch may become a typed failed
   start; in-progress returns to running; completed restores one canonical
   result; cancelled becomes interrupted; ambiguous remains uncertain. The
   uncertain job retains the one-job slot until evidence resolves it.
4. Require repository and thread identity to match the original job before
   accepting recovery evidence. A response from another thread is a hard
   mismatch, not success.
5. Deduplicate a completion observed once live and once through recovery using
   request and host turn IDs before worker publication.
6. Model approval as `waiting_approval` with approval ID, reason, thread ID, and
   exact Codex focus/resume hint. Do not store an arbitrary command received in
   the approval text.
7. Steering while waiting approval may be refused as `not_steerable` unless the
   host explicitly confirms it is supported. Interruption remains available.
8. A Realtime prompt saying “approve” cannot call a hidden approval function
   because no approval tool exists. The local UI links or copies the Codex
   identifier and explains that approval happens in Codex.
9. Retain a bounded number of terminal job snapshots in memory for status and
   follow-up. Eviction never deletes journal evidence or makes a delivery
   eligible to repeat.
10. Cover process restart by constructing a new manager from replay before
    attaching a host. It must recover or surface uncertainty before accepting a
    new delegation.

**Tests:** Failure before dispatch, failure after dispatch, every host
disposition, thread mismatch, duplicate live/recovered completion, approval
dedupe, interrupt while waiting, restart replay, bounded retention, and failed
adapter replacement cleanup.

**Acceptance:** No recovery path redispatches an uncertain host request;
approvals are visible and actionable in Codex but impossible to grant through
the four Realtime tools.

**Depends on:** RTOP-013 and RTOP-020.

**Unblocks:** RTOP-032 and RTOP-050.

## 10. Work package C: OpenAI protocol and session transport

### RTOP-030 — Build pure OpenAI event validation and client-event builders

**Purpose:** Keep a changing external schema at one strict boundary so
orchestration code consumes stable semantic events and tests can pin current
official behavior.

**Files:**

- `voice_mode/broker/realtime/protocol.py` (new)
- `tests/test_broker_realtime_protocol.py` (new)
- `tests/fixtures/realtime/openai-*.json` (from RTOP-001)

**Implementation:**

1. Define an `OpenAIRealtimeCodec` with `decode_server_event(bytes|str)` and
   explicit builders for call-session JSON, `session.update`, function output,
   isolated worker response, response create/cancel, input-buffer clear/commit,
   and a bounded close reason.
2. Parse JSON with a pre-parse byte limit, then validate object, event type,
   depth, required field types, ID bounds, text bounds, and list cardinality.
   Unknown extra fields are ignored; wrong required fields create a typed
   protocol error with no raw payload echo.
3. Normalize session ready/update, speech start/stop, audio commit, input
   transcription delta/completion, primary item added/done, the explicit item
   created compatibility alias, response created, output transcript
   delta/completion, function-argument completion, response terminal, function
   calls, rate limits, and the structured error wrapper.
4. Use `input_audio_buffer.committed.item_id` as the response cause. Do not wait
   for transcription completion before responding, since transcription is a
   separate display process and may complete out of order.
5. Parse complete function calls from `response.done.response.output`. Support
   multiple output items in stable order and ignore partial argument deltas for
   execution. Dispatch calls only when the response and function item are
   completed; cancelled, failed, incomplete, or partial calls never cause a
   host effect. Reject duplicate `call_id` entries within one response.
6. Distinguish the documented completed, cancelled, failed, and incomplete
   response statuses. Preserve response ID and bounded status detail, reject an
   unknown terminal status as a known-event schema error, and never wait for a
   nonexistent `response.cancelled` event.
7. Build exactly four JSON Schema function definitions. Set
   `additionalProperties: false`, required fields explicitly, string bounds in
   runtime validation, and descriptions that distinguish speech interruption
   from job interruption.
8. Build worker integration as a response-local input message inside
   `response.create`, with `conversation: "none"`, `tool_choice: "none"`, and no
   response-local tools. Delimit the bounded machine-produced worker data and
   encode a canonical JSON payload. Neither that input nor its output enters the
   default conversation, and it is never interpolated into session instructions.
9. Build client events with injected unique event IDs. Return an association
   from event ID to semantic mutation for error correlation. A response cancel
   includes the active response ID when known, and push-to-talk buffer events
   remain distinct from the arbiter-owned response create.
10. Keep model, voice, output speed, transcription model, language, VAD
    eagerness, and instructions in a validated configuration object; builders do
    not read environment variables.

**Tests:** Official fixtures, primary and compatibility item lifecycle events,
unknown fields/events, malformed error wrappers, deep JSON, oversize
bytes/text/lists, multi-function output, duplicate call IDs, all documented
response statuses, rejection of unknown terminal status, canonical builder
snapshots including push-to-talk and isolated worker response, refusal to execute
calls from non-completed responses/items, schema closedness, worker-data
escaping, event-error correlation, and secret-safe exceptions.

**Acceptance:** All supported external dictionaries become typed semantic
events before reaching session or job code; no unsupported tool name can be
executed.

**Depends on:** RTOP-010.

**Unblocks:** RTOP-031 and RTOP-041.

### RTOP-031 — Implement multipart SDP and sideband WebSocket transport

**Purpose:** Provide one injectable OpenAI I/O adapter with deterministic
timeouts, cleanup, limits, and redaction while keeping policy in higher layers.

**Files:**

- `voice_mode/broker/realtime/transport.py` (new)
- `tests/test_broker_realtime_transport.py` (new)

**Implementation:**

1. Construct the transport with API key, base HTTPS/WSS URLs, timeouts, maximum
   sizes, injected `aiohttp.ClientSession` factory, codec, and event sink. The
   default endpoints are fixed OpenAI HTTPS/WSS values; test overrides must be
   explicit constructor arguments. The production client ignores environment
   proxy/netrc settings (`trust_env=False`) so the bearer cannot be redirected
   through ambient process configuration.
2. `create_call(offer_sdp, session_config)` validates local offer size and
   format, builds `aiohttp.FormData` with `sdp` and canonical JSON `session`, and
   POSTs `/v1/realtime/calls` with bearer authorization.
3. Bound connect, response headers, and body reads. Reject redirects so the
   bearer credential cannot move to another origin. Require TLS for default
   endpoints.
4. On non-success, expose status, request correlation when present, and a
   redacted bounded provider message. Never include response headers wholesale,
   key, request form, SDP, or call ID.
5. Parse `Location` as an expected API path, not an arbitrary URL. Validate one
   final call-ID segment and store it in the private wrapper. Missing, multiple,
   foreign-host, query-bearing, or malformed values fail closed.
6. Return the SDP answer and private call identity to the session manager. The
   local web layer receives only the answer.
7. `connect_sideband` URL-encodes the call ID, sends bearer authorization,
   configures heartbeat and message size, and starts one reader task. It does
   not log the URL because it contains the private call identity.
8. The reader decodes text messages only, passes them through the codec, and
   publishes semantic events. Binary, fragmented-over-limit, malformed, or
   closed messages become typed transport events.
9. `send` serializes a prebuilt client event, verifies the bound, holds one
   async send lock, and classifies success, known provider error later, or
   uncertain disconnect. It does not retry mutating events automatically.
10. Once a duplicate-sensitive send begins, cancellation cannot escape between
    its journal claim and classified outcome. Shield the bounded wire operation,
    release the send lock in `finally`, then record confirmed or uncertain
    before re-raising cancellation. Shutdown uses the same path rather than
    abandoning an in-flight send task.
11. Add a bounded authenticated `hangup_call` operation using
    `POST /v1/realtime/calls/{call_id}/hangup`. It is the server-side fail-safe
    for WebRTC teardown when browser acknowledgement is missing; its URL and
    private call ID are never logged, and repeated not-found/already-ended
    outcomes are safe completion.
12. `close` cancels and awaits the reader with Python-3.10-compatible task
    handling, closes WebSocket and owned HTTP session idempotently, and applies
    a bounded timeout.

**Tests:** Multipart names/content, authorization presence at fake boundary,
redirect refusal, TLS/default endpoint validation, every Location variant,
answer bound, status/error redaction, WebSocket URL encoding, text/binary/large
messages, serialized sends, cancellation at every send await, disconnect during
send, authenticated hangup and idempotent terminal outcomes, reader
cancellation, close idempotency, ignored ambient proxy configuration, and no
leaked aiohttp sessions.

**Acceptance:** A scripted transport proves call creation and sideband event
flow without live network, and secret scanning of logs/errors/status finds no
credential, call ID, or SDP.

**Depends on:** RTOP-030.

**Unblocks:** RTOP-032.

### RTOP-032 — Manage session readiness, reconnect, and 60-minute rollover

**Purpose:** Turn low-level transport events into a stable operator session that
survives transient control loss and replaces expiring calls without touching the
Codex job lifecycle.

**Files:**

- `voice_mode/broker/realtime/session.py` (new)
- `tests/test_broker_realtime_session.py` (new)

**Implementation:**

1. Construct `RealtimeSessionManager` with transport factory, codec, arbiter
   runtime, job manager, journal, clock, ID factory, status sink, and local UI
   event sink. It owns one active call generation and at most one replacement.
2. Treat call creation, sideband policy readiness, and browser SDP application
   as separate gates. Withhold the SDP answer from the browser until the
   sideband is open and the expected profile is confirmed; publish `connected`
   only after the browser then applies the answer and confirms readiness.
3. Send or confirm the full session configuration before accepting committed
   user items. A mismatched model, tool set, VAD mode, or response-creation flag
   is a startup error rather than a degraded conversation.
4. Route semantic events: speech state and transcript events to UI; committed
   items and response lifecycle to arbiter; function calls to the bounded tool
   dispatcher; provider errors to the mutation correlation table; rate limits
   to status; unknown additive events to counters.
5. Execute tool handlers through one dispatcher that maps exact names to job
   methods. Return every result as canonical JSON function output, including
   validation, busy, not-found, unsupported, and internal typed errors. Reject
   every tool call whose correlated response cause is a worker delivery, even
   though worker responses are also constructed with tools disabled.
6. Apply arbiter actions sequentially. Before duplicate-sensitive sends, append
   the required claim; after successful local send, record sent. If disconnect
   or cancellation makes the send ambiguous, finish the shielded classification,
   record uncertainty, and do not retry automatically.
7. Publish transcript deltas by item/response ID and final text separately. Do
   not journal content unless authorized.
8. On sideband loss while WebRTC remains connected, show reconnecting, stop
   response creation, and send an authenticated `suspend_audio` event. The page
   must set the active track `enabled = false` and acknowledge with its
   generation before any reattach attempt. If acknowledgement misses the short
   deadline, call the server-side hangup endpoint, retire the generation, and
   keep the lease until hangup reaches a safe terminal disposition. Keep the
   Codex job alive. After confirmed suspension, attempt a bounded best-effort
   reattach to the same private call and rehydrate arbiter claims before
   consuming new events; the official contract documents attachment by call ID
   but does not promise reconnect semantics, so correctness cannot depend on
   reattach succeeding.
9. If same-call reattach is rejected or exhausted, hang up the old call and
   initiate a fresh-call rollover while the browser track remains disabled.
   Never let the browser continue sending audio to an ungoverned session without
   sideband policy.
10. At the soft deadline, signal the browser to prepare a replacement peer at
    the next idle boundary. Create the new call, attach/configure its sideband,
    install a bounded summary and active job references, then atomically switch
    generations and close the old call.
11. At the hard deadline, prioritize privacy and policy: require browser track
    suspension, invoke server-side hangup, and close the old call even if
    seamless replacement failed. Surface one recovery action; keep the Codex
    job and do not release the lease before browser suspension or a safe hangup
    disposition.
12. Deduplicate late events by `(generation, event_id)` and reject state changes
    from an old generation after cutover.
13. Shutdown stops accepting local controls, requests and waits boundedly for
    browser track suspension, uses server-side hangup when acknowledgement is
    absent, journals the classified outcome, closes current/replacement
    transports, and leaves job cleanup to the composition root's explicit
    policy.

**Tests:** All readiness orders including answer withholding, config mismatch,
event routing, worker-cause tool refusal, isolated worker response, canonical
tool errors, claim/send crashes and cancellation, supported same-call reattach,
sideband loss with suspension acknowledgement or hangup fallback,
rejected/failed reattach with a disabled track, stale generation events,
semantic-VAD/push-to-talk transitions, buffer clear/commit, soft idle rollover,
busy rollover, hard deadline, replacement failure, job survival, bounded
summary, and idempotent shutdown.

**Acceptance:** A 61-minute fake-clock session rolls calls without duplicating a
user response, tool effect, job, or worker integration; losing sideband never
leaves browser audio governed only by client JavaScript.

**Depends on:** RTOP-012, RTOP-021, RTOP-031.

**Unblocks:** RTOP-050.

## 11. Work package D: loopback client and audio ownership

### RTOP-040 — Build the authenticated loopback server

**Purpose:** Expose the minimum browser bootstrap and control surface while
defending against unrelated local pages, DNS rebinding assumptions, oversized
payloads, and accidental repository serving.

**Files:**

- `voice_mode/broker/realtime/security.py`
- `voice_mode/broker/realtime/web.py` (new)
- `tests/test_broker_realtime_server.py` (new)

**Implementation:**

1. Build an `aiohttp.web.Application` through an injected factory and bind only
   the literal IPv4 loopback address `127.0.0.1`. Port defaults to `0`; reject a
   configured host, wildcard, hostname, Unix proxy, or prebound non-loopback
   socket in the first slice.
2. Generate a 256-bit capability with `secrets.token_urlsafe`, place it only in
   the browser URL fragment, and compare bearer headers with
   `secrets.compare_digest`. The root HTML is public static content and cannot
   contain session state or the capability.
3. Validate `Host` against the exact chosen `127.0.0.1:<port>` authority and
   validate `Origin` against `http://127.0.0.1:<port>` for every authenticated
   mutation. Reject missing Origin on browser mutation routes; allow an explicit
   internal test/client identity only through injected policy. Reject
   `Forwarded` and every `X-Forwarded-*` header because this server has no proxy
   deployment mode.
4. Send a strict Content Security Policy allowing only same-origin script/style,
   no objects, no frames, no base override, and only the required OpenAI WebRTC
   connection behavior. Add `Referrer-Policy: no-referrer`,
   `X-Content-Type-Options: nosniff`, frame denial, and no-store caching.
5. Routes are closed and explicit:
   - `GET /` returns `index.html`;
   - `GET /assets/app.js` and `/assets/styles.css` return fixed resources;
   - `POST /v1/session` accepts bounded `application/sdp`, starts a call, and
     returns its answer only after sideband policy readiness;
   - `POST /v1/session/{generation}/ready` confirms applied SDP;
   - `POST /v1/control` accepts one bounded named control;
   - `GET /v1/events` returns authenticated NDJSON streaming fetch;
   - `GET /health` returns a tiny unauthenticated liveness response with no IDs.
6. Do not add a catch-all static path, directory traversal resolution, file
   upload, arbitrary JSON-RPC method, CORS middleware, debug page, traceback, or
   repository content route.
7. Limit concurrent page clients to one controller. A replacement page requires
   explicit takeover after the prior stream is dead; it cannot silently attach
   to an active microphone session.
8. Apply per-route content lengths before reading bodies. Parse JSON with a
   closed action schema. Return stable public error codes and one bounded
   redacted message.
9. Stream local events from a bounded per-client queue. Slow clients receive a
   `resync_required` marker and a fresh public snapshot rather than unbounded
   transcript or event memory. Send content-free keepalives and require an
   authenticated generation-scoped browser heartbeat through the closed control
   schema. If the controller stream and heartbeat both expire, ask the session
   manager to hang up the call and release the lease only after safe teardown.
10. Keep access logging disabled or structured through a redacting logger that
    never records fragment, authorization, SDP, transcript, or control body.
11. Shutdown first denies new session/control requests, asks the session manager
    to suspend the browser track and hang up the call, drains the event stream,
    and then cleans up the aiohttp runner/site under a timeout.

**Tests:** Real `aiohttp.test_utils` loopback requests for correct and wrong Host,
Origin, forwarding headers, bearer, content type, methods, paths, sizes, slow
stream, disconnect, heartbeat expiry/recovery, takeover, CSP/security headers,
no-store, traversal attempts, route inventory, health privacy, and shutdown.
Test app construction rejects non-loopback binds.

**Acceptance:** The browser can create and control one authenticated local
session; a page from another origin cannot start audio, post SDP, read the event
stream, or invoke a job control even though both run on the same machine.

**Depends on:** RTOP-010 and RTOP-001.

**Unblocks:** RTOP-041, RTOP-052A, and RTOP-050.

### RTOP-041 — Build the reference WebRTC transcript and status UI

**Purpose:** Make microphone, submission, operator speech, Codex job location,
and failure state visible while using the browser's native WebRTC media stack.

**Files:**

- `voice_mode/templates/realtime/index.html` (new)
- `voice_mode/templates/realtime/app.js` (new)
- `voice_mode/templates/realtime/styles.css` (new)
- `tests/test_broker_realtime_assets.py` (new)

No additional JavaScript test file or Node dependency is added in this slice;
browser behavior is covered by static contract checks here and the exact manual
runner owned by RTOP-061.

**Implementation:**

1. Build semantic HTML with one state strip, Cloud audio disclosure, Start/Stop,
   mic mute, push-to-talk fallback, stop speaking, job status, steer, cancel job,
   copy transcript, session timer, and chronological transcript/event region.
2. Keep Start disabled until capability bootstrap succeeds. Parse the URL
   fragment, retain the capability only in module memory, and immediately call
   `history.replaceState` to remove it from the address bar/history.
3. Start requests `getUserMedia({audio: ...})` only after the click. Prefer
   browser echo cancellation, noise suppression, and automatic gain control but
   show the actual track/settings and handle unsupported constraints.
4. Create one `RTCPeerConnection`, one remote autoplay audio element, and one
   `oai-events` data channel before the offer. Add exactly one microphone track.
5. POST raw offer SDP to the local route with bearer and Origin supplied by the
   browser, verify successful `application/sdp`, apply the answer, then confirm
   readiness to the broker.
6. Treat the Realtime data channel as receive-only application policy. Code may
   use provider transcript deltas for immediate provisional captions and media
   lifecycle for local rendering, but it cannot accept provider data-channel
   finals as authoritative or send `session.update`, `response.create`,
   function output, arbitrary conversation items, or tool control.
7. Open the authenticated local NDJSON stream through `fetch` with the bearer
   header. Parse incrementally across arbitrary chunk boundaries and enforce a
   browser-side line bound. Send a generation-scoped heartbeat while cloud audio
   is engaged and maintain a watchdog for content-free broker keepalives.
8. Reconcile provider provisional captions with sideband-normalized local finals
   by stable item/response IDs. A local final replaces its provisional row and
   is appended exactly once; job events use job/delivery IDs and never append
   the same Codex result as both transcript and worker result.
9. Render explicit states: ready, requesting mic, connecting, listening, user
   speaking, submitting, operator thinking, operator speaking, interrupted,
   reconnecting, rolling over, stopped, and error. No repeated chimes are added;
   visible state is the primary feedback in this slice.
10. Stop speaking calls the bounded local control, which lets the sideband send
    `response.cancel` for the known active response when available and waits for
    `response.done` with `cancelled`; muting only disables the local track.
    Cancel job is a separate labeled control with confirmation while a job is
    active.
11. Enter push-to-talk only after a local mode-change control has switched
    `turn_detection` to null and observed the matching `session.updated`. Press
    enables the track and requests response cancel/input-buffer clear; release
    disables the track and requests input-buffer commit. The committed item goes
    to the arbiter, so browser JavaScript still cannot send `response.create`.
    Switching back restores and confirms the full semantic-VAD profile at idle.
12. Steer job accepts bounded text and submits to local control. Approval state
    provides Copy thread ID and Copy resume command, never an Approve button.
13. On `pagehide`, track ended, permission revocation, or connection failure,
   immediately stop local tracks and best-effort notify the broker. Cleanup is
   idempotent and works when the network notification fails. On
   `suspend_audio`, disable the active track before acknowledging the generation;
   re-enable only after an authenticated `resume_audio` event. If broker
   keepalives expire, disable the track and close the peer locally without
   waiting for `pagehide` or a successful HTTP request.
14. Rollover creates a second peer only after a broker event requests it. Once
    the replacement is ready, atomically swap the active generation and stop old
    tracks/peer. Do not render transcript events from the retired generation.
15. Accessibility: keyboard-operable controls, visible focus, live regions that
    do not reread every partial token, state text independent of color, reduced
    motion support, and no audio cue required to understand submission.
16. Keep all assets local and dependency-free. No CDN, analytics, fonts,
    framework runtime, service worker, localStorage, IndexedDB, or cookies.

**Tests:** Static asset existence through `importlib.resources`, CSP-compatible
no-inline-script structure, exact control IDs/data attributes, no forbidden
storage/network strings, capability-fragment removal logic inspection,
suspend-before-ack and keepalive-watchdog logic inspection, package wheel
contents, and a manual browser checklist. Exercise JavaScript behavior in the
real-service manual test rather than introducing an unowned JS toolchain.

**Acceptance:** A user can always see whether cloud audio is off/on, what final
caption was heard, which Codex job/thread is active, whether output is speaking,
and whether a control succeeded; stopping the page ends its microphone track.

**Depends on:** RTOP-040 and RTOP-030.

**Unblocks:** RTOP-050.

### RTOP-052A — Add the realtime audio lease and classic compatibility guard

**Purpose:** Give the realtime session exclusive capture ownership when cloud
audio is engaged without making an idle browser page block classic local voice
or requiring edits to the current hands-free WIP.

**Files:**

- `voice_mode/broker/audio_lease.py` (new neutral primitive)
- `tests/test_broker_audio_lease.py` (new)
- `voice_mode/broker/realtime/audio_guard.py` (new)
- `voice_mode/broker/realtime/web.py`
- `tests/test_broker_realtime_audio_lease.py` (new)

**Implementation:**

1. Define an `AudioLease` over a user-private file under the existing broker
   runtime directory. Use an OS advisory lock held by an open descriptor while
   cloud audio is engaged, plus a bounded metadata record for diagnostics.
2. Metadata contains schema, mode, PID, process start evidence where portable,
   acquisition time, and a public recovery command. It contains no transcript,
   repo, thread, token, or provider call identity.
3. Acquisition validates the target is a regular user-owned file in a
   user-private directory and uses safe open flags. It never unlinks an active
   lock inode, follows a symlink, or trusts PID alone.
4. A conflict reads metadata best-effort and returns `audio_owner_busy` with the
   owning mode. It never kills, signals, stops, or replaces the holder.
5. Release unlocks and closes only the descriptor owned by that instance and is
   idempotent. Crash releases the advisory lock; stale metadata is informational
   and overwritten only after successful lock acquisition.
6. Add a `RealtimeAudioGuard` with an injected classic-broker probe, one
   `AudioLease`, and an injected deny-and-teardown callback. Startup may report a
   live classic owner, and `engage()` repeats that probe immediately before
   acquisition to close the startup-to-Start gap.
7. Wire the authenticated web Start control through `engage()` before returning
   browser microphone eligibility. The page can be served indefinitely in
   “Cloud audio is off” without holding the lease.
8. `disengage()` first invokes the injected deny-and-teardown callback and
   releases only when it reports browser suspension or remote call teardown as
   safe. Otherwise it retains the owned lease and surfaces a typed recovery
   state. This task defines and tests the ordering contract; RTOP-050 wires the
   actual session hangup, rollover, controller-expiry, and shutdown operations.
9. Expose a bounded guard snapshot for RTOP-050's status projection without
   exposing the metadata path by default.

**Tests:** Two instances in one process and subprocess contention, crash/stale
metadata, symlink/non-regular/wrong-owner refusal where portable, descriptor
ownership, idempotent release, authenticated Start/Stop lifetime, controller
teardown-before-release callback ordering, unsafe-teardown lease retention,
failure unwind at every post-acquisition step, classic socket guard at startup
and Start, and no broad deletion.

**Acceptance:** An idle realtime page holds no audio lease; authenticated Start
cannot proceed alongside a detected classic audio broker or another lease
holder; Stop releases only its own lease and only after the injected media
teardown contract reports a safe outcome.

**Depends on:** RTOP-010 and RTOP-040.

**Unblocks:** RTOP-050 and RTOP-060.

### RTOP-052B — Adopt the neutral lease in classic hands-free mode

**Purpose:** Complete the cross-mode guarantee after the current audio lane is
available for coordinated editing, without delaying the standalone realtime
vertical slice.

**Files:**

- `voice_mode/broker/handsfree.py`
- `tests/test_broker_handsfree.py`
- `tests/test_broker_audio_lease.py`

No edit to `voice_mode/broker/audio.py` or `tests/test_broker_audio.py` is part
of this task; their post-WIP behavior is run as regression evidence.

**Implementation:**

1. Re-read and preserve the committed mute, cancel-capture, spoken stop/pause,
   and teardown behavior from the current user WIP before editing either file.
2. Make classic hands-free acquire the neutral lease immediately before
   `PersistentVoiceAudio.start()` can open capture, then release it after audio
   teardown in every normal, error, signal, and repeated-shutdown path.
3. Map contention to the same bounded `audio_owner_busy` diagnosis and recovery
   hint as realtime mode. Never stop or signal the current holder.
4. Keep lease ownership outside mute state: mute retains the active session's
   lease, while complete audio/session stop releases it.
5. Add a cross-process test proving classic and realtime cannot both acquire,
   in either order, without importing realtime/OpenAI code on classic startup.

**Tests:** Existing hands-free/audio tests, acquisition-before-capture ordering,
all teardown paths, mute retention, subprocess contention in both directions,
and classic import-laziness regression.

**Acceptance:** Classic and realtime processes share one neutral capture lease,
neither can steal it, and existing classic controls and local-only operation
remain unchanged.

**Depends on:** RTOP-052A and explicit clearance of the current audio WIP.

**Unblocks:** RTOP-071.

## 12. Work package E: composition and command surface

### RTOP-050 — Compose the operator runtime and bounded tool dispatcher

**Purpose:** Join the independently tested planes in one composition root with
deterministic startup, ownership, and shutdown instead of cross-importing
components until they happen to run.

**Files:**

- `voice_mode/broker/realtime/runner.py` (new)
- `voice_mode/broker/realtime/status.py` (new)
- `voice_mode/broker/realtime/__init__.py`
- `tests/test_broker_realtime_runner.py` (new)
- `tests/test_broker_realtime_integration.py` (new)

**Implementation:**

1. Add a `RealtimeOperatorConfig` value created by CLI/config validation before
   any resource acquisition. It includes canonical repo, selected thread mode,
   model/voice/speed/transcription/VAD settings, timeouts, limits, journal
   paths, browser policy, and persistence authorization.
2. Startup order is: validate config/key → run the classic compatibility guard
   → start app-server transport → require exact capabilities → select thread →
   open journal and replay → construct job manager/arbiter/session → start
   loopback web server → write discovery/status record → open browser. Startup
   does not acquire the audio lease.
3. No microphone, held audio lease, or OpenAI call exists after startup. The
   authenticated Start path repeats the compatibility guard, acquires the
   lease, grants browser audio eligibility, and then permits SDP call creation.
4. Require app-server `LIST_THREADS`, `READ_THREAD`, `ATTACH_THREAD`,
   `CREATE_THREAD`, `START_TURN`, `STEER_TURN`, `INTERRUPT_TURN`,
   `SUBSCRIBE_EVENTS`, and `QUERY_DISPOSITION` capabilities. Fail before serving
   the page if the configured binary cannot satisfy the product contract.
5. Use existing `AppServerTransport.start_process`,
   `AppServerHostAdapter.connect`, and `select_thread`. Do not silently fall back
   to `ExecCodexAdapter` under `auto` behavior.
6. Tool dispatcher maps exact four names to job methods and validates the
   current configured repository/thread scope. It cannot import shell, MCP,
   filesystem mutation, approval, or arbitrary host-call functions.
7. Connect job events to UI status immediately and terminal worker events to
   arbiter queue. Connect session transcript/status to the local event stream.
8. RTOP-050 owns the in-process public projection and writes the atomic
   discovery artifact. Public status includes mode, PID/start evidence, local URL without fragment,
   transport/session state, model, voice, session age, speech/response state,
   thread ID, job snapshot, queue counts, rate-limit summary, lease mode, and
   last typed error. Exclude transcript and every secret/private identifier.
9. Persist a user-private atomic discovery record with PID/start evidence,
   health URL, and protocol version so `broker status` can find the realtime
   process without a shared secret. Validate liveness before trusting it and
   ignore/remove only proven stale records. Create and replace it without
   following symlinks, verify its directory and file ownership/mode, and remove
   it only when its process identity still matches this runner.
10. Signal handling schedules async shutdown rather than closing event-loop
    resources from a signal callback. Repeated SIGINT/SIGTERM is idempotent; a
    second signal may shorten the deadline but cannot skip local track denial.
11. Shutdown order is: deny new controls → request browser track suspension →
    hang up and close Realtime calls → close web streams → apply configured
    Codex job grace/interrupt policy → unsubscribe job manager → close host
    adapter/transport → close journal resources → remove the owned discovery
    record → release an engaged audio lease, if owned. Cancellation of one step
    cannot skip later local cleanup; every step has a bounded outcome.
12. Default page-close policy leaves a job alive for a short grace and keeps
   terminal evidence; process shutdown interrupts an active job only after the
   grace or explicit flag. This policy is visible and testable.
13. Run the server side of the bilateral controller watchdog. Heartbeat expiry
    tears down only the Realtime media/session and retains a running Codex job
    under the same grace policy; it cannot silently retain the lease or controller
    slot forever.

**Tests:** Every startup failure index unwinds only acquired resources in reverse
order; exact capability failure; no exec fallback; deterministic thread choice;
no cloud call before Start; tool allowlist and worker-cause refusal; event
wiring; page-close grace; controller heartbeat loss; cancellation during every
shutdown step; signal/repeated shutdown; stale/symlinked discovery; status
redaction; complete scripted delegate/converse/steer/complete/integrate flow.

**Acceptance:** One runner can execute the full fake vertical slice with one
host dispatch, concurrent voice events, one worker delivery, one response per
cause, and no resource leak or dirty classic import.

**Depends on:** RTOP-012, RTOP-021, RTOP-032, RTOP-040, RTOP-041, RTOP-052A.

**Unblocks:** RTOP-051, RTOP-060.

### RTOP-051 — Add configuration, CLI, status discovery, and packaging

**Purpose:** Make the slice installable and diagnosable through one intuitive
command without changing classic defaults or requiring repository checkout
assets.

**Files:**

- `voice_mode/config.py`
- `voice_mode/cli_commands/broker.py`
- `voice_mode/broker/diagnostics.py`
- `tests/test_broker_realtime_cli.py` (new)
- `tests/test_broker_realtime_assets.py`
- `tests/test_broker_cli.py`

The existing Hatch template include already covers these assets, so no
`pyproject.toml` edit is expected; the wheel test verifies that assumption.

**Implementation:**

1. Add environment-backed validated defaults for model, voice, output speed
   (`1.25`), transcription model/language, semantic VAD eagerness, soft/hard
   rollover, browser-open, page-close grace, journal directory, and status
   record. The bind address is not configurable in the first slice.
2. Register `voicemode broker realtime` with `--repo`, `--thread`,
   `--new-thread`, `--voice`, `--speed`, `--no-open`, and optional `--port 0`
   constrained to loopback. Reuse existing Codex executable setting and reject
   exec adapter selection.
3. Help text says Cloud audio, OpenAI key, app-server requirement, explicit
   browser Start, native Realtime voice, and experimental opt-in status in plain
   language.
4. Startup errors distinguish missing key, app-server unavailable/capability
   mismatch, thread selection, audio ownership, local bind, browser open, and
   OpenAI connection. Each gives one copyable recovery action where possible.
5. Browser-open failure is recoverable: keep the loopback server ready and print
   the fragment-bearing URL once to the controlling terminal. Never put that URL
   in logs or status JSON.
6. Read the projection schema and discovery artifact owned by RTOP-050; do not
   redefine or write them here. Extend `broker status --json` by checking
   classic broker state first and the validated realtime discovery record
   independently. Add an optional `realtime` object without removing or
   changing current keys.
7. Human status names mode, cloud mic state, complete Codex thread ID, copyable
   resume command, job status, and last error. It never truncates the thread ID
   when the purpose is locating the Codex task.
8. `broker stop` retains its classic target. Realtime process stop occurs from
   page control or its foreground terminal in this slice; do not make one
   ambiguous stop command kill both owners.
9. Load assets via `importlib.resources` from the already packaged templates
   directory. Add a build test opening the actual wheel and asserting the three
   resources exist.
10. Preserve import laziness: running `voicemode --help`, classic broker, and MCP
    server does not construct aiohttp clients or require the Realtime config.

**Tests:** Click runner help/options/pass-through, every startup error mapping,
browser opener injection/failure, no-open, status with classic/realtime/both/
stale record, full thread ID, secret absence, default config validation, Python
3.10 imports, wheel resource contents, and classic command regression.

**Acceptance:** A built wheel exposes `voicemode broker realtime`, serves all
assets, starts with zero live API calls before browser Start, and leaves every
classic command/output contract compatible.

**Depends on:** RTOP-050.

**Unblocks:** RTOP-060, RTOP-070.

## 13. Work package F: conformance, live qualification, and release

### RTOP-060 — Add cross-plane conformance and fault integration

**Purpose:** Prove the operator, worker, transport, browser boundary, journal,
and classic broker remain correct under duplicate, reordered, disconnected, and
crashing event sequences.

**Files:**

- `tests/fakes/realtime.py`
- `tests/test_broker_realtime_integration.py`
- `tests/test_broker_realtime_faults.py` (new)
- `tests/test_broker_realtime_soak.py` (new)
- `tests/test_broker_realtime_security.py` (new)
- `tests/fixtures/realtime/fault-cases.json` (new)

**Implementation:**

1. Build one reusable scripted harness containing fake monotonic/wall clocks,
   deterministic ID factory, blocking `HostAdapter`, app-server events, fake
   multipart response, scripted sideband, fake browser-controller events,
   journal writer, and collected actions/status.
2. Keep fakes under `tests/fakes`; production code never imports them, and tests
   do not import helper classes from other `test_*.py` modules.
3. Exercise the product sequence: start ready → explicit page Start → SDP and
   sideband ready in both orders → user speech/commit → operator function call
   → accepted Codex job → operator acknowledgement → second conversational turn
   → steer → approval event → completion → queued worker integration → idle
   spoken summary → Stop.
4. Assert complete correlation at every step: one item claim, response cause,
   function call claim/output, job/request/turn IDs, terminal worker event, and
   worker delivery claim/outcome.
5. Inject every supported duplicate event twice and in legal reordered forms.
   Assert at-most-once dispatch, steer, interrupt, response create, function
   output, isolated worker response, visible final row, and spoken integration.
6. Disconnect at every network mutation boundary: before send, after local
   journal claim, after kernel send returns, before provider acknowledgement,
   after acknowledgement, and during close. Expected outcome must be retry-safe,
   delivered, or explicitly uncertain; never optimistic duplication.
7. Crash/reconstruct at every journal claim/outcome boundary and compare the
   replay projection to uninterrupted execution.
8. Test user speech beginning during acknowledgement, during worker integration,
   during rollover, and while a completion arrives. User speech wins; Codex job
   state is unchanged.
9. Test hostile worker summaries and tool arguments: instruction injection,
   Markdown/code, huge Unicode, control characters, path escape, alternate
   thread, unknown keys, prototype-like JSON names, and secret-looking content.
   All are bounded/escaped/refused before policy execution. Worker responses use
   `conversation: "none"` and `tool_choice: "none"`, never enter default context,
   and cannot reach the dispatcher even if a hostile fixture emits a tool call.
10. Test local boundary attacks: wrong Origin/Host/bearer, capability in query,
    path traversal, method confusion, content-type confusion, oversized/slow
    body, forwarded headers, slow event consumer, second controller, dead
    browser/broker watchdogs, suspension acknowledgement loss, stale discovery,
    and symlinked journal/lease/status files.
11. Run 100 synthetic user turns with interleaved status requests and ten
    sequential Codex jobs. Track live tasks, file descriptors, queue sizes,
    remembered IDs, journal growth, and object snapshots. All bounded structures
    return to expected steady state.
12. Run classic broker tests after the realtime suite with no key and with
    `OPENAI_API_KEY=test-key`. Neither environment may trigger live network or
    change classic provider selection.
13. Run tests with xdist and repeated randomized event seeds. Persist only a
    failing seed in assertion output, not transcript payload.

**Required commands:**

```bash
uv run pytest -q --no-cov \
  tests/test_broker_realtime_types.py \
  tests/test_broker_realtime_journal.py \
  tests/test_broker_realtime_arbiter.py \
  tests/test_broker_realtime_jobs.py \
  tests/test_broker_realtime_job_recovery.py

uv run pytest -q --no-cov \
  tests/test_broker_realtime_protocol.py \
  tests/test_broker_realtime_transport.py \
  tests/test_broker_realtime_session.py \
  tests/test_broker_realtime_server.py \
  tests/test_broker_realtime_runner.py \
  tests/test_broker_realtime_cli.py \
  tests/test_broker_realtime_assets.py \
  tests/test_broker_realtime_integration.py \
  tests/test_broker_realtime_faults.py \
  tests/test_broker_realtime_soak.py \
  tests/test_broker_realtime_security.py

uv run pytest -q --no-cov tests/test_broker_*.py
uv run pytest -v -m "not slow"
uv build
make test-package
```

**Acceptance:** The harness records zero duplicate host mutations, responses,
function outputs, worker deliveries, or visible finals; zero worker-caused tool
effects; zero leaked tasks, sessions, peers under the fake boundary, streams, or
lease descriptors; no audio-eligible peer after policy/liveness loss; bounded
memory/queues; and an unchanged classic test surface.

**Depends on:** RTOP-050, RTOP-051, RTOP-052A.

**Unblocks:** RTOP-061, RTOP-070, RTOP-071.

### RTOP-061 — Run explicit real-service and microphone qualification

**Purpose:** Validate the behavior that fakes cannot establish: actual SDP and
sideband schema compatibility, browser echo handling, semantic endpointing,
barge-in, caption quality, and app-server interaction.

**Files:**

- `tests/manual/test_broker_realtime.py` (new manual runner/checklist)
- `docs/benchmarks/realtime-operator-baseline.md` (new, privacy-safe results)
- no recorded user audio or transcript fixtures

**Implementation:**

1. Require an explicit manual flag in addition to `OPENAI_API_KEY`; never infer
   authorization for a billable microphone test from the environment alone.
2. Print the model, voice, transcription model, cloud boundary, expected test
   length, and whether transcript/audio persistence is enabled before Start.
3. Verify the actual call-creation response includes the expected SDP and
   Location behavior and the sideband confirms the requested session profile.
4. Speak a script with short and long natural mid-thought pauses, trailing filler,
   a definitive end, code identifiers, numbers, and a correction. Record only
   timings and human pass/fail notes unless transcript saving is explicitly on.
5. Measure speech-stop to committed item, committed item to response-created,
   response-created to audible output, interruption speech-start to silence,
   tool call to accepted job, and sideband reconnect. Report distributions and
   raw sample count, not an invented SLA.
6. Interrupt acknowledgement and ordinary speech at least five times. Confirm
   unheard audio does not reappear as if heard and no interruption cancels the
   Codex job.
7. Start a real bounded Codex task in a disposable fixture repository, continue
   two conversational turns, steer once, observe an approval-required scenario
   without voice approval, then complete or interrupt explicitly.
8. Confirm the full Codex thread ID and resume command locate the exact worker
   task. Confirm the UI integrates its result once and does not duplicate the
   complete Codex output.
9. Close/reopen the page during the task, sever sideband once, and force a
   fake-clock-assisted or shortened test rollover using the production state
   path. The job survives; uncertain delivery is not replayed.
10. Test built-in microphone/speaker and one common headset topology. Note where
    browser acoustic echo cancellation succeeds or fails; Stop speaking remains
    deterministic.
11. Stop the session and verify the browser track indicator, OpenAI call,
    sideband, local port, Codex grace policy, status record, and audio lease all
    reach their documented terminal state.
12. Record provider model/date, OS/browser/Codex versions, hardware class,
    settings, aggregate timings, failures, and follow-up actions without secret
    identifiers or content.

**Acceptance:** The live run completes natural pauses, barge-in, asynchronous
delegation, continued conversation, steering, explicit approval boundary,
single result integration, reconnect, and teardown. Any failed invariant keeps
the mode experimental and blocks RTOP-071.

**Depends on:** RTOP-060 and an explicit live-test authorization/environment.

**Unblocks:** RTOP-071.

### RTOP-070 — Document operation, privacy, troubleshooting, and changelog

**Purpose:** Make the opt-in mode understandable from installed help and user
documentation, with cloud/audio boundaries and exact recovery steps visible
before release.

**Files:**

- `docs/reference/broker.md`
- `docs/reference/cli.md`
- `docs/guides/configuration.md`
- `docs/concepts/architecture.md`
- `docs/guides/realtime-codex-operator.md` (new)
- `README.md`
- `CHANGELOG.md` under `## [Unreleased]`

**Implementation:**

1. Document the operator/worker split, native Realtime voice tradeoff, app-server
   requirement, explicit cloud audio start, and why classic local VoiceMode
   remains available.
2. Provide one start command, the UI states/controls, thread location/resume,
   job delegation/status/steering/interruption behavior, page close, process
   stop, and session rollover.
3. Document every configuration variable with default, accepted values, privacy
   consequence, and restart requirement. Do not document a remote bind or
   unsupported exec fallback.
4. Explain transcript display versus transcript persistence, raw audio policy,
   journal content, standard-key boundary, capability fragment, and status
   redaction.
5. Troubleshooting begins with visible symptoms: page did not open, cloud audio
   off, microphone denied, app-server unsupported, another audio owner, no
   transcript, response stopped, job waiting approval, sideband reconnecting,
   result delivery uncertain, rollover failed, and stale status.
6. Every symptom names one diagnostic command and one recovery action. Avoid
   advising users to kill broad processes, delete directories, expose a port, or
   paste keys/logs.
7. Add an Unreleased `Added` entry describing the experimental Realtime Codex
   operator and a `Changed` or privacy note only if existing user behavior
   actually changes. Do not bump versions manually.
8. Cross-check generated/handwritten CLI reference behavior and installed wheel
   help so docs cannot claim a subcommand absent from the package.

**Tests:** Strict docs build/check, link check where available, command examples
against Click runner, configuration-name grep, changelog format extraction, and
secret-string scan.

**Acceptance:** A user can install the wheel, understand that audio is cloud,
start the mode, find the Codex task, interpret every UI state, stop it, and
resolve common failures without reading source or raw logs.

**Depends on:** RTOP-051 and RTOP-060.

**Unblocks:** RTOP-071.

### RTOP-071 — Make the opt-in release decision

**Purpose:** Ship only after code, package, docs, privacy boundaries, and real
interaction satisfy the approved design; otherwise preserve a useful testable
experimental lane without overstating readiness.

**Files:**

- `docs/benchmarks/realtime-operator-baseline.md`
- `docs/superpowers/plans/2026-07-20-gpt-realtime-codex-operator-plan.md`

No production or release-artifact edit is part of this task. A separate
explicit release request owns the existing release workflow.

**Implementation:**

1. Verify every task acceptance criterion with command output or manual evidence
   linked from its bead. A green unit suite cannot substitute for the live audio
   acceptance run.
2. Confirm the final wheel includes assets, imports on Python 3.10–3.14, and
   classic VoiceMode starts with no key and no realtime network activity.
3. Run dependency/security scan already used by the repository and inspect new
   network/server code manually for credential, SSRF, redirect, origin, host,
   size, cancellation, and cleanup issues.
4. Review public errors, status, journal, test fixtures, benchmark, and browser
   history/storage for key/capability/call-ID/SDP/transcript leaks.
5. Repeat single-response proof from journal and fake/live traces: no accepted
   item, tool call, Codex request, terminal event, or worker delivery has more
   than one irreversible effect.
6. Confirm the current audio-owner WIP is either integrated through the neutral
   lease and independently committed or remains a documented blocker. Never
   absorb it into a broad final commit.
7. Keep the command labeled experimental and opt-in for the first release. A
   later default-on decision requires longer daily-driver evidence, cost
   visibility, and a wake/re-engagement product decision.
8. Do not run `make release` from this task. The repository release process
   changes versions, changelog sections, commits, tags, and pushes, and needs a
   separate explicit user authorization on up-to-date `master`.

**Acceptance:** All automated and manual invariants pass, no secret/privacy
finding remains, package/docs match, and outstanding limitations are explicit.
If not, close the completed engineering beads but leave this gate open with the
specific evidence needed.

**Depends on:** RTOP-052B, RTOP-060, RTOP-061, RTOP-070.

**Unblocks:** a separately authorized release, not included here.

## 14. Beads conversion

Create one epic titled `GPT-Realtime Codex operator` with external reference
`RTOP-EPIC` and one task bead for every RTOP item. Use priority 1 for the
foundations, arbiter, async job manager, transport/session, server/UI,
composition, CLI, audio lease, and conformance; priority 2 for fixtures,
qualification, docs, and release gate unless live failures raise priority.

Each task description includes its Purpose, exact Files, concise Acceptance,
plan path, and external reference. Apply dependencies exactly as section 7,
then add every task as a child of the epic without treating epic membership as a
blocking edge.

After creation:

```text
br dep cycles
br ready --json
br show <epic-id> --json
br sync --flush-only
```

`br dep cycles` must be empty. The ready set should begin with RTOP-001 only,
apart from explicitly parallel groundwork whose dependency is satisfied. The
current Beads database has shown busy errors during planning; do not kill the
other owner or mutate JSONL manually. Wait for the lock, use normal structured
commands, and report a concrete conversion blocker if the database remains
unavailable.

## 15. Commit and ownership strategy

Implementation commits stay narrow and explicit. A task commit stages only the
files in that task plus its own Beads JSONL changes after `br sync --flush-only`.
Never use `git add -A` in this dirty shared checkout.

Preferred commit sequence follows the dependency graph:

1. fixtures;
2. types and journal;
3. arbiter;
4. Codex jobs/recovery;
5. OpenAI protocol/transport/session;
6. loopback server and assets;
7. realtime lease/guard, then runner/CLI/status/package;
8. conformance/fault tests;
9. classic lease adoption after dirty work resolves;
10. docs/changelog and qualification evidence.

Before every commit, capture `git status --short`, inspect the explicit staged
paths, and run the narrow task tests. Before integration commits, run the
complete broker slice. Existing unrelated WIP remains unstaged even if its tests
are part of regression verification.

## 16. Review protocol and steady-state record

This plan is ready for Beads only after four sequential reviews have been
integrated. Each round checks the complete current plan, not the original draft.

1. **Architecture and dependency review:** Verify ownership boundaries, task
   granularity, DAG, current code seams, and avoidance of premature native or
   multi-worker scope.
2. **External contract review:** Verify every OpenAI endpoint, header, payload,
   session field, event, interruption, transcription, and lifetime assumption
   against current official documentation.
3. **Reliability and security review:** Attack idempotency, crash recovery,
   cancellation, prompt/tool boundaries, loopback trust, credential handling,
   resource cleanup, and privacy.
4. **Execution and testability review:** Give the plan to a fresh implementer,
   validate exact files/dependencies/acceptance, run the DAG checks, and require
   only marginal final revisions.

After every round:

- select one obscure task and confirm it is independently implementable;
- render/check the DAG for cycles and orphan tasks;
- sample at least five non-obvious choices for explicit rationale;
- compare the revision size with the prior round and record whether changes are
  structural, local, or editorial.

Record round results below with reviewer focus, integrated changes, rejected
changes and rationale, graph result, self-containment result, and revision size.
The most recent round must be local/editorial rather than structural.

### Review round log

- Round 0: initial grounded draft.
- Round 1, architecture and dependencies: integrated the reviewer’s four
  structural findings. RTOP-052 became RTOP-052A realtime Start/Stop ownership
  plus RTOP-052B deferred classic adoption, so user-owned audio WIP blocks only
  release. Lease lifetime is now session-scoped. RTOP-013 promotes
  `recover_request` to `HostAdapter`. Browser data-channel captions are
  provisional while sideband-normalized local finals are authoritative. The
  graph and RTOP-060 dependencies were reconciled, and RTOP-050 now owns status
  projection writes while RTOP-051 only reads/exposes them. The output profile
  mismatch was resolved in favor of audio plus transcript events. No reviewer
  change was rejected. Automated parsing found 20 tasks, one root, one terminal,
  no unknown dependencies, no cycle, and no orphan. RTOP-013 was sampled and is
  independently implementable from its files, interface behavior, tests, and
  acceptance. Rationale sampling passed for sideband authority, aiohttp reuse,
  audio-only output, display-only transcription, fetch NDJSON, and the pure
  arbiter. Revision size was structural: two net task nodes plus local contract,
  workflow, graph, and design-header edits.
- Round 2, external contracts: a bounded audit against the official OpenAI
  Realtime model, WebRTC, sideband, conversation, VAD, transcription, and API
  reference pages replaced every vague wire assumption. Primary item lifecycle
  events are now `conversation.item.added`/`.done`, with `.created` retained as
  an explicit documented compatibility alias. Cancellation waits for
  `response.done` status `cancelled`; response statuses, error envelopes,
  function-call extraction, caption ordering, and audio-only output are exact.
  Push-to-talk now switches turn detection to null, confirms `session.updated`,
  and uses buffer clear/commit while the arbiter retains response ownership. The
  SDP answer is withheld until sideband policy is ready, and same-call sideband
  reattach is best-effort because the official contract does not guarantee
  reconnect behavior. No checked contract change was rejected. Automated
  parsing still found 20 tasks, one root, one terminal, no unknown dependency,
  no cycle, and no orphan. RTOP-031 was sampled and remains independently
  implementable from its fixed endpoint, multipart, Location, WebSocket,
  redaction, timeout, and cleanup contracts. Rationale sampling passed for
  server-held credentials, answer withholding, audio-only output, completed
  caption authority, semantic-VAD defaults, and conservative reconnect policy.
  Revision size was local: protocol/session/UI clauses and matching tests changed
  without adding tasks or changing the DAG.
- Round 3, reliability and security: integrated all five reviewer findings and
  the adjacent file-safety consequences. Worker completions now use
  tool-disabled, out-of-band responses that never enter default conversation,
  with dispatcher cause checks as defense in depth. Sideband loss requires an
  acknowledged browser track suspension or authenticated server-side WebRTC
  hangup before lease release. Bilateral heartbeat/keepalive watchdogs cover
  silent page or broker death. Duplicate-sensitive sends classify cancellation
  under a shielded bounded operation before releasing their lock. `uncertain`
  is explicitly recoverable and retains the one-job slot. Journal sequence
  allocation, retention, discovery files, and ambient proxy behavior gained
  cross-process, symlink, ownership, and credential protections. No reviewer
  change was rejected. Automated parsing still found 20 tasks, one root, one
  terminal, no unknown dependency, no cycle, and no orphan. RTOP-052A was
  sampled and is independently implementable from its lease lifetime, call
  teardown ordering, contention, ownership, and failure-unwind rules. Rationale
  sampling passed for out-of-band worker speech, cause-gated tools, bilateral
  liveness, hangup-before-release, cancellation classification, and recoverable
  uncertainty. Revision size was structural within existing task contracts; no
  task node, ownership boundary, or dependency changed.
- Round 4, execution and testability: the fresh implementer found no new product
  architecture issue. RTOP-052A was narrowed to the neutral lease, exact
  `audio_guard.py` seam, and `web.py` Start/Stop wiring; RTOP-050 retains actual
  session hangup, rollover, watchdog, and shutdown composition. Every previously
  conditional ownership entry now names an exact file or explicitly excludes
  it. RTOP-013 names both host-contract and direct app-server tests, RTOP-060
  owns one exact fault fixture, and RTOP-041 explicitly uses static contract
  checks plus the RTOP-061 manual browser runner rather than inventing an
  unowned JS toolchain. The configured `1.25` output speed was also carried
  through the pinned session profile and command/config tasks. No proposed
  change was rejected. Automated comparison found 20 tasks, one root, one
  terminal, no unknown dependency, no cycle, no orphan, and exact agreement
  between section 7 and every task’s `Depends on` line. RTOP-070 was sampled and
  is independently implementable from its exact docs, README, changelog, tests,
  and acceptance. Rationale sampling passed for the neutral lease location,
  narrow guard ownership, no new JS test runtime, existing Hatch asset rules,
  explicit live-test authorization, and release separation. Revision size was
  local/editorial: file ownership and acceptance wording were tightened without
  adding a task, moving a boundary, or changing the DAG.

## 17. Definition of done

Engineering is complete when all of the following are true:

- `voicemode broker realtime` is present in the built wheel and starts a
  loopback-only reference page without opening cloud audio before Start.
- The browser and sideband establish the same `gpt-realtime-2.1` call through
  the documented multipart SDP and Location/call-ID contracts.
- Semantic VAD uses low eagerness, response auto-creation is off, interruption
  is on, and only the response arbiter creates responses.
- User captions, operator output transcripts, microphone state, complete Codex
  thread ID, job state, approvals, reconnect, rollover, and errors are visible.
- A Codex job is accepted asynchronously; voice conversation continues; status,
  steer, and explicit interruption work; a second job returns busy.
- Barge-in stops operator audio without interrupting the Codex job.
- One accepted item produces at most one response; one function call produces at
  most one host mutation/output; one worker terminal produces at most one
  integration, including duplicate/reconnect/crash sequences.
- Codex approvals remain in Codex, worker text cannot authorize tools, and the
  four-function allowlist is the entire Realtime action surface.
- API key, capability, call ID, SDP, audio, and default-off transcript content
  are absent from public status, logs, journal, fixtures, browser storage, and
  errors.
- Realtime and classic capture honor the same cross-process audio lease, or the
  release gate remains blocked and the experimental command refuses a detected
  classic audio owner.
- Session reconnect and pre-60-minute rollover keep the Codex job and delivery
  decisions without replaying speech or tool effects.
- Page close, signal, startup failure, network failure, and process recovery
  leave no leaked microphone track under controllable client behavior, HTTP
  session, WebSocket task, app-server process, local port, discovery record, or
  owned lease.
- Focused, complete broker, non-slow, build, and package tests pass; the explicit
  real-service run proves natural pauses, barge-in, delegation, continued talk,
  steering, approval boundary, single integration, and teardown.
- Existing hands-free/audio/CLAUDE/user files are preserved outside explicitly
  coordinated later integration.
- Documentation and Unreleased changelog match the installed command, and no
  release/version/tag/push occurs without separate authorization.
- Four plan reviews have reached steady state, the Beads graph is cycle-free,
  and every task contains exact evidence or a concrete blocker.
