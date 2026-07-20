import json
import re
from pathlib import Path
from typing import Any

import pytest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "realtime"
MAX_FIXTURE_BYTES = 64 * 1024
MAX_STRING_BYTES = 4 * 1024
MAX_COLLECTION_ITEMS = 128
MAX_DEPTH = 16

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]+", re.IGNORECASE),
)
RAW_CALL_ID_PATTERN = re.compile(r"\brtc_(?!fixture_)[A-Za-z0-9_-]+\b")
URL_FRAGMENT_PATTERN = re.compile(r"https?://[^\s\"]*#", re.IGNORECASE)
HOME_PATH_PATTERN = re.compile(r"(?:/Users/|/home/)[^\s\"]+")
RAW_SDP_PATTERN = re.compile(
    r"(?:^|\r?\n)(?:v=0|o=-\s|s=-(?:\r?$)|t=0 0|m=audio\s|"
    r"a=(?:candidate|fingerprint|ice-pwd|ice-ufrag|mid|rtpmap|setup):)"
)
SYNTHETIC_CONTENT_KEYS = frozenset(
    {"delta", "instruction", "message", "reason", "summary", "task", "text", "transcript"}
)
TIMESTAMP_KEYS = frozenset(
    {
        "completedAt",
        "completed_at",
        "createdAt",
        "created_at",
        "timestamp",
        "updatedAt",
        "updated_at",
    }
)


