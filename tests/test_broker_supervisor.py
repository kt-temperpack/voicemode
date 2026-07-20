import subprocess
from pathlib import Path

import pytest

from voice_mode.broker.supervisor import BrokerSupervisor, SupervisorState


FIXTURES = Path(__file__).parent / "fixtures" / "broker"


class Commands:
    def __init__(self, returncodes=None):
        self.calls = []
        self.returncodes = iter(returncodes or [])

    def __call__(self, command):
        self.calls.append(command)
        try:
            returncode = next(self.returncodes)
        except StopIteration:
            returncode = 0
        return subprocess.CompletedProcess(command, returncode, "", "failed")


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_renders_deterministically_and_is_idempotent(tmp_path, system):
    commands = Commands()
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system=system,
        command_runner=commands,
        liveness_probe=lambda: False,
        uid=501,
    )

    first = supervisor.install()
    rendered = supervisor.service_path.read_text()
    second = supervisor.install()

    assert first.changed is True
    assert second.changed is False
    assert "{" not in rendered
    assert str(supervisor.script_path) in rendered
    assert supervisor.script_path.stat().st_mode & 0o777 == 0o755
    assert supervisor.log_dir.is_dir()
    if system == "Darwin":
        assert "ThrottleInterval" in rendered
        assert commands.calls == []
    else:
        assert "Restart=always" in rendered
        assert commands.calls == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "voicemode-broker.service"],
        ]


@pytest.mark.parametrize(
    ("system", "fixture"),
    [
        ("Darwin", "supervisor_darwin.plist"),
        ("Linux", "supervisor_linux.service"),
    ],
)
def test_service_rendering_matches_platform_goldens(system, fixture):
    supervisor = BrokerSupervisor(
        home=Path("/Users/tester"),
        system=system,
        command_runner=Commands(),
        liveness_probe=lambda: False,
        uid=501,
    )

    assert supervisor.render_service() == (FIXTURES / fixture).read_text()


def test_start_script_loads_configuration_as_data_and_keeps_foreground_mode(tmp_path):
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Linux",
        command_runner=Commands(),
        liveness_probe=lambda: False,
    )
    script = supervisor.render_script()

    assert "source " not in script
    assert "eval " not in script
    assert 'exec voicemode broker run --repo "$BROKER_REPO"' in script
    assert "--no-terminal-keys" in script


def test_start_retains_a_live_owner_without_touching_service_manager(tmp_path):
    commands = Commands()
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Darwin",
        command_runner=commands,
        liveness_probe=lambda: True,
        uid=501,
    )

    first = supervisor.start()
    second = supervisor.start()

    assert first == second
    assert first.state is SupervisorState.RUNNING
    assert first.changed is False
    assert commands.calls == []


def test_start_rechecks_liveness_after_install_before_service_start(tmp_path):
    probes = iter([False, False, True])
    commands = Commands()
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Darwin",
        command_runner=commands,
        liveness_probe=lambda: next(probes),
        uid=501,
    )

    result = supervisor.start()

    assert result.state is SupervisorState.RUNNING
    assert result.changed is False
    assert commands.calls == []


def test_launchd_start_kickstarts_an_already_loaded_but_unhealthy_service(tmp_path):
    commands = Commands([1, 0])
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Darwin",
        command_runner=commands,
        liveness_probe=lambda: False,
        uid=501,
    )
    supervisor.install()

    result = supervisor.start()

    assert result.state is SupervisorState.STARTING
    assert commands.calls == [
        ["launchctl", "bootstrap", "gui/501", str(supervisor.service_path)],
        ["launchctl", "kickstart", "-k", "gui/501/com.voicemode.broker"],
    ]


def test_stop_preserves_service_logs_journal_and_user_data(tmp_path):
    commands = Commands()
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Linux",
        command_runner=commands,
        liveness_probe=lambda: True,
    )
    supervisor.install()
    marker = supervisor.base_dir / "journal" / "keep-me"
    marker.parent.mkdir(parents=True)
    marker.write_text("user data")

    result = supervisor.stop()

    assert result.state is SupervisorState.STOPPED
    assert supervisor.service_path.exists()
    assert supervisor.script_path.exists()
    assert marker.read_text() == "user data"
    assert commands.calls[-1] == [
        "systemctl",
        "--user",
        "stop",
        "voicemode-broker.service",
    ]


def test_status_distinguishes_absent_stopped_unhealthy_and_live(tmp_path):
    live = False
    commands = Commands([0, 0, 1, 0])
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Linux",
        command_runner=commands,
        liveness_probe=lambda: live,
    )
    assert supervisor.status().state is SupervisorState.NOT_INSTALLED
    supervisor.install()
    assert supervisor.status().state is SupervisorState.STOPPED
    assert supervisor.status().state is SupervisorState.UNHEALTHY
    live = True
    assert supervisor.status().state is SupervisorState.RUNNING


def test_restart_uses_supervisor_without_deleting_or_spawning_directly(tmp_path):
    commands = Commands()
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Linux",
        command_runner=commands,
        liveness_probe=lambda: False,
    )
    supervisor.install()

    result = supervisor.restart()

    assert result.state is SupervisorState.STARTING
    assert commands.calls[-1] == [
        "systemctl",
        "--user",
        "restart",
        "voicemode-broker.service",
    ]


def test_unsupported_platform_and_command_failure_are_explicit(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        BrokerSupervisor(home=tmp_path, system="Windows")

    commands = Commands([1])
    supervisor = BrokerSupervisor(
        home=tmp_path,
        system="Linux",
        command_runner=commands,
        liveness_probe=lambda: False,
    )
    with pytest.raises(RuntimeError, match="daemon-reload failed"):
        supervisor.install()
