# GPT-Realtime Codex Operator Design

**Status:** Approved for implementation; reviewed plan complete

**Date:** 2026-07-20

**Initial target:** An opt-in, cloud-first vertical slice on macOS using
`gpt-realtime-2.1`, a loopback browser client, and the existing Codex app-server
adapter.

## Product Contract

VoiceMode will add a realtime interaction plane that can listen, respond, and
handle interruption while Codex works asynchronously. The realtime model is the
only speaking agent. Codex remains the coding and tool-using worker, and its
progress and results return to the realtime model as correlated job events.

This is the smallest useful version of the interaction model demonstrated by
Thinking Machines: a fast conversational operator remains present while slower
work continues elsewhere. It does not require Inkling or a custom-trained
model. The behavior comes primarily from the harness: streaming audio,
semantic endpointing, asynchronous delegation, state reconciliation, and a
strict single-response policy.

The new mode is additive. Existing `converse` and classic broker hands-free
flows remain available for local STT/TTS, offline use, and the configured
`am_michael` voice.

## Goals

- Let the user speak naturally during an engaged session without repeating a
  wake phrase for every turn.
- Use semantic endpointing so ordinary pauses are less likely to submit an
  unfinished thought.
- Stop model audio immediately on barge-in and preserve the conversation at the
  point the user actually heard.
- Start a Codex task without blocking the voice session, then support status,
  steering, and interruption while the task runs.
- Show live user and operator transcripts, Codex job state, and the current
  listening, thinking, or speaking state on screen.
- Enforce one speaking authority, one dispatch per accepted tool call, and one
  integration of each terminal Codex result.
- Keep OpenAI credentials and Codex control on the local Python sideband rather
  than in browser JavaScript.

## Non-goals

- Reproducing Inkling's trained interaction policy or the full micro-turn
  behavior of the Thinking Machines research system.
- Rewriting VoiceMode in Rust or building a GPUI/native desktop application in
  this slice.
- Replacing the existing broker, `converse`, local Whisper, or Kokoro paths.
- Preserving `am_michael` in realtime mode. Native Realtime audio uses an
  OpenAI-provided session voice; routing text through Kokoro would give up the
  speech-to-speech timing and interruption behavior this mode is meant to test.
- Giving the realtime model arbitrary shell, filesystem, MCP, or Codex
  app-server access. It receives only the bounded job-control tools defined
  here.
- Multiple simultaneous Codex workers, remote browser access, mobile clients,
  video, repository memory extraction, or automatic ambient cloud streaming.

## Why This Architecture

Three designs were considered.

1. **Extend the current Python microphone loop.** This reuses more code, but the
   current hands-free runner waits for a Codex turn to finish and Python would
   have to own echo cancellation, output truncation, and streaming audio timing.
   It cannot deliver the intended interaction quality without a larger audio
   rewrite.
2. **Build a separate native Rust or GPUI application.** This can become the
   polished product shell later, but it adds a new application, packaging
   surface, and protocol before the operator-worker interaction has been
   validated.
3. **Add a browser WebRTC client with a Python sideband.** This uses browser
   media handling for low-latency audio and echo cancellation, while the
   existing Python broker keeps credentials, Codex attachment, policy, and
   diagnostics. It is the chosen first slice because it tests the product
   behavior with the least irreversible work.

The provider boundary remains explicit even though the first implementation is
OpenAI-specific. Realtime session transport and events sit behind an adapter so
a later local duplex model or another realtime API can implement the same
operator and job contracts.

## Architecture

```text
browser on loopback
  microphone + WebRTC + transcript/status UI
              |
              | SDP offer/answer and realtime media
              v
        OpenAI Realtime session
              ^
              | sideband WebSocket
              |
Python VoiceMode broker
  session manager -> response arbiter -> bounded operator tools
                                           |
                                           v
                                   Codex job manager
                                           |
                                           v
                              Codex app-server adapter
```

