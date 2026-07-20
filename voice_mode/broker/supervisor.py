"""Idempotent user-service lifecycle for the hands-free broker."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .client import BrokerClient, BrokerUnavailable


class SupervisorState(str, Enum):
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class LifecycleResult:
    action: str
    state: SupervisorState
    changed: bool
    service_path: Path
    message: str


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess]
LivenessProbe = Callable[[], bool]


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _broker_is_live() -> bool:
    try:
        BrokerClient().status()
    except BrokerUnavailable:
        return False
    return True


class BrokerSupervisor:
    """Render and control one launchd or systemd user service."""

    LABEL = "com.voicemode.broker"
    UNIT = "voicemode-broker.service"

    def __init__(
        self,
        *,
        home: Path | None = None,
        system: str | None = None,
        command_runner: CommandRunner = _run,
        liveness_probe: LivenessProbe = _broker_is_live,
        uid: int | None = None,
    ) -> None:
        self.home = (home or Path.home()).resolve()
        self.system = system or platform.system()
        if self.system not in {"Darwin", "Linux"}:
            raise ValueError(f"unsupported service platform: {self.system}")
        self.command_runner = command_runner
        self.liveness_probe = liveness_probe
        self.uid = os.getuid() if uid is None else uid
        self.base_dir = self.home / ".voicemode"
        self.log_dir = self.base_dir / "logs" / "broker"
        self.script_path = (
            self.base_dir / "services" / "broker" / "bin" / "start-voicemode-broker.sh"
        )
        self.service_path = (
            self.home / "Library" / "LaunchAgents" / f"{self.LABEL}.plist"
            if self.system == "Darwin"
            else self.home / ".config" / "systemd" / "user" / self.UNIT
        )

    @property
    def _templates(self) -> Path:
        return Path(__file__).parent.parent / "templates"

    def render_script(self) -> str:
        return (
            self._templates / "scripts" / "start-voicemode-broker.sh"
        ).read_text(encoding="utf-8")

    def render_service(self) -> str:
        template = (
            self._templates / "launchd" / f"{self.LABEL}.plist"
            if self.system == "Darwin"
            else self._templates / "systemd" / self.UNIT
        ).read_text(encoding="utf-8")
        return template.format(HOME=self.home, START_SCRIPT=self.script_path)

    def install(self) -> LifecycleResult:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        script_changed = self._write_if_changed(
            self.script_path, self.render_script(), mode=0o755
        )
        service_changed = self._write_if_changed(
            self.service_path, self.render_service(), mode=0o644
        )
        if self.system == "Linux" and service_changed:
            self._checked(["systemctl", "--user", "daemon-reload"])
            self._checked(["systemctl", "--user", "enable", self.UNIT])
        return LifecycleResult(
            "install",
            SupervisorState.RUNNING if self.liveness_probe() else SupervisorState.STOPPED,
            script_changed or service_changed,
            self.service_path,
            "broker service installed" if script_changed or service_changed else "broker service already current",
        )

    def start(self) -> LifecycleResult:
        if self.liveness_probe():
            return LifecycleResult(
                "start",
                SupervisorState.RUNNING,
                False,
                self.service_path,
                "broker is already running; existing owner retained",
            )
        if not self.service_path.exists() or not self.script_path.exists():
            self.install()
            if self.liveness_probe():
                return LifecycleResult(
                    "start",
                    SupervisorState.RUNNING,
                    False,
                    self.service_path,
                    "broker became live during install; existing owner retained",
                )
        if self.system == "Darwin":
            domain = f"gui/{self.uid}"
            loaded = self.command_runner(
                ["launchctl", "bootstrap", domain, str(self.service_path)]
            )
            if loaded.returncode != 0:
                self._checked(
                    ["launchctl", "kickstart", "-k", f"{domain}/{self.LABEL}"]
                )
        else:
            self._checked(["systemctl", "--user", "start", self.UNIT])
        return LifecycleResult(
            "start",
            SupervisorState.STARTING,
            True,
            self.service_path,
            "broker start requested; readiness is confirmed by the broker socket",
        )

    def stop(self) -> LifecycleResult:
        if not self.service_path.exists() and not self.liveness_probe():
            return LifecycleResult(
                "stop",
                SupervisorState.NOT_INSTALLED,
                False,
                self.service_path,
                "broker service is not installed",
            )
        if self.system == "Darwin":
            result = self.command_runner(
                ["launchctl", "bootout", f"gui/{self.uid}", str(self.service_path)]
            )
        else:
            result = self.command_runner(
                ["systemctl", "--user", "stop", self.UNIT]
            )
        changed = result.returncode == 0
        return LifecycleResult(
            "stop",
            SupervisorState.STOPPED,
            changed,
            self.service_path,
            "broker stopped; configuration, logs, and journals were preserved",
        )

    def restart(self) -> LifecycleResult:
        if not self.service_path.exists() or not self.script_path.exists():
            self.install()
        if self.system == "Darwin":
            domain = f"gui/{self.uid}"
            result = self.command_runner(
                ["launchctl", "kickstart", "-k", f"{domain}/{self.LABEL}"]
            )
            if result.returncode != 0:
                self._checked(
                    ["launchctl", "bootstrap", domain, str(self.service_path)]
                )
        else:
            self._checked(["systemctl", "--user", "restart", self.UNIT])
        return LifecycleResult(
            "restart",
            SupervisorState.STARTING,
            True,
            self.service_path,
            "broker restart requested",
        )

    def status(self) -> LifecycleResult:
        if self.liveness_probe():
            state = SupervisorState.RUNNING
            message = "broker socket is live"
        elif not self.service_path.exists():
            state = SupervisorState.NOT_INSTALLED
            message = "broker service is not installed"
        else:
            command = (
                ["launchctl", "print", f"gui/{self.uid}/{self.LABEL}"]
                if self.system == "Darwin"
                else ["systemctl", "--user", "is-active", self.UNIT]
            )
            result = self.command_runner(command)
            state = (
                SupervisorState.UNHEALTHY
                if result.returncode == 0
                else SupervisorState.STOPPED
            )
            message = (
                "service manager reports active but broker socket is not live"
                if state is SupervisorState.UNHEALTHY
                else "broker service is stopped"
            )
        return LifecycleResult(
            "status", state, False, self.service_path, message
        )

    def _checked(self, command: list[str]) -> subprocess.CompletedProcess:
        result = self.command_runner(command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(f"{' '.join(command)} failed: {detail}")
        return result

    @staticmethod
    def _write_if_changed(path: Path, content: str, *, mode: int) -> bool:
        encoded = content.encode()
        if path.exists() and path.read_bytes() == encoded:
            path.chmod(mode)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(encoded)
        temporary.chmod(mode)
        temporary.replace(path)
        return True
