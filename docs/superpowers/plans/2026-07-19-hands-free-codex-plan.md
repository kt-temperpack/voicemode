# Hands-Free Codex Delivery Plan

**Goal:** Turn the committed conversation broker foundation into a foreground, terminal-resident voice loop that can wake locally, drive one persistent Codex session, display complete answers, and speak concise summaries.

**Scope:** This plan delivers the Codex loop and local activation slices from the broker design. Repository memory and Inkling remain later slices because neither is required for a usable hands-free conversation.

## 1. Codex process adapter

- Add a small adapter that starts `codex exec --json` in a chosen repository and resumes the returned thread ID on later turns.
- Require a structured final response with `display_text` and `spoken_summary`, while retaining a plain-text fallback when structured output is unavailable.
- Stream Codex event text to the terminal so tool activity remains visible, then return the final display and spoken forms separately.
- Cover command construction, event parsing, session reuse, malformed output, and subprocess failure with isolated tests.

## 2. Local audio adapter

- Reuse VoiceMode's existing listen/transcribe and speak paths instead of introducing another audio stack.
- When asleep, accept only an utterance beginning with the configurable wake phrase `Computer`; once engaged, treat subsequent speech as follow-up turns until an idle timeout or explicit sleep phrase.
- Recognize `go to sleep`, `stop listening`, and `exit voice mode` locally so those controls never reach Codex.
- Keep the default voice local (`am_michael`) and preserve existing provider discovery and privacy behavior.

## 3. Foreground hands-free runtime

- Add `voicemode broker converse` as the explicit foreground loop and make `voicemode broker run` start the same loop by default; retain `--daemon-only` for the original socket-only broker.
- Start the Unix-socket broker alongside the foreground loop so status and stop continue to work.
- Print the exact user transcript and complete Codex answer, speak only the concise summary, then immediately return to listening.
- Exit cleanly on Ctrl-C, remote stop, explicit exit, audio failure, or Codex failure without leaving a stale socket.

## 4. Configuration and operator UX

- Add environment-backed settings for wake phrase, follow-up timeout, voice, Codex executable, and listen durations.
- Update broker status/help and the broker guide with the exact everyday commands and explain that the loop creates its own Codex CLI session rather than attaching to the currently open Codex UI thread.
- Add an Unreleased changelog entry.

## 5. Verification and rollout

- Run focused broker and CLI tests plus existing converse/control regression tests.
- Reinstall the editable uv tool, verify help and status outside the repository, and run a live local audio/Codex smoke test.
- Leave the final hands-free process running if microphone and local speech services are available; otherwise leave the exact start command and the concrete service blocker.

## Acceptance criteria

- `voicemode broker run` remains in a microphone loop instead of returning after server startup.
- Saying `Computer, <task>` creates a Codex thread in the current repository and produces a visible full answer plus a spoken summary.
- At least three follow-up utterances reuse the same Codex thread without repeating the wake phrase.
- The loop returns to wake-only mode after the follow-up timeout or a sleep phrase, and exits on an exit phrase or remote stop.
- The socket remains owner-only, no ambient audio or transcript is added to broker logs, and existing one-shot `converse` behavior remains intact.
