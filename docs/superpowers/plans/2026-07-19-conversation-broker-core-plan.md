# Conversation Broker Core Implementation Plan

**Status:** Ready for implementation

**Date:** 2026-07-19

**Design source:**
`docs/superpowers/specs/2026-07-19-codex-conversation-broker-design.md`

**Scope:** Delivery slice 1 only: broker process, protocol, state machine, CLI
status, and a fake-adapter integration harness. This plan deliberately excludes
wake-word engines, real microphone ownership, Codex MCP tools, speech rendering,
repository memory, and Inkling evaluation.

## Outcome

After this plan is implemented, VoiceMode has a secure local broker process that
can be run in the foreground, accepts one active logical voice session over a
versioned Unix-domain-socket protocol, long-polls for a bounded pending
utterance, exposes machine-readable status, and shuts down cleanly. All behavior
is proven with socket-level integration tests and fake semantic events; no audio
device or model provider is involved yet.

The broker core is successful when later slices can add activation, Codex, STT,
and TTS adapters without changing the state machine or wire envelope. Slice 1
must not modify the production `converse` path.

## Constraints and Rationale

### Preserve the existing voice path

`voice_mode/tools/converse.py` is large and behavior-rich, with current conch,
control-channel, streaming, retry, and logging semantics. Broker slice 1 adds a
parallel package and CLI surface rather than refactoring `converse`; this keeps
the new work reversible and makes the existing one-shot conversation path the
fallback required by the design.

### Use a synchronous threaded Unix socket for the first transport

`voice_mode/control_socket.py` already proves that a synchronous Unix socket,
bounded reads, peer-UID checks, short handler threads, and explicit teardown work
on the project's macOS and Linux targets. The broker needs long-polling, but it
does not need an event loop in slice 1. A `Condition`-backed runtime plus one
bounded handler thread per client is simpler to test and avoids coupling broker
lifecycle to FastMCP's asyncio loop.

The public client and runtime interfaces remain transport-neutral. Windows named
pipes or a loopback transport can implement the same protocol later without
changing state or session behavior.

### Keep control and broker sockets separate

The control socket drives an in-flight utterance, while the broker socket owns a
conversation session. Combining them would make control availability depend on
broker lifecycle and would force a migration of a security-sensitive surface.
Slice 1 shares conventions, not authority or socket paths.

### Admit one active logical session

The broker owns one physical microphone and speaker pair, so slice 1 rejects a
second `open` while a live session exists. Multi-agent fairness remains the
conch's responsibility at the later Codex adapter boundary. Adding a second
session queue now would duplicate the conch and create two ordering authorities.

### Bound every wait and queue

MCP cannot accept an unsolicited push from the broker. The later Codex adapter
will long-poll, so the core returns `idle` after a bounded timeout and allows the
caller to reissue. The runtime stores at most one pending utterance. A second
utterance while the slot is full returns an explicit `queue_full` outcome rather
than silently replacing user speech or growing without bound.

## Existing Code Map

| Concern | Existing seam | How slice 1 uses it |
|---|---|---|
| CLI root | `voice_mode/cli.py` imports groups from `voice_mode/cli_commands/` near the bottom | Register one new `broker` Click group; keep implementation out of the monolithic file. |
| Socket security | `voice_mode/control_socket.py` | Mirror peer-UID checks, owner-only directories, bounded messages, safe stale-socket unlinking, and idempotent teardown. Do not change its wire protocol. |
| Configuration | `voice_mode/config.py` | Add broker socket and limit settings alongside control-channel settings, using existing `env_bool`, `expand_path`, and base directories. |
| Event logs | `voice_mode/utils/event_logger.py` | Emit content-free broker lifecycle and phase events through `get_event_logger()` when initialized. |
| Conch | `voice_mode/conch.py`, `conch_queue.py`, `conch_ops.py` | No broker lock is added. Slice 1 status reports no conch data; integration happens in the Codex slice. |
| MCP tool loading | `voice_mode/tools/__init__.py`, `voice_mode/server.py` | Untouched in slice 1. Broker MCP tools belong to slice 2. |
| CLI testing | `tests/test_conch_cli.py`, `tests/test_control_cli.py` | Follow `CliRunner` patterns and isolate filesystem paths with monkeypatching. |
| Socket testing | `tests/test_control_socket.py` | Reuse short temporary socket-path fixtures and real client/server integration style. |

## Target Package Layout

