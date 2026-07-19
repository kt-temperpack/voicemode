# Codex Conversation Broker Design

**Status:** Approved direction; implementation-ready design

**Date:** 2026-07-19

**Initial target:** Codex on macOS, with portable interfaces for later clients

## Summary

VoiceMode will add a long-running local conversation broker that owns the
microphone, wake-word detection, turn state, playback, and interruption. Codex
remains the reasoning and tool-using agent. A thin MCP adapter connects the two,
so an audio session can survive individual model and tool calls without moving
agent behavior into the audio service.

The experience should feel like an open call: saying “Computer” or pressing a
hotkey engages the agent, follow-up turns remain open briefly, speech can be
interrupted immediately, and inactivity returns the microphone to local-only
wake detection. Spoken responses default to one or two sentences; the complete
answer stays visible in Codex. Durable context is structured and scoped to the
current Git repository. Ambient audio and raw recordings are discarded unless
the user explicitly enables saving.

Inkling is an evaluation target, not a production dependency. It accepts audio
and produces text, so it can be benchmarked as an alternate reasoning or memory
extraction backend without owning the realtime audio loop.

## Goals

- Make voice interaction feel continuously available without continuously
  sending microphone audio to a cloud service.
- Keep spoken responses brief by default while preserving complete technical
  detail on screen.
- Preserve useful decisions and working preferences across Codex sessions
  without mixing context between repositories.
- Reuse VoiceMode's existing provider failover, streaming playback, control
  channel, exchange logging, and conch coordination.
- Keep the broker independent of any one model so Codex, Inkling, or later
  backends can be evaluated behind stable interfaces.

## Non-goals

- Replacing Codex as the coding agent or rebuilding its tool runtime.
- Sending a continuous microphone stream to a hosted model.
- Shipping a general multi-client broker in the first release.
- Training or self-hosting Inkling as part of the broker MVP.
- Automatically preserving every transcript, raw audio segment, or ambient
  conversation.
- Building a full graphical application. A menu-bar status surface can follow
  once the broker protocol is stable.

## User Experience

### Activation and session lifecycle

The broker starts with the user's login and remains in `asleep`, where a local
wake engine and global hotkey are the only active inputs. It maintains only the
small in-memory audio window required for wake detection and continuously
overwrites that buffer.

Saying “Computer” or pressing the hotkey moves the broker to `engaged`, plays a
short local acknowledgement, and records the first utterance. After each agent
reply it opens a configurable follow-up window, so the user can continue without
repeating the wake phrase. Silence expires the window and returns to `asleep`.
“Go to sleep,” the stop control, or the hotkey closes it immediately.

The state machine is:

```text
asleep -> engaged -> listening -> thinking -> speaking
   ^          |          ^            |          |
   |          |          |            |          +-- barge-in --> listening
   |          |          |            +-- failure --> engaged
   +----------+----------+-- timeout / explicit sleep
```

Only the broker owns audio devices. MCP tools submit or await semantic events;
they do not open independent microphone streams. This prevents VoiceMode and
Spokenly-style recorder collisions.

For the MVP, “open call” means that Codex has entered a voice session and keeps a
broker long-poll active between replies. The daemon remains wake-capable when no
poll is active, but it can only buffer one activated utterance; MCP cannot start
a new Codex model turn by pushing from the server. Waking a completely idle
Codex host requires a future host-native callback and is outside the MVP.

### Speech contract

Every Codex response has two representations:

- `display_text` is the complete answer rendered in Codex.
- `spoken_summary` is normally one or two sentences and contains the conclusion,
  the most consequential caveat, and any immediate action the user must take.

The broker speaks `spoken_summary`. If the adapter supplies only display text,
the renderer selects complete leading sentences up to the configured budget and
never cuts a sentence in half. Code, commands, tables, paths, citations, and long
enumerations remain visual unless the user explicitly asks to hear them.

“More” requests the next useful layer of detail from Codex rather than replaying
the full visible answer. “Repeat” replays cached speech without another model
call. “Stop” cuts playback in the existing control channel, and a barge-in moves
directly to listening within the same broker session.

## Components

### 1. Broker daemon

