"""Persistent Codex CLI adapter for the foreground voice loop."""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .types import CanonicalResponse


class CodexTurnError(RuntimeError):
    """A user-facing failure from the Codex child process."""


@dataclass(frozen=True)
class CodexTurn:
    display_text: str
    spoken_summary: str
    thread_id: str
    request_id: str | None = None
    host_turn_id: str | None = None
    completed_at: datetime | None = None

    def canonical_response(self, request_id: str) -> CanonicalResponse:
        if self.request_id is not None and self.request_id != request_id:
            raise CodexTurnError("Codex response request identity changed")
        return CanonicalResponse(
            schema_version=1,
            request_id=request_id,
            thread_id=self.thread_id,
            display_text=self.display_text,
            spoken_text=self.spoken_summary,
            host_turn_id=self.host_turn_id or f"exec-{request_id}",
            completed_at=self.completed_at or datetime.now(timezone.utc),
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


_BROKER_DEVELOPER_INSTRUCTIONS = (
    "You are running inside the VoiceMode broker. Return exactly one response through the "
    "required output schema. Do not call VoiceMode, converse, TTS, Spokenly, or any audio "
    "tool; the parent broker exclusively owns audio playback."
)


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "response": {
            "type": "string",
            "description": "One cohesive final answer. Keep it concise and lead with the result; include technical detail only when it changes what the user does next.",
        },
    },
    "required": ["response"],
    "additionalProperties": False,
}


def _fallback_summary(text: str, word_limit: int = 45) -> str:
    clean = " ".join(text.replace("`", "").split())
    words = clean.split()
    if len(words) <= word_limit:
        return clean
    return " ".join(words[:word_limit]).rstrip(".,;:") + "…"


def _parse_thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    return None


class CodexAdapter:
    """Run one resumable Codex thread with one canonical response."""

    def __init__(
        self,
        repo_root: Path,
        *,
        executable: str = "codex",
        sandbox: str = "workspace-write",
        model: str = "gpt-5.6-terra",
        reasoning_effort: str = "low",
        runner: Runner = subprocess.run,
        event_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.executable = executable
        self.sandbox = sandbox
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.runner = runner
        self.event_sink = event_sink
        self.thread_id: str | None = None

    def _command(self, prompt: str, schema_path: Path, output_path: Path) -> list[str]:
        shared = [
            "--model",
            self.model,
            "--config",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--config",
            "mcp_servers={}",
            "--config",
            f"developer_instructions={json.dumps(_BROKER_DEVELOPER_INSTRUCTIONS)}",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if self.thread_id:
            return [self.executable, "exec", "resume", *shared, self.thread_id, prompt]
        return [
            self.executable,
            "exec",
            "--color",
            "never",
            "--sandbox",
            self.sandbox,
            "--cd",
            str(self.repo_root),
            *shared,
            prompt,
        ]

    def _emit_events(self, stdout: str) -> None:
        if self.event_sink is None:
            return
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self.event_sink(event)

    def run_turn(
        self,
        prompt: str,
        *,
        request_id: str | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> CodexTurn:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        with tempfile.TemporaryDirectory(prefix="voicemode-codex-") as temp_dir:
            temp = Path(temp_dir)
            schema_path = temp / "response-schema.json"
            output_path = temp / "last-message.json"
            schema_path.write_text(json.dumps(_RESPONSE_SCHEMA), encoding="utf-8")
            command = self._command(prompt, schema_path, output_path)
            try:
                completed = self.runner(
                    command,
                    cwd=self.repo_root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise CodexTurnError(f"Codex executable not found: {self.executable}") from exc

            self._emit_events(completed.stdout)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "Codex exited without details").strip()
                raise CodexTurnError(detail[-1000:])

            started_thread = _parse_thread_id(completed.stdout)
            if started_thread:
                self.thread_id = started_thread
            if not self.thread_id:
                raise CodexTurnError("Codex did not return a thread ID")

            try:
                raw = output_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise CodexTurnError("Codex did not write a final response") from exc

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                response = payload.get("response")
                if isinstance(response, str) and response.strip():
                    response = response.strip()
                    if on_started is not None:
                        on_started()
                    identity = request_id or str(uuid.uuid4())
                    return CodexTurn(
                        response,
                        _fallback_summary(response),
                        self.thread_id,
                        identity,
                        f"exec-{identity}",
                        datetime.now(timezone.utc),
                    )
            if not raw:
                raise CodexTurnError("Codex returned an empty final response")
            if on_started is not None:
                on_started()
            identity = request_id or str(uuid.uuid4())
            return CodexTurn(
                raw,
                _fallback_summary(raw),
                self.thread_id,
                identity,
                f"exec-{identity}",
                datetime.now(timezone.utc),
            )