```text
voice_mode/
  broker/
    __init__.py          # stable exports only
    types.py             # enums, immutable records, errors
    state.py             # deterministic state machine
    protocol.py          # v1 envelope parsing and response encoding
    runtime.py           # single-session authority and pending-turn condition
    server.py            # secure local socket listener and lifecycle
    client.py            # transport-neutral caller used by CLI and later MCP
  cli_commands/
    broker.py            # run/status/stop commands
tests/
  test_broker_state.py
  test_broker_protocol.py
  test_broker_runtime.py
  test_broker_socket.py
  test_broker_cli.py
docs/reference/
  broker.md
```

`voice_mode/broker/` must not import `voice_mode/tools/converse.py`, audio
libraries, FastMCP, provider clients, or model SDKs. That import boundary keeps
the core deterministic and cheap to start.

## Protocol v1

### Framing

Each connection carries exactly one UTF-8 JSON request terminated by a newline
and receives exactly one UTF-8 JSON response terminated by a newline. One
request per connection keeps long-poll cancellation simple: closing the client
socket cancels only that request. Persistent multiplexing is deferred until
evidence shows connection setup matters.

The server reads at most `BROKER_MAX_MESSAGE_BYTES` plus one byte. It rejects an
oversized request before JSON parsing. Read, long-poll, and write phases each
have explicit timeouts.

### Request envelope

```json
{
  "version": 1,
  "request_id": "caller-generated opaque string",
  "operation": "status|open|turn|close|stop",
  "payload": {}
}
```

Rules:

- `version` must equal `1` exactly; booleans do not count as integers.
- `request_id` is required, must be 1-128 printable characters, and is echoed.
- `operation` must be one of the fixed names above.
- `payload` must be an object; unknown top-level fields are rejected.
- Each operation validates and rejects unknown payload fields, which catches
  client/server drift instead of ignoring a misspelled safety option.

### Response envelope

Success:

```json
{
  "version": 1,
  "request_id": "same value",
  "ok": true,
  "result": {"kind": "status", "...": "operation-specific fields"}
}
```

Failure:

```json
{
  "version": 1,
  "request_id": "same value when recoverable",
  "ok": false,
  "error": {
    "code": "invalid_request",
    "message": "human-readable bounded detail",
    "retryable": false
  }
}
```

Error codes are a closed enum in slice 1:

- `invalid_json`
- `invalid_request`
- `unsupported_version`
- `unknown_operation`
- `session_busy`
- `session_not_found`
- `session_mismatch`
- `queue_full`
- `timeout`
- `internal_error`
- `shutting_down`

Tracebacks, local paths other than the configured socket, environment values,
and transcript contents never enter an error response.

### Operations

#### `status`

Payload is empty. Result fields:

```json
{
  "kind": "status",
  "state": "asleep|engaged|listening|thinking|speaking",
  "session": null,
  "pending_turns": 0,
  "uptime_seconds": 12.4,
  "protocol_version": 1,
  "shutting_down": false
}
```

When a session exists, `session` exposes only `session_id`, a redacted
`codex_session_id` prefix, canonical `repo_root`, and age. It never includes a
transcript or spoken response.

#### `open`

Payload requires `codex_session_id` and an absolute `repo_root`. The runtime
canonicalizes the path without requiring it to be a Git repository yet. It
creates a random broker `session_id`, moves `asleep -> engaged`, and returns a
session record plus broker capabilities.

Calling `open` again with the same Codex ID and canonical repo is idempotent and
returns the existing session. Any other open request receives `session_busy`.
This supports client retry without permitting session takeover.

#### `turn`

Payload requires `session_id`, accepts `spoken_summary` as a bounded string, and
accepts `wait_seconds` from zero through the configured maximum. Slice 1 stores
only summary metadata such as length; it does not synthesize or log the content.

If a pending utterance exists, the result is:

```json
{
  "kind": "utterance",
  "utterance_id": "uuid",
  "text": "fake-adapter transcript",
  "captured_at": "UTC ISO-8601",
  "repo_root": "/canonical/path"
}
```

If none arrives before the wait expires, the result is `{"kind":"idle"}`.
The timeout is a successful protocol outcome because idle is normal and callers
should reissue while voice mode remains active.

The fake integration adapter invokes the runtime directly to enqueue text. No
network `inject` operation ships in production because exposing transcript
injection over the broker socket would create a second unauthenticated input
surface even with peer-UID checks.

#### `close`

Payload requires `session_id`. It clears the pending slot, returns the runtime to
`asleep`, wakes blocked turn calls with `session_not_found`, and is idempotent for
the most recently closed session within the process lifetime.

#### `stop`