The browser and broker connect to the same Realtime call. The browser carries
media over WebRTC. The broker attaches through the documented sideband
WebSocket, configures the session, handles function calls, and observes response
lifecycle events. The browser never receives the standard API key and never
talks directly to Codex.

### 1. Loopback WebRTC Client

`voicemode broker realtime` starts a loopback-only HTTP server and opens a
minimal static page. The first implementation uses plain HTML, CSS, and
JavaScript rather than adding a frontend framework. The page:

- requests microphone permission only after an explicit Start action;
- acquires the broker's audio lease before opening its media track and releases
  both on Stop or page teardown;
- creates an `RTCPeerConnection`, sends its SDP offer to the local broker, and
  applies the returned SDP answer;
- plays the remote Realtime audio track through one audio element;
- renders input-transcription, output-transcription, speech-started,
  speech-stopped, response, error, and local job events;
- exposes Stop session, push-to-talk fallback, stop speaking, cancel job, and
  copy-transcript controls.

Realtime engagement suspends classic local capture before the browser opens its
microphone. If the existing owner cannot release the device cleanly, startup
refuses with the owning mode and recovery action instead of creating two capture
loops. While engaged, the browser media track is the sole microphone transport;
the broker remains the policy owner of the lease.

The page has no API key, shell bridge, general RPC method, or externally bound
listener. A random per-launch capability token protects its loopback endpoints
from unrelated local pages, and all mutating requests require that token and an
expected Origin.

Input transcription is enabled for display and diagnostics, but it is treated
as an approximate caption rather than the authoritative audio input to the
realtime model. Transcript persistence continues to follow VoiceMode's existing
explicit setting; the page itself does not persist content.

### 2. Realtime Session Manager

The browser posts its SDP offer to the local broker. In this first slice, the
broker proxies the SDP exchange with its server-side OpenAI credential instead
of minting a browser-visible ephemeral token. It extracts the call identifier
from the response `Location` header, attaches and configures the sideband
WebSocket, then returns the SDP answer so microphone media is never connected to
an ungoverned call. This follows OpenAI's documented
server-control topology, keeps the standard credential out of browser storage
and developer tools, and stays simpler than a second token-minting path while
the client is loopback-only.

The initial session profile is:

- model `gpt-realtime-2.1`, configurable only through validated broker config;
- audio output with provider output-transcript events supplying visible text,
  defaulting to one supported OpenAI session voice from validated broker config
  at `1.25` speed, allowing another supported voice only before the first audio
  response and a validated speed change only between responses;
- semantic VAD with low eagerness;
- interruption enabled and automatic response creation disabled, so the
  response arbiter owns every `response.create` event;
- input-audio transcription enabled for the visible user transcript;
- only the four Codex job-control functions in this design;
- compact operator instructions that require conversational brevity, explicit
  delegation, and no invented worker progress.

A Realtime session currently has a 60-minute maximum. The manager starts a
replacement before expiry, carrying a bounded text summary, active job IDs,
their Codex thread IDs, and unresolved terminal events. Raw audio and the full
conversation are not copied. Rollover is visible in diagnostics but should not
cancel a running Codex job.

### 3. Response Arbiter

The response arbiter is the single writer of `response.create`. It consumes
committed user-audio items, completed function outputs, and queued Codex job
events, then permits at most one active spoken response.

Its invariants are enforced with IDs and state, not prompt wording:

- each committed user item can trigger at most one response;
- each Realtime function `call_id` can execute and receive an output at most
  once;
- each Codex job terminal event can enter Realtime context at most once;
- only one audio response may be active;
- a user turn takes priority over a queued worker notification;
- barge-in cancels the current audio response but does not cancel a Codex job;
- stopping or cancelling a job requires an explicit job-control intent;
- a reconnect reconstructs these decisions before generating new audio.

When the user interrupts, WebRTC and Realtime handle played-audio accounting and
truncate the unheard output. The arbiter marks that response interrupted and
does not replay its remainder. A later user request to repeat is a new response,
not a retry of the interrupted audio stream.