def _fixture_paths() -> tuple[Path, ...]:
    return tuple(sorted(FIXTURE_DIR.glob("*.json"), key=lambda path: path.name))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_file_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _load_fixture(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    assert len(raw) <= MAX_FIXTURE_BYTES, f"fixture exceeds {MAX_FIXTURE_BYTES} bytes: {path.name}"
    value = json.loads(raw)
    assert isinstance(value, dict), f"fixture root must be an object: {path.name}"
    assert value.get("schema_version") == 1
    assert isinstance(value.get("contract_source"), str)
    return value


def _assert_safe(value: Any, *, key: str | None = None, depth: int = 0) -> None:
    assert depth <= MAX_DEPTH, "fixture nesting is unbounded"

    if isinstance(value, dict):
        assert len(value) <= MAX_COLLECTION_ITEMS, "fixture object is unbounded"
        for child_key, child_value in value.items():
            assert isinstance(child_key, str)
            if child_key in TIMESTAMP_KEYS:
                assert child_value in (None, 0), f"unstable timestamp in {child_key}"
            _assert_safe(child_value, key=child_key, depth=depth + 1)
        return

    if isinstance(value, list):
        assert len(value) <= MAX_COLLECTION_ITEMS, "fixture list is unbounded"
        for child in value:
            _assert_safe(child, depth=depth + 1)
        return

    if not isinstance(value, str):
        return

    assert len(value.encode("utf-8")) <= MAX_STRING_BYTES, "fixture string is unbounded"
    for pattern in SECRET_PATTERNS:
        assert pattern.search(value) is None, "credential-like fixture content"
    assert RAW_CALL_ID_PATTERN.search(value) is None, "non-synthetic realtime call ID"
    assert URL_FRAGMENT_PATTERN.search(value) is None, "URL fragment may contain a capability"
    assert HOME_PATH_PATTERN.search(value) is None, "absolute user-home path"
    assert RAW_SDP_PATTERN.search(value) is None, "raw SDP is forbidden"

    if key in SYNTHETIC_CONTENT_KEYS:
        assert value.startswith("[synthetic]"), f"unlabeled content in {key}"

    stripped = value.lstrip()
    if key in {"arguments", "output"} and stripped.startswith(("{", "[")):
        _assert_safe(json.loads(stripped), depth=depth + 1)


def _fixture_cases(document: dict[str, Any], collection: str) -> set[str]:
    return {entry["fixture_case"] for entry in document[collection]}


def test_fixture_inventory_is_complete_and_deterministically_sorted():
    paths = _fixture_paths()

    assert tuple(path.name for path in paths) == (
        "app-server-events.json",
        "app-server-recovery.json",
        "openai-call-cases.json",
        "openai-server-events.json",
    )


def test_all_fixtures_decode_and_pass_the_scrubber():
    for path in _fixture_paths():
        _assert_safe(_load_fixture(path))


def test_fixture_canonical_bytes_are_equal_across_two_runs():
    first = tuple((path.name, _canonical_bytes(_load_fixture(path))) for path in _fixture_paths())
    reparsed = tuple(
        (name, _canonical_bytes(json.loads(encoded))) for name, encoded in first
    )

    assert first == reparsed


def test_committed_fixture_bytes_use_the_deterministic_serializer():
    first = tuple((path.name, path.read_bytes()) for path in _fixture_paths())
    second = tuple((path.name, path.read_bytes()) for path in _fixture_paths())

    assert first == second
    for path in _fixture_paths():
        assert path.read_bytes() == _canonical_file_bytes(_load_fixture(path))


def test_openai_event_fixture_covers_the_pinned_server_contract():
    document = _load_fixture(FIXTURE_DIR / "openai-server-events.json")

    assert _fixture_cases(document, "events") == {
        "audio_cleared",
        "audio_committed",
        "conversation_item_added",
        "conversation_item_created_compatibility",
        "conversation_item_done",
        "function_call_arguments_done",
        "function_output_acknowledged_added",
        "function_output_acknowledged_done",
        "input_transcript_completed",
        "input_transcript_delta",
        "output_transcript_delta",
        "output_transcript_done",
        "rate_limits_updated",
        "response_created",
        "response_done_cancelled",
        "response_done_completed_function_call",
        "session_created",
        "session_updated",
        "speech_started",
        "speech_stopped",
        "structured_error_correlated",
        "unknown_additive_event",
    }

    events = {entry["fixture_case"]: entry["wire"] for entry in document["events"]}
    session = events["session_created"]["session"]
    assert session["model"] == "gpt-realtime-2.1"
    assert session["audio"]["output"] == {
        "format": {"rate": 24000, "type": "audio/pcm"},
        "speed": 1.25,
        "voice": "marin",
    }
    assert session["audio"]["input"]["turn_detection"] == {
        "create_response": False,
        "eagerness": "low",
        "interrupt_response": True,
        "type": "semantic_vad",
    }

    completed = events["response_done_completed_function_call"]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["name"] == "delegate_codex"
    assert completed["output"][0]["status"] == "completed"
    assert events["response_done_cancelled"]["response"]["status"] == "cancelled"
    assert events["structured_error_correlated"]["error"]["event_id"] == "client_event_fixture_0001"


def test_openai_call_fixture_contains_metadata_without_sdp():
    document = _load_fixture(FIXTURE_DIR / "openai-call-cases.json")

    assert document["call_creation"]["endpoint"] == "https://api.openai.com/v1/realtime/calls"
    assert [part["name"] for part in document["call_creation"]["multipart_parts"]] == [
        "sdp",
        "session",
    ]
    assert _fixture_cases(document, "responses") == {
        "foreign_location",
        "missing_location",
        "query_bearing_location",
        "success",
    }
    assert _fixture_cases(document, "hangup_cases") == {"already_ended", "ended"}
    assert "offer_sdp" not in document
    assert "answer_sdp" not in document


def test_app_server_fixtures_cover_terminal_and_recovery_states():
    events = _load_fixture(FIXTURE_DIR / "app-server-events.json")
    recovery = _load_fixture(FIXTURE_DIR / "app-server-recovery.json")

    assert _fixture_cases(events, "events") == {
        "approval_required",
        "transport_lost",
        "turn_completed",
        "turn_failed",
        "turn_interrupted",
        "turn_started",
    }
    assert _fixture_cases(recovery, "cases") == {
        "absent",
        "ambiguous_duplicate",
        "cancelled",
        "completed",
        "in_progress",
    }
    correlated_items = [
        item
        for case in recovery["cases"]
        for turn in case["result"]["thread"]["turns"]
        for item in turn["items"]
        if "clientUserMessageId" in item
    ]
    assert correlated_items
    assert all("requestId" not in item for item in correlated_items)


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("token", "sk-fixtureSecret123"),
        ("authorization", "Bearer fixture-secret"),
        ("call_id", "rtc_realCallIdentifier"),
        ("location", "https://localhost.invalid/#capability-fixture"),
        ("path", "/Users/fixture/private.json"),
        ("path", "/home/fixture/private.json"),
        ("transcript", "real transcript content"),
        ("created_at", 1721491200),
        ("text", "x" * (MAX_STRING_BYTES + 1)),
        ("sdp", "v=0\na=ice-ufrag:fixture"),
        ("sdp", "a=fingerprint:sha-256 fixture"),
        ("arguments", " {\"text\":\"real transcript content\"}"),
        ("output", "[{\"text\":\"real transcript content\"}]"),
    ),
)
def test_scrubber_rejects_sensitive_or_unbounded_payloads(key: str, value: Any):
    with pytest.raises(AssertionError):
        _assert_safe({key: value})