Payload is empty. It is accepted only from the same OS user, sets the server's
shutdown event, wakes long-polls with `shutting_down`, and lets the foreground
process perform normal socket cleanup. It does not kill a PID or unlink a socket
from the client side.

## State Model

### Public states

- `ASLEEP`: no logical session or a closed session.
- `ENGAGED`: a session exists and can accept semantic adapter events.
- `LISTENING`: an activation adapter is capturing an utterance.
- `THINKING`: an utterance was delivered and the agent has not submitted its
  next summary.
- `SPEAKING`: a summary was accepted for future playback.

Slice 1 has no real audio, so fake adapter events drive `LISTENING` and
`SPEAKING`. The state machine still ships now because later adapters must not
invent their own transition rules.

### Events

- `OPEN`
- `ACTIVATE`
- `LISTEN_STARTED`
- `UTTERANCE_ENQUEUED`
- `UTTERANCE_DELIVERED`
- `SUMMARY_ACCEPTED`
- `PLAYBACK_FINISHED`
- `BARGE_IN`
- `FOLLOWUP_EXPIRED`
- `CLOSE`
- `FAULT`
- `RESET`

`transition(current, event)` is a pure function. Invalid pairs raise a typed
`InvalidTransition` containing the state and event names but no user content.
`BrokerRuntime` owns locks, timestamps, queue mutation, and event logging; the
state module owns only legal transitions.

### Required transition table

| Current | Event | Next | Notes |
|---|---|---|---|
| asleep | open | engaged | Creates the only active session. |
| engaged | activate | listening | Future wake/hotkey seam. |
| listening | utterance_enqueued | thinking | Pending slot becomes occupied. |
| thinking | utterance_delivered | thinking | Delivery alone does not imply a response. |
| thinking | summary_accepted | speaking | Slice 1 stores metadata only. |
| speaking | playback_finished | engaged | Follow-up-capable state. |
| speaking | barge_in | listening | Interruption takes precedence. |
| engaged | followup_expired | asleep | Runtime closes logical session for slice 1. |
| any non-asleep | close | asleep | Clears pending data and wakes waiters. |
| any | fault | asleep | Fail closed; detail goes to event log. |
| asleep | reset | asleep | Idempotent recovery. |

Tests must enumerate every state/event pair, proving allowed transitions and
explicit rejection of all others.

## Dependency Graph

```text
B0 Characterization gate
 ├─> B1 Types and state machine
 └─> B2 Configuration and protocol codec

B1 + B2 ─> B3 Runtime/session authority
B2 ──────> B4 Secure socket server
B2 + B4 ─> B5 Client
B3 + B4 + B5 ─> B6 End-to-end broker server
B5 + B6 ─> B7 CLI surface
B3 + B6 ─> B8 Observability and status contract
B6 + B7 + B8 ─> B9 Failure and concurrency hardening
B9 ─> B10 Documentation, changelog, and final verification
```

No task after B0 may start if the characterization suite fails before broker
changes. B1 and B2 can proceed independently. B4 can build transport mechanics
against a stub dispatcher, but B6 is where the real runtime becomes reachable.

## Task B0: Establish the Characterization Gate

**Purpose:** Prove the existing control socket, conch, CLI loading, and one-shot
converse surfaces are green before adding another long-lived process.

**Files changed:** None.

**Steps:**

1. Record the current branch, commit, and dirty paths. Preserve `CLAUDE.md` and
   any other unrelated user changes.
2. Run the focused non-audio suites:

   ```bash
   uv run pytest -q \
     tests/test_control_socket.py \
     tests/test_control_cli.py \
     tests/test_conch_cli.py \
     tests/test_converse_conch_queue.py
   ```

3. Run CLI help and confirm the existing commands load without warnings:

   ```bash
   uv run voicemode --help
   uv run voicemode conch status --json
   ```

4. If an existing test fails, record it with the exact command and stop. Do not
   fold unrelated repairs into broker work.

**Completion evidence:** Exact commands and pass counts in the implementation
commit or task log.

**Dependencies:** None.

**Commit:** None.

## Task B1: Add Broker Types and the Pure State Machine

**Purpose:** Lock the vocabulary and legal lifecycle before introducing threads,
sockets, or clocks.

**Files:**

- Create `voice_mode/broker/__init__.py`.
- Create `voice_mode/broker/types.py`.
- Create `voice_mode/broker/state.py`.
- Create `tests/test_broker_state.py`.

**Steps:**

