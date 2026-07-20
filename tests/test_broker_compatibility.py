import json
from pathlib import Path

import pytest

from voice_mode.broker.compatibility import (
    CompatibilityBlocked,
    CompatibilityDisposition,
    CompatibilitySeverity,
    ProviderCapability,
    evaluate_compatibility,
    load_plugin_metadata,
    provider_capabilities,
)
from voice_mode.broker import handsfree as handsfree_module
from voice_mode.broker.protocol import DiagnosticRequest
from voice_mode.broker.server import create_broker
from voice_mode.broker.types import HostCapability, HostProbe
from voice_mode.provider_discovery import EndpointInfo


APP_SERVER_CAPABILITIES = frozenset(
    {
        HostCapability.START_TURN,
        HostCapability.SUBSCRIBE_EVENTS,
        HostCapability.INTERRUPT_TURN,
    }
)


def healthy_providers() -> tuple[ProviderCapability, ...]:
    return (
        ProviderCapability("stt", "whisper", True, ("transcribe",)),
        ProviderCapability("tts", "kokoro", True, ("speak",)),
    )


def evaluate(**overrides):
    values = {
        "package_version": "8.11.0",
        "cli_version": "8.11.0",
        "protocol_versions": (1, 2),
        "plugin_metadata": {
            "version": "8.11.0p0",
            "voicemodeCompatibility": {"brokerProtocolVersions": [1, 2]},
        },
        "providers": healthy_providers(),
        "host_probe": HostProbe("app-server", True, APP_SERVER_CAPABILITIES),
    }
    values.update(overrides)
    return evaluate_compatibility(**values)


def test_supported_capabilities_win_over_version_suffixes():
    report = evaluate(cli_version="VoiceMode, version 8.11.0-dev.abc123")

    assert report.disposition is CompatibilityDisposition.SUPPORTED
    assert report.issues == ()


def test_stale_cli_is_a_hard_blocker_with_one_upgrade_command():
    report = evaluate(cli_version="8.10.0")

    assert report.disposition is CompatibilityDisposition.BLOCKED
    issue = report.issues[0]
    assert issue.code == "cli_package_mismatch"
    assert issue.severity is CompatibilitySeverity.BLOCKER
    assert issue.recovery_command == "uv tool install --upgrade voice-mode"
    with pytest.raises(CompatibilityBlocked, match="Run: uv tool install"):
        report.require_supported_input()


def test_same_version_stale_cli_missing_broker_command_is_blocked():
    report = evaluate(cli_commands=("status", "converse"))

    issue = next(issue for issue in report.issues if issue.code == "cli_commands_missing")
    assert issue.severity is CompatibilitySeverity.BLOCKER
    assert "broker, restart, start, stop" in issue.message
    assert issue.recovery_command == "uv tool install --upgrade voice-mode"


def test_plugin_protocol_mismatch_blocks_even_when_versions_match():
    report = evaluate(
        plugin_metadata={
            "version": "8.11.0p0",
            "voicemodeCompatibility": {"brokerProtocolVersions": [9]},
        }
    )

    issue = next(issue for issue in report.issues if issue.code == "plugin_protocol_incompatible")
    assert issue.severity is CompatibilitySeverity.BLOCKER
    assert issue.recovery_command == "claude plugin install voicemode@voicemode && voicemode restart"


def test_plugin_version_drift_is_advisory_when_protocol_is_compatible():
    report = evaluate(
        plugin_metadata={
            "version": "8.9.0p0",
            "voicemodeCompatibility": {"brokerProtocolVersions": [2]},
        }
    )

    assert report.disposition is CompatibilityDisposition.SUPPORTED
    assert report.issues[0].code == "plugin_version_drift"
    assert report.issues[0].severity is CompatibilitySeverity.ADVISORY


def test_stt_outage_blocks_before_capture_but_tts_outage_degrades():
    no_stt = evaluate(
        providers=(
            ProviderCapability("stt", "whisper", False, ("transcribe",)),
            ProviderCapability("tts", "kokoro", True, ("speak",)),
        )
    )
    no_tts = evaluate(
        providers=(
            ProviderCapability("stt", "whisper", True, ("transcribe",)),
            ProviderCapability("tts", "kokoro", False, ("speak",)),
        )
    )

    assert no_stt.disposition is CompatibilityDisposition.BLOCKED
    assert no_stt.issues[0].code == "stt_unavailable"
    assert no_stt.issues[0].recovery_command.startswith("voicemode service install whisper")
    assert no_tts.disposition is CompatibilityDisposition.DEGRADED
    assert no_tts.issues[0].code == "tts_unavailable"