Worker completion never talks over the user. If speech or a response is active,
the event waits. At the next idle boundary, the broker creates one out-of-band,
tool-disabled response with bounded worker data as response-local input. Neither
that input nor its spoken output enters the default conversation, and the
dispatcher independently rejects worker-caused tool calls. The UI may show
progress immediately even while spoken integration is deferred.

### 4. Bounded Operator Tools

The Realtime session receives four function tools:

```text
delegate_codex(task, repo_root?, thread_id?, client_request_id?)
  -> {job_id?, thread_id?, status: "accepted" | "busy" | "rejected", error?}

get_codex_job(job_id)
  -> {job_id, status: "accepted" | "starting" | "running" | "completed" |
      "failed" | "interrupted" | "uncertain" | "not_found",
      summary?, last_progress?, error?}

steer_codex(job_id, instruction, client_request_id?)
  -> {job_id, status: "steer_accepted" | "not_steerable" | "not_found" |
      "rejected"}

interrupt_codex(job_id, client_request_id?)
  -> {job_id, status: "interrupt_accepted" | "already_terminal" |
      "not_found" | "rejected"}
```

`delegate_codex` returns as soon as the app-server turn is accepted. It never
waits for terminal output. The operator can acknowledge naturally, continue the
conversation, and query or modify the job later.

`repo_root` and `thread_id` are hints, not unrestricted paths or identities.
The broker resolves them against its configured repository and attached Codex
threads. Ambiguity creates a broker-owned thread or returns a typed refusal; it
never silently controls an unrelated recent thread.

Every mutating call carries the Realtime `call_id`, and an optional model-issued
`client_request_id` is only an additional correlation key. Repeated calls with
the same effective key return the recorded outcome without dispatching again.

### 5. Asynchronous Codex Job Manager

The Codex job manager wraps the existing app-server host adapter and replaces
the current request-shaped wait with a durable job lifecycle:

```text
accepted -> starting -> running -> completed
                              -> failed
                              -> interrupted
                              -> uncertain
```

Each job records its job ID, idempotency key, canonical repository, Codex thread
and turn IDs, timestamps, last progress summary, terminal result, and delivery
state. Transcript content follows existing persistence policy; identifiers and
state transitions are always journaled for recovery.

App-server notifications update the job without blocking the Realtime event
loop. A terminal Codex response becomes a structured event containing a short
result summary plus a reference to the complete visible output. Codex workers
receive no voice tools and never invoke TTS, so they cannot create a second
spoken response.

The broker gives the isolated integration response only a size-bounded status
and summary, never an arbitrary worker transcript. Worker text is encoded as
data with its job and thread IDs, `conversation` is `none`, and `tool_choice` is
`none`. The dispatcher also refuses any tool call caused by a worker delivery.
All user-caused tool calls still pass deterministic allowlists, and Codex's own
sandbox and approval policy remain authoritative; realtime mode does not create
a voice shortcut around an approval prompt.

The first slice permits one active Codex job. A second delegation returns a
typed `busy` result with the active job ID, while steering, status, interruption,
and ordinary conversation remain available. This constraint removes scheduling
ambiguity while the interaction contract is being validated.

### 6. Visible State and Transcript

The browser page shows one authoritative state strip and a chronological event
view:

```text
[listening]  [Codex job vmj_7c91: running]  [session 18m]

You: Make the broker resilient, and keep talking with me while that runs.
Operator: I started that in Codex. We can refine it while it works.
Codex vmj_7c91: inspecting broker recovery paths
```

The UI distinguishes captions from final transcript segments and distinguishes
operator speech from Codex worker results. It does not print a Codex terminal
result a second time if that result is already visible in the attached Codex
task; it shows a linkable thread/job reference and the operator's integration.

`voicemode broker status --json` adds the realtime transport state, model,
session age, active response ID, attached Codex job and thread IDs, queued
worker-event count, and last recoverable error. Secrets, raw SDP, call tokens,
and transcript text are excluded.

## Turn and Job Flows

### Conversational turn