1. Define string enums `BrokerPhase`, `BrokerEvent`, `ResultKind`, and
   `BrokerErrorCode` with the exact protocol values in this plan.
2. Define frozen dataclasses for `SessionInfo`, `PendingUtterance`,
   `BrokerSnapshot`, and `BrokerCapabilities`. Keep serialization out of these
   records; `protocol.py` owns JSON shape.
3. Define typed exceptions `BrokerError` and `InvalidTransition`. Every
   `BrokerError` carries a closed error code, bounded public message, and
   retryable flag.
4. Implement `transition(phase, event) -> phase` from the required table. Use a
   static mapping so tests can exhaust it.
5. Export only stable types and `transition` from `broker/__init__.py`.
6. Add parameterized tests for every allowed transition and the Cartesian set
   of rejected pairs. Verify string values match the protocol specification.
7. Test dataclass immutability and ensure exception `str()` contains no object
   reprs that could later carry transcript text.

**Validation:**

```bash
uv run pytest -q tests/test_broker_state.py
```

**Dependencies:** B0.

**Commit:** `feat(broker): define session state model`

## Task B2: Add Configuration and the Protocol Codec

**Purpose:** Validate all untrusted wire input before any runtime method sees it.

**Files:**

- Modify `voice_mode/config.py`.
- Create `voice_mode/broker/protocol.py`.
- Create `tests/test_broker_protocol.py`.

**Configuration additions:**

- `BROKER_SOCKET_PATH`, default `~/.voicemode/broker.sock`.
- `BROKER_MAX_MESSAGE_BYTES`, default 65,536, clamped to 4,096-1,048,576.
- `BROKER_READ_TIMEOUT_SECONDS`, default 2.0, clamped to 0.1-30.
- `BROKER_WRITE_TIMEOUT_SECONDS`, default 2.0, clamped to 0.1-30.
- `BROKER_LONG_POLL_MAX_SECONDS`, default 120, clamped to 1-300.

The pending-turn limit is a runtime constant fixed at one in slice 1, not an
environment setting. Exposing a knob before the runtime supports larger queues
would create a configuration contract that the implementation cannot honor.

Follow existing environment parsing conventions. If numeric parsing helpers are
missing, add a small private helper beside comparable config code with unit
coverage in the protocol/config test file; do not introduce a new dependency.

**Codec steps:**

1. Define request dataclasses for `status`, `open`, `turn`, `close`, and `stop`.
2. Implement `decode_request(raw: bytes, limits)`. Validate UTF-8, JSON object
   shape, exact fields, types, bounds, operation names, and per-operation
   payloads in that order.
3. Reject Python boolean values where integer or float fields are required.
4. Canonicalize `repo_root` with `Path.resolve(strict=False)` after verifying it
   is absolute. Do not require the directory to exist in the codec; runtime can
   report environment-specific concerns later.
5. Normalize `wait_seconds` to a float no greater than configured max.
6. Bound `spoken_summary` by UTF-8 bytes as well as characters. Slice 1 permits
   an empty summary and caps a non-empty summary at 4,000 characters.
7. Implement success and failure encoders. Ensure `json.dumps` cannot serialize
   arbitrary exception objects.
8. Add table-driven tests for valid operations, every missing field, every
   unknown field, wrong types, booleans-as-numbers, invalid UTF-8, excessive
   nesting, over-limit strings, unsupported versions, and exact error codes.
9. Add round-trip golden tests for each response kind. Golden values live in the
   test file because the small protocol should be readable without fixture
   indirection.

**Validation:**

```bash
uv run pytest -q tests/test_broker_protocol.py
```

**Dependencies:** B0.

**Commit:** `feat(broker): define validated protocol v1`

## Task B3: Implement the Single-Session Runtime

**Purpose:** Create the sole authority for sessions, phase transitions, and the
one-slot pending utterance queue without transport concerns.

**Files:**

- Create `voice_mode/broker/runtime.py`.
- Create `tests/test_broker_runtime.py`.

**Steps:**

1. Implement `BrokerRuntime` with an injected monotonic clock, UTC clock, UUID
   factory, and optional event sink. Tests must not sleep or patch global time.
2. Protect all mutable state with one `threading.Condition`. The condition owns
   active-session mutation, pending utterance mutation, shutdown state, and
   long-poll wakeups. Do not layer a second lock around the same fields.
3. Implement `open_session(codex_session_id, repo_root)`. Preserve idempotency
   for the exact same logical caller and return `session_busy` otherwise.