`voice_mode.broker` is a long-running local process with one responsibility:
coordinate the realtime conversation state. It owns audio capture, wake events,
follow-up timers, interruption, and the bounded queue of completed utterances.
It exposes a versioned protocol over a user-only Unix domain socket on macOS and
Linux, with a platform-local equivalent on Windows.

The broker must remain useful without Codex attached. It can acknowledge wake,
report that no agent is connected, accept a single pending utterance, and return
to sleep safely. It never invents agent responses.

### 2. Activation adapters

Wake detection and hotkeys implement a small adapter interface that emits
`activate`, `sleep`, and `push_to_talk` events. The first implementation uses a
local wake-word engine and a configurable phrase, defaulting to “Computer.” The
hotkey is mandatory because wake recognition will never be perfect and noisy
rooms need a deterministic fallback.

The wake engine receives microphone frames locally. Frames are released after
the rolling wake window unless an activated utterance is captured under the
normal VoiceMode recording policy.

### 3. Codex MCP adapter

The Codex adapter is intentionally thin. It provides three broker-backed tools:

- `voice_session_open(repo_root, codex_session_id)` registers the active Codex
  session and returns broker capabilities.
- `voice_session_turn(session_id, spoken_summary, memory_updates, wait_seconds)`
  sends the prior assistant summary for playback, applies validated memory
  updates, then long-polls for the next activated utterance or named control
  intent. It returns a discriminated `utterance`, `intent`, `idle`, or `error`
  result rather than overloading transcript text.
- `voice_session_close(session_id)` detaches Codex without stopping the broker.

`voice_session_turn` is the conversational loop: Codex calls it after producing
each answer, VoiceMode speaks the short summary, and the tool returns the next
transcribed user turn. The daemon persists across calls, so wake state, replay
history, and queued interruptions survive the MCP request boundary. An `idle`
result is normal; while the voice session remains active, the adapter reissues
the long-poll instead of synthesizing filler speech.

MCP cannot inject speech into a model while Codex is executing another tool.
During long work the broker may capture one explicit wake utterance and queue it,
but Codex receives it only at the next adapter call. Host-native steering is a
future adapter capability, not an MVP promise.

### 4. Speech renderer

The renderer validates the spoken contract before TTS. Defaults are two
sentences, roughly 45 words, with a lower budget for progress updates. It removes
Markdown syntax and rejects code blocks or table-shaped content from automatic
speech. The full unmodified response remains the host's responsibility and is
never replaced by the summary.

The renderer records budget adherence and interruption rates. These metrics show
whether brevity is working without storing response content.

### 5. Repository memory

Memory is keyed by a stable repository identity derived from the canonical Git
root and normalized remote when available. Remote credentials and query strings
are removed before hashing. A repository without a remote uses a local identity
and never merges automatically with another checkout.

Stored entries are structured records with `kind`, `text`, `repo_id`,
`provenance`, `created_at`, `last_confirmed_at`, and optional `expires_at`.
Initial kinds are `decision`, `preference`, `constraint`, and `open_loop`.
Every record points to the completed turn that produced it, so users can inspect
or delete the source-derived memory.

The MVP writes memory in two cases:

1. The user explicitly says “remember that.”
2. Codex proposes a repository decision or durable working preference after a
   completed turn and the entry passes deterministic type and scope validation.

Raw ambient audio is never memory. Raw audio saving remains controlled by the
existing opt-in VoiceMode settings. Cross-repository preferences require an
explicit global scope; repository memory never leaks into another repo by
default.

### 6. Model lab and Inkling seam

The model lab replays consented evaluation cases through interchangeable
backends. An Inkling adapter may test both direct audio input and the existing
Whisper transcript, but its output re-enters the same text and speech-rendering
contracts as Codex.

The evaluation records time to first token, total latency, spoken word count,
context recall, tool-plan validity, estimated cost, and blinded user preference.
Inkling can enter the live path only after it produces a measurable win on a
specific responsibility. A broad “sounds better” impression is insufficient to
replace the Codex path.

## Data Flow

1. The local wake adapter detects “Computer” or receives the hotkey.
2. The broker acknowledges locally and records the activated utterance.
3. Existing STT provider discovery transcribes it, with current retry and
   failover behavior.
4. `voice_session_turn` returns the transcript and repository identity to Codex.
5. Codex reasons, uses tools, renders the complete answer, and creates a short
   spoken summary.
