# Conversation Broker

The conversation broker is an experimental, audio-free foundation for a future
always-available voice session. This first slice provides one local process,
one active Codex session, a strict protocol, and a bounded turn slot; it does
not listen to a microphone, detect a wake word, synthesize speech, or connect to
Codex automatically yet.

## Run and inspect it

The broker runs in the foreground so process ownership and failures stay
visible:

```bash
voicemode broker run
voicemode broker status
voicemode broker status --json
voicemode broker stop
```

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

Future slices will add local activation, audio capture and playback, the Codex
adapter, repository-scoped memory, and optional model evaluation. They are not
part of this command surface yet.