4. Implement the semantic fake-adapter methods `activate`, `start_listening`,
   `enqueue_utterance`, `accept_summary`, `finish_playback`, `barge_in`, and
   `expire_followup`. Each method applies one legal state event and emits one
   content-free event record after mutation.
5. Implement `wait_for_turn(session_id, wait_seconds, cancel_event=None)`.
   Consume a pending utterance exactly once. Return idle on deadline. Wake and
   return typed errors on close or shutdown.
6. Implement `close_session` and `begin_shutdown`. Both notify all waiters.
   `close_session` is idempotent for the most recently closed ID and rejects a
   different ID.
7. Implement `snapshot()` by copying immutable records under the condition and
   calculating ages after release where safe. Never expose the pending text.
8. Test concurrent waiters even though the public client supports one active
   session: exactly one may consume the utterance, and the other must idle or
   receive session closure. This proves no duplicate delivery.
9. Test queue-full behavior, close while waiting, shutdown while waiting,
   idempotent open/close, busy open, every fake adapter transition, and snapshot
   redaction.

**Validation:**

```bash
uv run pytest -q tests/test_broker_runtime.py
```

**Dependencies:** B1, B2.

**Commit:** `feat(broker): implement single-session runtime`

## Task B4: Build the Secure Local Socket Server

**Purpose:** Expose protocol requests to a bounded local server without making
the transport the runtime authority.

**Files:**

- Create `voice_mode/broker/server.py`.
- Create the transport-focused part of `tests/test_broker_socket.py`.

**Steps:**

1. Implement `BrokerServer(socket_path, dispatcher, limits)` with `start`,
   `serve_forever`, `stop`, `is_running`, and `socket_path` following the
   lifecycle shape of `ControlSocketListener`.
2. Create the parent directory with mode `0700`; bind under umask `077`; chmod
   the socket to `0600` as defense in depth.
3. Before unlinking an existing path, `lstat` it and require a socket owned by
   the current UID. Refuse regular files, symlinks, foreign-owned sockets, and
   paths whose parent is not owned by the current user.
4. On Linux use `SO_PEERCRED`; on macOS/BSD use `getpeereid` when available.
   Reject a peer UID different from the server UID before reading a request.
   Encapsulate this check so Windows can replace it later.
5. Bound the accept loop and handler population. Use a semaphore with a default
   maximum of eight active handlers; excess connections receive a short
   retryable error or close without reading if authentication is unavailable.
6. Read until newline, EOF, timeout, or byte limit. One request per connection;
   ignore trailing data only by rejecting it as invalid input.
7. Pass decoded requests to an injected dispatcher and encode only typed
   responses. Catch unexpected exceptions at the connection boundary, log the
   traceback locally, and return bounded `internal_error`.
8. Make `stop` idempotent. Close the listening socket to unblock `accept`, wake
   dispatcher long-polls through the runtime shutdown seam in B6, join handler
   threads with a total deadline, and safely unlink the socket.
9. Test bind/stop/restart, stale socket replacement, refusal to unlink a file or
   symlink, socket modes, malformed and oversized messages, read timeout,
   handler cap, unexpected dispatcher errors, and a good request after each bad
   request.

**Validation:**

```bash
uv run pytest -q tests/test_broker_socket.py -k 'server or transport'
```

**Dependencies:** B2.

**Commit:** `feat(broker): add secure local server`

## Task B5: Build the Broker Client

**Purpose:** Give the CLI and future MCP adapter one typed client rather than
letting each surface construct JSON or sockets independently.

**Files:**

- Create `voice_mode/broker/client.py`.
- Extend `tests/test_broker_socket.py`.

**Steps:**

1. Implement `BrokerClient(socket_path, connect_timeout, read_timeout)` with
   `status`, `open`, `turn`, `close`, and `stop` methods.
2. Generate request IDs with an injected factory in tests. Validate the response
   version, request ID, envelope exclusivity, result kind, and operation-specific
   fields before returning typed records.
3. Map missing socket, connection refused, timeouts, malformed responses, and
   broker error envelopes to typed client exceptions. Preserve retryability but
   do not expose raw socket exceptions to CLI callers.
4. Set the turn read timeout to `wait_seconds + write/read grace`, capped by the
   configured maximum. Other calls use the short request timeout.
5. Ensure client cancellation closes the socket and does not send a second stop
   request.
6. Test against both a small fake server for malformed responses and the real
   `BrokerServer` for valid round trips.

**Validation:**

```bash
uv run pytest -q tests/test_broker_socket.py -k 'client or round_trip'
```

**Dependencies:** B2, B4.

**Commit:** `feat(broker): add typed local client`