1. Browser WebRTC streams microphone audio to the Realtime session.
2. Semantic VAD commits the user item and interrupts current audio when needed.
3. The sideband receives the committed item and the arbiter claims it once.
4. The arbiter creates one response; audio returns over WebRTC while transcript
   events update the page.
5. Completion or interruption closes that response ID before another can start.

### Delegated Codex turn

1. The operator calls `delegate_codex` with a bounded task and optional thread
   hint.
2. The sideband deduplicates the call, creates the job, starts the Codex turn,
   and immediately returns `accepted` as the function output.
3. The arbiter lets the operator acknowledge the accepted job in one short
   response.
4. App-server events advance the job while the Realtime session remains free
   for conversation, status requests, steering, or interruption.
5. The terminal event is journaled first, shown in the UI, then queued for one
   isolated, tool-disabled spoken integration at the next idle boundary.
6. After the out-of-band response is classified, the journal marks the terminal
   event delivered or uncertain; reconnect and rollover cannot deliver it again.

## Failure and Recovery

- **Realtime connection loss:** Keep Codex jobs alive, mark the voice session
  disconnected, disable microphone eligibility, try a bounded best-effort
  sideband reattach, and otherwise roll to a fresh call. The page must
  acknowledge that its track is disabled; if it cannot, the broker uses the
  authenticated WebRTC hangup endpoint before releasing the audio lease.
  Rehydrate active job state, and never replay terminal speech merely because
  transport returned.
- **Browser closes or loses microphone permission:** Stop cloud audio
  immediately. Bidirectional heartbeat/keepalive expiry covers silent page or
  broker death when `pagehide` never runs. The sideband remains for a short grace
  period so a Codex job can finish and journal its result, then closes the
  Realtime call.
- **Codex app-server loss:** Mark the job `uncertain`, reconnect, and inspect the
  known thread and turn before retrying. An uncertain turn is never submitted
  again automatically.
- **Duplicate tool or event delivery:** Return the journaled result for the same
  idempotency key. Terminal result injection is a compare-and-set transition.
- **Realtime tool failure:** Return a typed function result so the operator can
  explain the failure without ending the voice session.
- **Session rollover:** Start before the 60-minute limit, carry only bounded
  text state, and preserve all Codex job identities and delivery decisions.
- **Local server failure:** Existing classic VoiceMode paths remain usable, and
  the command reports one exact recovery action without starting another
  microphone owner.

## Privacy and Security

Realtime mode is cloud audio by definition and must say so before the first
Start action. Wake integration is deferred from this slice: the existing local
wake detector remains local, but it neither opens nor feeds a Realtime call.
Cloud audio starts only after explicit browser engagement.

The HTTP server binds to loopback on an ephemeral port, uses a per-launch
capability token, validates Origin, applies request and event size limits, and
never serves repository files. The standard OpenAI key stays in the broker.
Codex tool arguments pass through path, thread, and action allowlists before the
app-server adapter sees them.

Recordings and transcripts remain off unless the user has enabled their
respective persistence settings. Operational logs contain IDs, durations,
states, provider/model names, and error codes without API keys, SDP, audio, or
transcript content.

## Implementation Boundaries

The implementation should add a narrow realtime package beneath the broker
rather than mix provider-specific event handling into `handsfree.py`. Expected
responsibilities are:

- Realtime provider adapter and event types;
- loopback HTTP/SDP bridge and static reference client;
- session manager and sideband lifecycle;
- response arbiter and idempotency journal;
- asynchronous Codex job manager over the app-server adapter;
- CLI entry point, status fields, tests, and an Unreleased changelog entry.

The extraction starts from the current seams rather than adding another Codex
transport. `voice_mode/broker/hosts/base.py` remains the host contract,
`voice_mode/broker/hosts/app_server.py` and `app_server_transport.py` retain
thread and JSON-RPC ownership, and `voice_mode/broker/hosts/events.py` retains
notification normalization. The blocking wait now performed by
`HostTurnRunner` in `voice_mode/broker/handsfree.py` is split from turn startup
so the new job manager can subscribe to the same normalized events
asynchronously. Existing recovery decisions in `voice_mode/broker/recovery.py`
and journal/state primitives in `voice_mode/broker/runtime.py` are reused or
generalized rather than reimplemented with different retry semantics.

