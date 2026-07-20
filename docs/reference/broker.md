# Conversation Broker

The conversation broker is a foreground hands-free loop for Codex. It listens
through VoiceMode's configured local speech service, wakes on `Computer`, runs
the request in one persistent Codex CLI thread, prints the complete answer, and
speaks a separate one- or two-sentence summary. Follow-up speech stays in the
same Codex thread until silence or an explicit sleep phrase closes the window.

## Run and inspect it

Run it from the repository you want Codex to work in. The process stays in the
foreground so the transcript, Codex activity, and failures remain visible:

```bash
voicemode broker run
voicemode broker converse
voicemode broker status
voicemode broker status --json
voicemode broker stop
```

`run` and `converse` start the same hands-free loop. Say `Computer, <request>`
to begin, continue speaking naturally for follow-ups, say `go to sleep` to
return to wake-only listening, or say `exit voice mode` to stop. The default
voice is the local `am_michael` voice. Use `--repo`, `--voice`, `--wake-phrase`,
and `--listen-duration` to override the everyday defaults.

The broker starts its own resumable `codex exec` thread in the selected
repository. It does not attach to an already-open Codex UI conversation, so the
voice transcript lives in the broker terminal while file changes land in the
same working tree. Codex starts with the `workspace-write` sandbox by default;
change `VOICEMODE_BROKER_CODEX_SANDBOX` only when the repository needs a
different policy.

Use `voicemode broker run --daemon-only` when developing against the socket
protocol without microphone, speech, or Codex integration.

Use `--socket PATH` on any command to override the default
`~/.voicemode/broker.sock`. `status` exits 1 when no broker is running; its JSON
form prints `{"running": false}`. `stop` is idempotent and exits successfully
when the broker is already stopped.

The socket directory is owner-only (`0700`), the socket is `0600`, peers are
checked against the broker's operating-system user where the platform exposes
credentials, and every message has byte and time limits. Startup replaces a
stale socket owned by the current user, but refuses to remove regular files,
symlinks, or another user's socket.

## Privacy and recovery

The broker's status and lifecycle events contain phase, timing, queue depth,
session identifiers, and repository path. It never writes audio, utterance
text, or spoken summaries. Text injection exists only as an in-process testing
adapter; there is no production socket operation for it.

If startup reports a busy socket, use `voicemode broker status` first and stop
the live owner normally. If the socket is stale, a new broker replaces it after
verifying its type and owner. Permission errors mean the socket directory or
path is not safely owned by the current user; correct that ownership or choose
an owner-controlled path with `--socket`.

Configuration is environment-backed: `VOICEMODE_BROKER_WAKE_PHRASE`,
`VOICEMODE_BROKER_VOICE`, `VOICEMODE_BROKER_LISTEN_DURATION_SECONDS`,
`VOICEMODE_BROKER_MIN_LISTEN_DURATION_SECONDS`,
`VOICEMODE_BROKER_CODEX_EXECUTABLE`, and `VOICEMODE_BROKER_CODEX_SANDBOX`.
Repository-scoped long-term memory and optional wake-model evaluation remain
later slices; normal Codex session context already persists across follow-ups.