## Task B6: Wire Runtime, Dispatcher, and Server End to End

**Purpose:** Turn the tested pieces into a broker process without contaminating
the transport with business rules.

**Files:**

- Extend `voice_mode/broker/server.py` or create
  `voice_mode/broker/application.py` if wiring exceeds roughly 150 lines.
- Extend `tests/test_broker_socket.py`.

**Steps:**

1. Implement a dispatcher that maps typed protocol requests to
   `BrokerRuntime`. It owns no mutable session state.
2. Map runtime errors to closed protocol error codes in one function. Unknown
   runtime exceptions become `internal_error`; they are never stringified onto
   the wire.
3. Implement operation semantics exactly as Protocol v1 specifies, including
   idempotent open and close, idle as success, and shutdown wakeups. On the
   first `turn` after `open`, an empty `spoken_summary` means “wait for the
   initial utterance” and does not emit `SUMMARY_ACCEPTED`. On later turns in
   `thinking`, a non-empty summary advances through the fake speaking lifecycle
   before the dispatcher waits again; tests drive `PLAYBACK_FINISHED` through
   the injected fake adapter.
4. Provide a process entry function `run_broker(socket_path=None)` that
   initializes logging, creates runtime/dispatcher/server, installs SIGINT and
   SIGTERM handlers on the main thread, and blocks in `serve_forever`.
5. Keep fake adapter access as a runtime Python API only. Tests enqueue
   utterances through the runtime instance retained by the fixture.
6. Add end-to-end tests that open via the real client, drive fake adapter events,
   long-poll a turn, receive the utterance exactly once, submit a summary, inspect
   status, close, and stop the process.
7. Add a subprocess smoke test using a short temporary socket path. Start the
   real broker entry function, wait for status readiness without fixed sleeps,
   request stop, and assert zero exit plus socket cleanup.

**Validation:**

```bash
uv run pytest -q tests/test_broker_socket.py
```

**Dependencies:** B3, B4, B5.

**Commit:** `feat(broker): wire broker process`

## Task B7: Add the CLI Surface

**Purpose:** Make the broker observable and operable without exposing internal
Python APIs.

**Files:**

- Create `voice_mode/cli_commands/broker.py`.
- Modify `voice_mode/cli.py` only to import and register the group.
- Create `tests/test_broker_cli.py`.

**Commands:**

```text
voicemode broker run [--socket PATH]
voicemode broker status [--json] [--socket PATH]
voicemode broker stop [--socket PATH]
```

`run` stays in the foreground and is the only lifecycle command in slice 1.
Service installation and login startup belong to the activation slice, where
platform-specific deployment can be tested with the wake engine.

**Steps:**

1. Follow the `cli_commands/conch.py` Click group pattern and standard `-h` help
   option.
2. `run` calls the process entry function and reports bind errors as
   `ClickException` with a recovery action. It never daemonizes itself.
3. `status --json` emits the broker response unchanged except stable JSON
   formatting. Human status shows running state, phase, session prefix, repo,
   pending count, protocol version, and uptime.
4. A missing socket makes human `status` print “Broker is not running” and exit
   1; JSON status emits a stable object with `running:false` and exits 1.
5. `stop` sends the graceful stop operation. A missing broker is idempotent:
   print that it is already stopped and exit 0.
6. Add `CliRunner` tests for help, registration at the root, human and JSON
   status, live status, stop, run bind failure, alternate socket path, and exit
   codes.

**Validation:**

```bash
uv run pytest -q tests/test_broker_cli.py
uv run voicemode broker --help
```

**Dependencies:** B5, B6.

**Commit:** `feat(broker): expose lifecycle CLI`

## Task B8: Add Content-Free Observability

**Purpose:** Make broker lifecycle and phase failures diagnosable without
creating a transcript side channel.

**Files:**

- Modify `voice_mode/utils/event_logger.py` only if named constants materially
  improve consistency; otherwise use explicit event strings as existing conch
  code does.
- Extend `voice_mode/broker/runtime.py` and broker wiring.
- Extend `tests/test_broker_runtime.py` and `tests/test_broker_socket.py`.

**Events:**

- `BROKER_START`
- `BROKER_STOP`
- `BROKER_SESSION_OPEN`
- `BROKER_SESSION_CLOSE`
- `BROKER_PHASE_CHANGE`
- `BROKER_TURN_ENQUEUED`
- `BROKER_TURN_DELIVERED`
- `BROKER_TURN_IDLE`
- `BROKER_QUEUE_FULL`
- `BROKER_PROTOCOL_ERROR`
- `BROKER_INTERNAL_ERROR`

