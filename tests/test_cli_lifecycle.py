from pathlib import Path

from click.testing import CliRunner

from voice_mode.broker.supervisor import LifecycleResult, SupervisorState
from voice_mode.cli import voice_mode_main_cli
from voice_mode.cli_commands import lifecycle


class FakeSupervisor:
    calls: list[str] = []

    def _result(self, action: str) -> LifecycleResult:
        self.calls.append(action)
        return LifecycleResult(
            action=action,
            state=(
                SupervisorState.STOPPED
                if action == "stop"
                else SupervisorState.STARTING
            ),
            changed=True,
            service_path=Path("/tmp/voicemode-broker.service"),
            message=f"broker {action} requested",
        )

    def start(self) -> LifecycleResult:
        return self._result("start")

    def stop(self) -> LifecycleResult:
        return self._result("stop")

    def restart(self) -> LifecycleResult:
        return self._result("restart")


def test_root_help_teaches_canonical_lifecycle_commands():
    result = CliRunner().invoke(voice_mode_main_cli, ["--help"])

    assert result.exit_code == 0
    assert "Use `voicemode start` for persistent hands-free conversation" in result.output
    assert all(command in result.output for command in ("start", "stop", "restart"))


def test_broker_help_preserves_diagnostic_commands_and_points_to_top_level():
    result = CliRunner().invoke(voice_mode_main_cli, ["broker", "--help"])

    assert result.exit_code == 0
    assert all(command in result.output for command in ("run", "status", "stop"))
    assert "voicemode start" in result.output
    assert "voicemode restart" in result.output


def test_top_level_lifecycle_commands_delegate_to_supervisor(monkeypatch):
    FakeSupervisor.calls = []
    monkeypatch.setattr(lifecycle, "BrokerSupervisor", FakeSupervisor)

    runner = CliRunner()
    results = [runner.invoke(voice_mode_main_cli, [action]) for action in ("start", "stop", "restart")]

    assert [result.exit_code for result in results] == [0, 0, 0]
    assert FakeSupervisor.calls == ["start", "stop", "restart"]
    assert results[0].output == "broker start requested\nState: starting\n"
    assert results[1].output == "broker stop requested\nState: stopped\n"


def test_json_output_is_one_deterministic_uncolored_stdout_record(monkeypatch):
    monkeypatch.setattr(lifecycle, "BrokerSupervisor", FakeSupervisor)

    result = CliRunner().invoke(
        voice_mode_main_cli,
        ["start", "--json"],
        env={"CI": "1", "NO_COLOR": "1"},
        color=True,
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.output == (
        '{"action": "start", "changed": true, '
        '"message": "broker start requested", '
        '"service_path": "/tmp/voicemode-broker.service", '
        '"state": "starting"}\n'
    )
    assert "\x1b[" not in result.output


def test_repeated_start_is_a_successful_noop(monkeypatch):
    class AlreadyRunning:
        def start(self):
            return LifecycleResult(
                "start",
                SupervisorState.RUNNING,
                False,
                Path("/tmp/service"),
                "broker is already running; existing owner retained",
            )

    monkeypatch.setattr(lifecycle, "BrokerSupervisor", AlreadyRunning)
    result = CliRunner().invoke(voice_mode_main_cli, ["start", "--json"])

    assert result.exit_code == 0
    assert '"changed": false' in result.output
    assert '"state": "running"' in result.output


def test_usage_errors_keep_click_exit_category():
    result = CliRunner().invoke(voice_mode_main_cli, ["start", "--unknown"])

    assert result.exit_code == lifecycle.LifecycleExit.USER_INPUT
    assert "No such option '--unknown'" in result.stderr


def test_environment_failure_uses_stable_stderr_and_exit_code(monkeypatch):
    class UnsupportedPlatform:
        def __init__(self):
            raise ValueError("unsupported service platform: Plan9")

    monkeypatch.setattr(lifecycle, "BrokerSupervisor", UnsupportedPlatform)
    result = CliRunner().invoke(voice_mode_main_cli, ["start", "--json"])

    assert result.exit_code == lifecycle.LifecycleExit.ENVIRONMENT_FAILURE
    assert result.stdout == ""
    assert result.stderr == (
        '{"action": "start", "error": "unsupported service platform: Plan9", '
        '"exit_category": "environment_failure", "exit_code": 4}\n'
    )


def test_upstream_failure_uses_stable_human_stderr(monkeypatch):
    class FailedManager:
        def restart(self):
            raise RuntimeError("systemctl failed: unavailable")

    monkeypatch.setattr(lifecycle, "BrokerSupervisor", FailedManager)
    result = CliRunner().invoke(voice_mode_main_cli, ["restart"])

    assert result.exit_code == lifecycle.LifecycleExit.UPSTREAM_FAILURE
    assert result.stdout == ""
    assert result.stderr == "Could not restart VoiceMode: systemctl failed: unavailable\n"


def test_common_typos_receive_an_exact_correction():
    runner = CliRunner()
    broker_typo = runner.invoke(voice_mode_main_cli, ["brocker"])
    daemon_alias = runner.invoke(voice_mode_main_cli, ["daemon"])

    assert broker_typo.exit_code == lifecycle.LifecycleExit.USER_INPUT
    assert "Did you mean 'voicemode broker'?" in broker_typo.stderr
    assert daemon_alias.exit_code == lifecycle.LifecycleExit.USER_INPUT
    assert "Did you mean 'voicemode start'?" in daemon_alias.stderr
