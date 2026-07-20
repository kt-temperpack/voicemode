"""Deterministic, privacy-safe broker diagnostic projections."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
COMMANDS = (
    "voicemode broker capabilities --json",
    "voicemode broker status --json",
    "voicemode restart",
    "voicemode start",
    "voicemode stop",
)
ENVIRONMENT_KEYS = (
    "VOICEMODE_BROKER_CODEX_ADAPTER",
    "VOICEMODE_BROKER_HOTKEY",
    "VOICEMODE_BROKER_OUTPUT_MODE",
    "VOICEMODE_BROKER_TERMINAL_KEYS",
    "VOICEMODE_BROKER_WAKE_PHRASE",
)
EXIT_CODES = {
    "conflict": 6,
    "environment_failure": 4,
    "safety_refusal": 3,
    "success": 0,
    "upstream_failure": 5,
    "user_input": 2,
}


def _generated_at() -> str | None:
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        return None
    try:
        stamp = int(value)
    except ValueError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_label(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return Path(path).name or None


def capabilities_document(capabilities: Mapping[str, Any] | None = None) -> dict[str, object]:
    compatibility = (capabilities or {}).get("compatibility")
    features = {
        "app_server_attachment": True,
        "at_most_once_dispatch": True,
        "deterministic_interruption": True,
        "persistent_audio_owner": True,
        "privacy_safe_diagnostics": True,
        "supervised_lifecycle": True,
    }
    return {
        "commands": list(COMMANDS),
        "compatibility": compatibility,
        "environment_keys": list(ENVIRONMENT_KEYS),
        "exit_codes": dict(sorted(EXIT_CODES.items())),
        "features": dict(sorted(features.items())),
        "generated_at": _generated_at(),
        "protocol_versions": list((capabilities or {}).get("protocol_versions", [1, 2])),
        "schema_version": SCHEMA_VERSION,
    }


def status_document(payload: Mapping[str, Any]) -> dict[str, object]:
    capabilities = payload.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    compatibility = capabilities.get("compatibility")
    compatibility = compatibility if isinstance(compatibility, Mapping) else {}
    session = payload.get("session")
    session = session if isinstance(session, Mapping) else {}
    turn = payload.get("turn")
    turn = turn if isinstance(turn, Mapping) else {}
    providers = compatibility.get("providers", [])
    return {
        "adapter": compatibility.get("host", {}).get("adapter")
        if isinstance(compatibility.get("host"), Mapping)
        else turn.get("adapter"),
        "audio": {"enabled": bool(capabilities.get("audio_enabled", False))},
        "broker": {
            "phase": payload.get("state"),
            "queue_depth": payload.get("pending_turns", 0),
            "shutting_down": bool(payload.get("shutting_down", False)),
            "uptime_seconds": payload.get("uptime_seconds", 0),
        },
        "generated_at": _generated_at(),
        "health": compatibility.get("disposition", "unknown"),
        "last_recoverable_error": turn.get("last_recoverable_error"),
        "latency_ms": {},
        "providers": providers if isinstance(providers, list) else [],
        "repository": _repo_label(session.get("repo_root") or turn.get("repo_root")),
        "request": {
            "id": turn.get("request_id"),
            "presentation": turn.get("presentation"),
            "state": turn.get("state"),
        },
        "schema_version": SCHEMA_VERSION,
        "supervisor": {"state": "running"},
        "thread": {"id": session.get("codex_session_id") or turn.get("thread_id")},
    }