Allowed event data is limited to broker session ID, redacted Codex session
prefix, repository path or a future hash, old/new phase, duration, queue count,
protocol operation, error code, and retryability. `text`, `spoken_summary`, raw
request bytes, and exception messages from validation are prohibited.

**Steps:**

1. Inject an event sink into `BrokerRuntime` so state tests capture structured
   events without initializing global logging.
2. Add a server event helper for protocol and lifecycle events.
3. Initialize the existing event logger in the foreground process using the same
   config and path behavior as `server.py`.
4. Test each event shape against an allowlist of keys. Add an explicit regression
   test with recognizable secret text and assert it never occurs in serialized
   event payloads or status output.
5. Test phase-duration calculation with injected clocks.

**Validation:**

```bash
uv run pytest -q tests/test_broker_runtime.py tests/test_broker_socket.py -k event
```

**Dependencies:** B3, B6.

**Commit:** `feat(broker): add privacy-safe lifecycle events`

## Task B9: Harden Failure and Concurrency Behavior

**Purpose:** Prove that process failure, cancellation, malformed clients, and
races cannot wedge the broker or lose ownership cleanup.

**Files:**

- Extend all broker test files.
- Modify broker modules only where a failing test proves a gap.

**Required tests:**

1. Client disconnects during long-poll; handler exits and later turns still work.
2. Close races with utterance enqueue; either the utterance is delivered once or
   close wins and it is discarded, never both.
3. Stop races with open; stop wins after the shutdown flag and no new session is
   observable.
4. Two clients open different sessions simultaneously; one succeeds and one gets
   `session_busy`.
5. Two turn clients wait on the same session; one consumes a single utterance,
   the other returns idle or closure.
6. Handler limit saturation does not block status after slots free.
7. A stale socket from a killed subprocess is safely replaced on restart.
8. A regular file or symlink at the socket path survives unchanged and startup
   fails.
9. SIGTERM wakes long-polls, returns or closes them cleanly, unlinks the socket,
   and exits within five seconds.
10. A malformed response from a fake server cannot trick the client into
    returning an unvalidated record.
11. 100 sequential open/close cycles leave no threads or file descriptors above
    a small measured baseline tolerance.
12. The existing control socket and conch tests still pass unchanged.

Avoid timing-flaky tests. Use events, barriers, injected clocks, and polling with
deadlines. Fixed sleeps may appear only as sub-50-ms scheduler yields inside a
bounded helper.

**Validation:**

```bash
uv run pytest -q \
  tests/test_broker_state.py \
  tests/test_broker_protocol.py \
  tests/test_broker_runtime.py \
  tests/test_broker_socket.py \
  tests/test_broker_cli.py

uv run pytest -q \
  tests/test_control_socket.py \
  tests/test_control_cli.py \
  tests/test_conch_cli.py \
  tests/test_converse_conch_queue.py
```

**Dependencies:** B6, B7, B8.

**Commit:** `test(broker): harden lifecycle and concurrency`

## Task B10: Document and Certify Slice 1

**Purpose:** Make the experimental core understandable and keep release notes
accurate without claiming wake-word or Codex support that does not exist yet.

**Files:**

- Create `docs/reference/broker.md`.
- Modify `docs/reference/cli.md`.
- Modify `docs/reference/environment.md`.
- Modify `CHANGELOG.md` under `Unreleased`.
- Modify the broker design only if implementation discovered an approved
  contract correction; never silently drift the code from the design.

**Documentation content:**

- State clearly that slice 1 is an experimental, audio-free broker core.
- Document `run`, `status`, `stop`, socket path, timeouts, and JSON status.
- Explain that `run` is foreground-only and login service installation is not
  shipped yet.
- Document privacy: no audio, transcript, or spoken summary is written by the
  broker core; fake utterance injection is test-only.
- Document recovery for missing, busy, stale, and permission-blocked sockets.
- List the future slices without promising release dates.

**Final verification:**

```bash
uv run pytest -q \
  tests/test_broker_state.py \
  tests/test_broker_protocol.py \
  tests/test_broker_runtime.py \
  tests/test_broker_socket.py \
  tests/test_broker_cli.py \
  tests/test_control_socket.py \
  tests/test_control_cli.py \
  tests/test_conch_cli.py \
  tests/test_converse_conch_queue.py

uv run voicemode broker --help
uv run voicemode broker status --json
make docs-check
git diff --check
```

