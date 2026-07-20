"""Capability-based startup compatibility for the hands-free broker."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from voice_mode.__version__ import __version__ as PACKAGE_VERSION
from voice_mode.provider_discovery import EndpointInfo, provider_registry

from .protocol import SUPPORTED_PROTOCOL_VERSIONS
from .types import HostCapability, HostProbe


class CompatibilitySeverity(str, Enum):
    BLOCKER = "blocker"
    DEGRADED = "degraded"
    ADVISORY = "advisory"


class CompatibilityDisposition(str, Enum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ProviderCapability:
    service: str
    provider: str
    available: bool
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class CompatibilityIssue:
    code: str
    severity: CompatibilitySeverity
    message: str
    recovery_command: str

    def projection(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "recovery_command": self.recovery_command,
            "severity": self.severity.value,
        }


@dataclass(frozen=True)
class CompatibilityReport:
    package_version: str
    cli_version: str | None
    protocol_versions: tuple[int, ...]
    plugin_version: str | None
    host_adapter: str
    host_capabilities: tuple[str, ...]
    providers: tuple[ProviderCapability, ...]
    issues: tuple[CompatibilityIssue, ...]

    @property
    def disposition(self) -> CompatibilityDisposition:
        severities = {issue.severity for issue in self.issues}
        if CompatibilitySeverity.BLOCKER in severities:
            return CompatibilityDisposition.BLOCKED
        if CompatibilitySeverity.DEGRADED in severities:
            return CompatibilityDisposition.DEGRADED
        return CompatibilityDisposition.SUPPORTED

    def projection(self) -> dict[str, object]:
        return {
            "cli_version": self.cli_version,
            "disposition": self.disposition.value,
            "host": {
                "adapter": self.host_adapter,
                "capabilities": list(self.host_capabilities),
            },
            "issues": [issue.projection() for issue in self.issues],
            "package_version": self.package_version,
            "plugin_version": self.plugin_version,
            "protocol_versions": list(self.protocol_versions),
            "providers": [
                {
                    "available": provider.available,
                    "capabilities": list(provider.capabilities),
                    "provider": provider.provider,
                    "service": provider.service,
                }
                for provider in self.providers
            ],
        }

    def require_supported_input(self) -> None:
        blocker = next(
            (
                issue
                for issue in self.issues
                if issue.severity is CompatibilitySeverity.BLOCKER
            ),
            None,
        )
        if blocker is not None:
            raise CompatibilityBlocked(blocker)


class CompatibilityBlocked(RuntimeError):
    def __init__(self, issue: CompatibilityIssue) -> None:
        self.issue = issue
        super().__init__(
            f"{issue.message}. Run: {issue.recovery_command}"
        )


def _base_version(version: str | None) -> str | None:
    if version is None:
        return None
    match = re.search(r"\d+\.\d+\.\d+", version)
    return match.group(0) if match else version.strip() or None


def _probe_cli_version() -> str | None:
    executable = shutil.which("voicemode")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return _base_version(result.stdout)


def _probe_cli_commands() -> tuple[str, ...] | None:
    executable = shutil.which("voicemode")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    commands = {
        line.strip().split(maxsplit=1)[0]
        for line in result.stdout.splitlines()
        if line.startswith("  ") and line.strip()
    }
    return tuple(sorted(commands))


def load_plugin_metadata(paths: Iterable[Path] | None = None) -> Mapping[str, Any] | None:
    candidates = tuple(paths or ()) or (
        Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json",
    )
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _plugin_protocols(metadata: Mapping[str, Any] | None) -> tuple[int, ...] | None:
    if metadata is None:
        return None
    compatibility = metadata.get("voicemodeCompatibility")
    if not isinstance(compatibility, Mapping):
        return None
    versions = compatibility.get("brokerProtocolVersions")
    if not isinstance(versions, list) or any(
        isinstance(version, bool) or not isinstance(version, int) for version in versions
    ):
        return None
    return tuple(sorted(set(versions)))


def provider_capabilities(
    snapshot: Mapping[str, Iterable[EndpointInfo]],
) -> tuple[ProviderCapability, ...]:
    capabilities = []
    for service in ("stt", "tts"):
        for endpoint in snapshot.get(service, ()):
            capabilities.append(
                ProviderCapability(
                    service=service,
                    provider=endpoint.provider_type or "unknown",
                    available=endpoint.last_check is not None and endpoint.last_error is None,
                    capabilities=("transcribe",) if service == "stt" else ("speak",),
                )
            )
    return tuple(
        sorted(
            capabilities,
            key=lambda item: (item.service, item.provider, not item.available),
        )
    )


def evaluate_compatibility(
    *,
    package_version: str,
    cli_version: str | None,
    protocol_versions: Iterable[int],
    plugin_metadata: Mapping[str, Any] | None,
    providers: Iterable[ProviderCapability],
    host_probe: HostProbe,
    cli_commands: Iterable[str] | None = None,
) -> CompatibilityReport:
    protocol_versions = tuple(sorted(set(protocol_versions)))
    providers = tuple(providers)
    issues: list[CompatibilityIssue] = []
    if _base_version(cli_version) != _base_version(package_version):
        issues.append(
            CompatibilityIssue(
                "cli_package_mismatch",
                CompatibilitySeverity.BLOCKER,
                "the installed voicemode CLI does not match the running package",
                "uv tool install --upgrade voice-mode",
            )
        )
    required_commands = {"broker", "restart", "start", "stop"}
    if cli_commands is not None and not required_commands.issubset(cli_commands):
        missing = sorted(required_commands - set(cli_commands))
        issues.append(
            CompatibilityIssue(
                "cli_commands_missing",
                CompatibilitySeverity.BLOCKER,
                f"the installed voicemode CLI is missing commands: {', '.join(missing)}",
                "uv tool install --upgrade voice-mode",
            )
        )
    if not set(protocol_versions).intersection(SUPPORTED_PROTOCOL_VERSIONS):
        issues.append(
            CompatibilityIssue(
                "broker_protocol_incompatible",
                CompatibilitySeverity.BLOCKER,
                "the broker and client have no compatible protocol version",
                "uv tool install --upgrade voice-mode && voicemode restart",
            )
        )

    plugin_version = (
        str(plugin_metadata.get("version"))
        if plugin_metadata is not None and plugin_metadata.get("version") is not None
        else None
    )
    plugin_protocols = _plugin_protocols(plugin_metadata)
    if plugin_protocols is not None and not set(plugin_protocols).intersection(
        protocol_versions
    ):
        issues.append(
            CompatibilityIssue(
                "plugin_protocol_incompatible",
                CompatibilitySeverity.BLOCKER,
                "the installed VoiceMode plugin cannot speak this broker protocol",
                "claude plugin install voicemode@voicemode && voicemode restart",
            )
        )
    elif plugin_version is not None and _base_version(plugin_version) != _base_version(
        package_version
    ):
        issues.append(
            CompatibilityIssue(
                "plugin_version_drift",
                CompatibilitySeverity.ADVISORY,
                "the VoiceMode plugin and package versions differ but their declared capabilities remain compatible",
                "claude plugin install voicemode@voicemode",
            )
        )

    available_stt = any(
        provider.service == "stt"
        and provider.available
        and "transcribe" in provider.capabilities
        for provider in providers
    )
    if not available_stt:
        issues.append(
            CompatibilityIssue(
                "stt_unavailable",
                CompatibilitySeverity.BLOCKER,
                "no configured speech-to-text provider is healthy",
                "voicemode service install whisper && voicemode restart",
            )
        )
    available_tts = any(
        provider.service == "tts"
        and provider.available
        and "speak" in provider.capabilities
        for provider in providers
    )
    if not available_tts:
        issues.append(
            CompatibilityIssue(
                "tts_unavailable",
                CompatibilitySeverity.DEGRADED,
                "no configured text-to-speech provider is healthy; responses remain visible",
                "voicemode service install kokoro && voicemode restart",
            )
        )

    required_host = {HostCapability.START_TURN, HostCapability.SUBSCRIBE_EVENTS}
    if not host_probe.available or not required_host.issubset(host_probe.capabilities):
        issues.append(
            CompatibilityIssue(
                "codex_host_unavailable",
                CompatibilitySeverity.BLOCKER,
                "the selected Codex adapter cannot deliver correlated turns",
                "voicemode broker run --adapter exec",
            )
        )
    elif host_probe.adapter == "exec":
        issues.append(
            CompatibilityIssue(
                "codex_app_server_unavailable",
                CompatibilitySeverity.DEGRADED,
                "Codex app-server is unavailable; using a labeled separate Codex child",
                "voicemode broker run --adapter exec",
            )
        )
    if host_probe.available and HostCapability.INTERRUPT_TURN not in host_probe.capabilities:
        issues.append(
            CompatibilityIssue(
                "host_interrupt_unavailable",
                CompatibilitySeverity.DEGRADED,
                "the selected Codex adapter cannot interrupt an active turn",
                "voicemode broker run --adapter app-server",
            )
        )

    severity_rank = {
        CompatibilitySeverity.BLOCKER: 0,
        CompatibilitySeverity.DEGRADED: 1,
        CompatibilitySeverity.ADVISORY: 2,
    }
    issues.sort(key=lambda issue: (severity_rank[issue.severity], issue.code))
    return CompatibilityReport(
        package_version=package_version,
        cli_version=cli_version,
        protocol_versions=protocol_versions,
        plugin_version=plugin_version,
        host_adapter=host_probe.adapter,
        host_capabilities=tuple(
            sorted(capability.value for capability in host_probe.capabilities)
        ),
        providers=tuple(providers),
        issues=tuple(issues),
    )


def probe_startup_compatibility(
    host_probe: HostProbe,
    *,
    cli_version_probe: Callable[[], str | None] = _probe_cli_version,
    cli_commands_probe: Callable[[], tuple[str, ...] | None] = _probe_cli_commands,
    plugin_metadata: Mapping[str, Any] | None = None,
) -> CompatibilityReport:
    snapshot = asyncio.run(provider_registry.probe_compatibility())
    return evaluate_compatibility(
        package_version=PACKAGE_VERSION,
        cli_version=cli_version_probe(),
        protocol_versions=SUPPORTED_PROTOCOL_VERSIONS,
        plugin_metadata=(
            load_plugin_metadata() if plugin_metadata is None else plugin_metadata
        ),
        providers=provider_capabilities(snapshot),
        host_probe=host_probe,
        cli_commands=cli_commands_probe(),
    )
