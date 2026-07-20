import json
import re
import subprocess
from pathlib import Path

from voice_mode.broker.codex import CodexAdapter
from voice_mode.broker.protocol import StatusRequest, decode_request, encode_success
from voice_mode.broker.runtime import BrokerRuntime
from voice_mode.broker.server import BrokerDispatcher


FIXTURES = Path(__file__).parent / "fixtures" / "broker"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8").strip()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_protocol_v1_status_wire_contract_matches_goldens():
    request_text = _read("protocol_v1_status_request.json")
    request = decode_request(request_text.encode())
    assert isinstance(request, StatusRequest)
    assert request.request_id == "request-1"
    assert _canonical_json(json.loads(request_text)) == request_text

    actual = encode_success("request-1", {"kind": "status", "phase": "asleep"})
    assert _canonical_json(json.loads(actual)) == _read("protocol_v1_status_response.json")


def test_protocol_v2_status_wire_contract_matches_goldens():
    request_text = _read("protocol_v2_status_request.json")
    request = decode_request(request_text.encode())
    assert isinstance(request, StatusRequest)
    assert request.protocol_version == 2

    runtime = BrokerRuntime(monotonic=lambda: 0.0)
    result = BrokerDispatcher(runtime, audio_enabled=True).dispatch(request)
    actual = encode_success("request-1", result, version=2)
    assert _canonical_json(json.loads(actual)) == _read(
        "protocol_v2_status_response.json"
    )


def test_codex_single_response_contract_matches_goldens(tmp_path):
    events_text = _read("codex_events.jsonl")
    response_text = _read("canonical_response.json")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(response_text, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=events_text, stderr="")

    events = []
    adapter = CodexAdapter(tmp_path, runner=runner, event_sink=events.append)
    result = adapter.run_turn("synthetic request")

    assert result.thread_id == "thread-1"
    assert result.display_text == result.spoken_summary == (
        "One cohesive answer from the existing Codex thread."
    )
    assert events == [json.loads(line) for line in events_text.splitlines()]
    command = calls[0][0]
    assert "mcp_servers={}" in command
    assert sum("developer_instructions=" in argument for argument in command) == 1


def test_handsfree_cue_baseline_is_explicit():
    assert json.loads(_read("handsfree_cues.json")) == [
        "submitted",
        "listening",
        "submitted",
        "listening",
        "submitted",
        "submitted",
    ]


def test_broker_fixtures_are_canonical_and_privacy_safe():
    forbidden = re.compile(
        r"/Users/|/home/|sk-[A-Za-z0-9]|api[_-]?key|bearer\s+[A-Za-z0-9]",
        re.IGNORECASE,
    )
    for path in sorted(FIXTURES.iterdir()):
        if path.suffix not in {".json", ".jsonl"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), f"sensitive fixture content in {path.name}"
        assert "\r" not in text
        if path.suffix == ".json":
            assert _canonical_json(json.loads(text)) + "\n" == text