6. The next `voice_session_turn` submits that summary. Existing TTS discovery and
   streaming playback speak it while the broker watches for interruption.
7. A completed turn may emit validated repository-memory candidates.
8. The follow-up timer keeps listening locally; timeout returns to wake-only
   `asleep` state.

## Error Handling and Recovery

- If the wake engine fails, the hotkey and existing one-shot `converse` tool
  remain available.
- If the broker is unavailable, the MCP adapter fails quickly with the exact
  command needed to start it; it does not silently fall back to cloud capture.
- If Codex disconnects, the broker retains at most one activated utterance for a
  short timeout, announces locally that the agent is unavailable, then discards
  it unless transcript saving is enabled.
- If STT or TTS fails, the broker uses the existing provider registry, retries,
  and failover order. A failure never changes the microphone privacy mode.
- If memory storage fails, conversation continues stateless and reports the
  unsaved memory event visually.
- If a stale lock or crashed process owns the voice channel, existing conch
  liveness and grant-expiry behavior clears it. The broker does not add a second
  coordination lock.

## Privacy and Security

The broker socket lives in a user-only directory, rejects peers owned by other
users, bounds messages, and accepts named intents rather than arbitrary commands.
Wake audio stays local. Cloud STT receives only an activated utterance when the
user has configured a cloud provider; cloud model adapters receive only the
inputs explicitly routed to them.

The default installation saves neither ambient audio nor activated recordings.
Transcript and audio persistence remain separate, visible settings. Memory
records are inspectable, deletable, and scoped before they are written.

## Observability

Operational events use the existing event log with a shared `voice_session_id`
and explicit phases: wake, record, transcribe, agent_wait, synthesize, play,
interrupt, and sleep. Latency metrics contain durations and provider names but no
audio or transcript content. A `voicemode broker status` command reports the
state, active repo, attached Codex session, wake adapter health, pending-turn
count, and last recoverable error.

## Testing

### Unit tests

- State-machine transitions, follow-up expiry, and barge-in precedence.
- Speech-budget enforcement, Markdown removal, and complete-sentence fallback.
- Repository identity, memory isolation, expiry, and deletion.
- Socket authorization, bounded messages, and named-intent validation.
- Queue bounds and disconnect cleanup.

### Integration tests

- Broker plus fake wake, STT, Codex, and TTS adapters across complete sessions.
- Existing provider failover and control-channel behavior through the broker.
- Process restart with a stale socket, stale conch state, and a queued utterance.
- Two repositories in alternating Codex sessions with no memory crossover.

### Manual and hardware tests

- Wake-word false accepts and false rejects in quiet and noisy rooms.
- Hotkey activation, headphones, device changes, and sleep/wake recovery.
- Perceived latency, interruption responsiveness, and spoken-answer length during
  real Codex work.

## Acceptance Criteria

- No microphone audio leaves the machine before an explicit wake or hotkey
  activation.
- Wake, one initial utterance, at least three follow-up turns, explicit sleep,
  and timeout sleep work in one broker session.
- Playback can be interrupted locally and listening begins within 250 ms on the
  reference macOS machine.
- At least 95% of default spoken responses remain within two complete sentences
  and 45 words; full answers remain visible and unchanged.
- Repository memory survives Codex restart and passes an automated no-crossover
  test between two repositories.
- Broker, wake, memory, STT, or TTS failure always leaves the one-shot
  `converse` path usable.
- Inkling is absent from the runtime dependency graph and can be evaluated only
  through the model-lab interface.

## Delivery Sequence

This design decomposes into independent implementation slices:

1. **Broker core:** local daemon, protocol, state machine, status command, and
   fake-adapter integration harness.
2. **Codex loop:** MCP session tools, concise speech contract, visible full-text
   convention, and one queued interruption.
3. **Local activation:** wake-word adapter, hotkey, follow-up window, and privacy
   verification.
4. **Repository memory:** identity, structured store, explicit remember/forget,
   then validated automatic extraction.
5. **Inkling spike:** reproducible comparison harness and recommendation based on
   measured cases, without production routing.

The implementation plan should cover slice 1 first. Each later slice depends on
the stable broker protocol and receives its own acceptance tests before work
begins.