The expected standalone `broker status --json` exit code is 1 when no broker is
running; the verification wrapper must assert the payload and expected exit
rather than treating that command as a generic success gate.

**Dependencies:** B9.

**Commit:** `docs: document experimental conversation broker`

## Implementation Boundaries

### Files slice 1 may modify

- `voice_mode/config.py`
- `voice_mode/cli.py` for one command registration
- `voice_mode/utils/event_logger.py` only for event constants if justified
- New `voice_mode/broker/**`
- New `voice_mode/cli_commands/broker.py`
- New broker tests
- Broker reference docs, CLI/environment references, and `CHANGELOG.md`

### Files slice 1 must not modify

- `voice_mode/tools/converse.py`
- `voice_mode/streaming.py`
- `voice_mode/core.py`
- `voice_mode/control_channel.py`
- `voice_mode/control_socket.py`, except a separately reviewed follow-up if a
  security helper extraction becomes unavoidable
- `voice_mode/conch.py`, `conch_queue.py`, or `conch_ops.py`
- Provider selection or STT/TTS service code
- Plugin prompts, hooks, or MCP tool schemas

This boundary prevents a broker-core change from altering existing speech or
multi-agent behavior.

## Verification Matrix

| Risk | Proof |
|---|---|
| Invalid state transition | Exhaustive state/event matrix unit test. |
| Duplicate utterance delivery | Two-waiter concurrency test with one pending item. |
| Unbounded memory | One-slot queue and oversized-message rejection. |
| Local user impersonation | Peer-UID integration test where platform APIs permit; unit seam elsewhere. |
| Destructive stale cleanup | Regular-file, symlink, foreign-owner, and stale-socket tests. |
| Wedge on client cancel | Disconnect-during-long-poll integration test. |
| Wedge on shutdown | SIGTERM subprocess test with bounded exit. |
| Transcript leakage | Sentinel secret absent from status and event serialization. |
| CLI/code drift | CLI calls `BrokerClient`; no duplicate JSON construction. |
| Existing behavior regression | Focused control/conch/converse suite after broker suite. |
| Cross-platform lock-in | Transport-neutral runtime/client types; platform guard around AF_UNIX tests. |

## Rollback Strategy

Slice 1 is additive and unreferenced by the MCP server. If it causes packaging or
CLI regressions, remove the `broker` command registration and new package; the
existing `converse` path remains intact. Configuration keys are inert unless the
broker command runs, and no persistent data migration exists.

Do not add automatic startup in slice 1. This keeps rollback equivalent to
stopping a foreground process and reverting additive commits.

## Review Rounds

### Round 1: Architecture and scope

The initial plan attempted to combine broker core, Codex MCP tools, wake-word
activation, and memory. Review split delivery at the stable protocol boundary.
Slice 1 now contains no audio or model dependency, so it can be tested
deterministically and merged without changing user conversation behavior.

### Round 2: Protocol and security

Review found that a generic network `inject` operation would create an
unnecessary transcript-injection surface. The plan now keeps fake utterance
injection as an in-process test adapter, uses exact-field validation, bounds each
phase, authenticates local peers, and refuses destructive stale-path cleanup.

### Round 3: Concurrency and recovery

Review found ambiguous ownership between session state, socket handlers, and
shutdown. The runtime is now the single authority protected by one condition;
the server owns transport only. Close and shutdown wake all long-polls, session
open is idempotent only for the same caller, and the queue is explicitly one
item.

### Round 4: Implementability and dependency graph

Review traced the least obvious task—B9 concurrency hardening—from prerequisites
through exact fixtures and commands. The graph is acyclic, every task has a
consumer, exact files and commit boundaries are named, and the final gate
re-runs existing control/conch behavior. No structural revisions remained after
this pass.

## Tracker Conversion Decision

The planning workflow normally converts the dependency graph into Beads. This
repository has no `.beads`, `.br`, or issue JSONL metadata, and its project
instructions do not establish Beads as an authority. Introducing a tracker would
be a separate repository-level decision, so this plan preserves the complete DAG
and task IDs in Markdown. If the maintainer later initializes Beads, create one
epic for slice 1, one bead for B0-B10, and copy the dependency edges exactly from
the graph above.

## Implementation Handoff

Start with B0 and stop immediately on a pre-existing focused-test failure. Then
implement B1 and B2 independently, using explicit-path commits and preserving the
current `CLAUDE.md` modification. Do not start wake-word, Codex adapter, memory,
or Inkling work under this plan; those begin only after B10 certifies the broker
protocol and lifecycle.