def test_missing_app_server_has_a_deterministic_exec_fallback():
    report = evaluate(
        host_probe=HostProbe(
            "exec",
            True,
            frozenset(
                {
                    HostCapability.START_TURN,
                    HostCapability.SUBSCRIBE_EVENTS,
                    HostCapability.INTERRUPT_TURN,
                }
            ),
        )
    )

    assert report.disposition is CompatibilityDisposition.DEGRADED
    assert report.issues[0].code == "codex_app_server_unavailable"
    assert report.issues[0].recovery_command == "voicemode broker run --adapter exec"


def test_missing_correlated_turn_capability_is_a_blocker():
    report = evaluate(
        host_probe=HostProbe(
            "app-server", True, frozenset({HostCapability.START_TURN})
        )
    )

    assert report.disposition is CompatibilityDisposition.BLOCKED
    assert report.issues[0].code == "codex_host_unavailable"


def test_provider_projection_never_contains_urls_or_raw_errors():
    providers = provider_capabilities(
        {
            "stt": (
                EndpointInfo(
                    "https://token@example.invalid/v1",
                    [],
                    [],
                    "whisper",
                    "2026-07-20T00:00:00Z",
                    "Authorization: secret-token",
                ),
            ),
            "tts": (
                EndpointInfo(
                    "http://127.0.0.1:8880/v1",
                    ["tts-1"],
                    ["am_michael"],
                    "kokoro",
                    "2026-07-20T00:00:00Z",
                    None,
                ),
            ),
        }
    )

    encoded = json.dumps([provider.__dict__ for provider in providers])
    assert "example.invalid" not in encoded
    assert "secret-token" not in encoded
    assert providers[0].available is False
    assert providers[1].available is True


def test_plugin_metadata_declares_current_protocol_range():
    metadata = load_plugin_metadata(
        (Path(__file__).parents[1] / ".claude-plugin" / "plugin.json",)
    )

    assert metadata is not None
    assert metadata["voicemodeCompatibility"]["brokerProtocolVersions"] == [1, 2]


def test_status_capabilities_expose_redacted_compatibility_projection(tmp_path):
    projection = evaluate().projection()
    runtime, dispatcher, server = create_broker(
        tmp_path / "broker.sock",
        compatibility=projection,
    )

    result = dispatcher.dispatch(DiagnosticRequest("diagnostic-1"))

    assert result["capabilities"]["compatibility"] == projection
    assert result["capabilities"]["compatibility"]["disposition"] == "supported"
    runtime.begin_shutdown()
    server.stop()


def test_startup_block_closes_host_before_audio_is_created(monkeypatch, tmp_path):
    calls = []

    class FakeHost:
        def __init__(self, _codex):
            pass

        def probe(self):
            calls.append("host-probe")
            return HostProbe("exec", True, APP_SERVER_CAPABILITIES)

        def close(self):
            calls.append("host-close")

    monkeypatch.setattr(handsfree_module, "ExecCodexAdapter", FakeHost)
    monkeypatch.setattr(
        handsfree_module,
        "probe_startup_compatibility",
        lambda _probe: (_ for _ in ()).throw(RuntimeError("blocked before audio")),
    )
    monkeypatch.setattr(
        handsfree_module,
        "PersistentVoiceAudio",
        lambda **_kwargs: calls.append("audio-created"),
    )

    with pytest.raises(RuntimeError, match="blocked before audio"):
        handsfree_module.run_handsfree_broker(
            tmp_path / "broker.sock",
            repo_root=tmp_path,
            wake_phrase="Computer",
            voice="am_michael",
            voice_speed=1.25,
            listen_duration=60,
            min_duration=2,
            codex_executable="codex",
            codex_sandbox="workspace-write",
            codex_model="model",
            codex_reasoning_effort="low",
            silence_threshold_ms=900,
            codex_adapter="exec",
        )

    assert calls == ["host-probe", "host-close"]