The command is added through `voice_mode/cli_commands/broker.py`, but it does
not enter the classic `run_handsfree_broker` audio loop. Realtime mode requires
the app-server adapter in the first slice because steering and correlated event
recovery are part of its product contract. If app-server capability probing
fails, startup explains the requirement and exits; it does not silently fall
back to the weaker exec adapter.

Existing audio ownership work and classic hands-free behavior are separate
lanes. The realtime mode may reuse neutral state and diagnostics types, but it
must not make WebRTC or OpenAI a dependency of classic broker startup.

## Verification

### Automated tests

- Session configuration pins the selected model, semantic VAD policy, manual
  response creation, bounded tools, and input transcription.
- A committed user item produces one `response.create` under duplicate and
  reconnect event delivery.
- A repeated function `call_id` or idempotency key starts one Codex job.
- Delegation returns before the fake Codex turn completes.
- Progress, completion, steering, interruption, failure, and uncertain recovery
  follow the allowed job transitions.
- A terminal result is injected once, waits behind active user speech, and
  survives reconnect without replay.
- Barge-in cancels spoken output without interrupting the Codex job.
- Session rollover carries the bounded summary and active job references while
  excluding secrets and audio.
- Loopback endpoints reject wrong tokens, wrong origins, oversized payloads,
  non-loopback binds, and unapproved repository or thread targets.

Integration tests use scripted fake Realtime sideband events and a fake Codex
app-server transport. They exercise the complete accept, acknowledge, continue
talking, steer, complete, and integrate sequence without microphones or network
credentials.

### Real-service acceptance run

With an explicit OpenAI API key and a live Codex app-server session:

1. Speak a sentence containing natural mid-thought pauses; it submits once and
   the visible transcript matches the intent.
2. Interrupt operator audio and continue immediately; unheard audio does not
   reappear in later context.
3. Start a Codex task, discuss a refinement while it runs, steer it, and hear
   one concise integration when it completes.
4. Cancel a running job without ending the voice conversation.
5. Disconnect and reconnect the browser while a job runs; the job survives and
   its result is integrated at most once.
6. Confirm classic local `converse` and broker hands-free tests still pass with
   no OpenAI credential configured.

The first run records measured endpointing delay, interruption-to-silence,
function-call acknowledgment, Codex dispatch overhead, and reconnect recovery.
The design does not set fabricated latency targets before this baseline exists.

## Delivery Sequence

1. Extract asynchronous Codex job lifecycle and idempotency from the existing
   blocking host-turn runner, covered by fake app-server tests.
2. Add the response arbiter and provider-neutral Realtime event contract, then
   prove single-response and terminal-delivery invariants with scripted events.
3. Add the OpenAI SDP bridge and sideband adapter with model/session config.
4. Add the loopback WebRTC transcript/status client and explicit cloud-audio
   consent surface.
5. Wire the opt-in CLI command and status diagnostics, then run the real-service
   acceptance sequence.

Each stage remains testable without a live microphone until the final reference
client run.

## External Contracts

The design relies on current OpenAI Realtime behavior documented in:

- [GPT-Realtime-2.1 model](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
- [WebRTC connection guide](https://developers.openai.com/api/docs/guides/realtime-webrtc)
- [Realtime conversations and interruption](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Voice activity detection](https://developers.openai.com/api/docs/guides/realtime-vad)
- [Realtime tools](https://developers.openai.com/api/docs/guides/realtime-mcp)
- [Server-side controls and sideband connections](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- [Realtime API reference](https://developers.openai.com/api/reference/resources/realtime)
- [Realtime call hangup](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/hangup)

These details must be covered by an adapter-level contract test or a documented
manual compatibility check because model names, session fields, and event
schemas can evolve independently of VoiceMode.
