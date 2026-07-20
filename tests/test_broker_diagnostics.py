import json

from voice_mode.broker.diagnostics import capabilities_document, status_document


def broker_payload():
    return {
        "state": "thinking",
        "pending_turns": 1,
        "uptime_seconds": 12.5,
        "shutting_down": False,
        "session": {
            "codex_session_id": "thread-1",
            "repo_root": "/Users/private/secret-repo",
        },
        "turn": {
            "request_id": "request-1",
            "adapter": "app-server",
            "thread_id": "thread-1",
            "repo_root": "/Users/private/secret-repo",
            "state": "dispatched",
            "presentation": "none",
            "last_recoverable_error": None,
        },
        "capabilities": {
            "protocol_versions": [1, 2],
            "audio_enabled": True,
            "compatibility": {
                "disposition": "supported",
                "host": {"adapter": "app-server"},
                "providers": [
                    {
                        "service": "stt",
                        "provider": "whisper",
                        "available": True,
                        "capabilities": ["transcribe"],
                    }
                ],
            },
        },
    }


def test_status_projection_is_deterministic_and_redacts_home_path(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")

    first = json.dumps(status_document(broker_payload()), sort_keys=True)
    second = json.dumps(status_document(broker_payload()), sort_keys=True)

    assert first == second
    assert "/Users/private" not in first
    assert '"repository": "secret-repo"' in first
    assert '"generated_at": "1970-01-01T00:00:00Z"' in first
    assert "transcript" not in first


def test_capabilities_document_has_stable_agent_contract(monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    document = capabilities_document(broker_payload()["capabilities"])

    assert document["schema_version"] == 1
    assert document["generated_at"] is None
    assert document["commands"] == sorted(document["commands"])
    assert document["environment_keys"] == sorted(document["environment_keys"])
    assert document["exit_codes"]["conflict"] == 6
    assert document["features"]["at_most_once_dispatch"] is True
