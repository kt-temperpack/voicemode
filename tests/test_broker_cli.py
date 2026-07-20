from click.testing import CliRunner

from voice_mode.cli import voice_mode_main_cli
from voice_mode.cli_commands import broker as broker_module


def test_root_and_group_help_register_commands():
    runner = CliRunner()
    root = runner.invoke(voice_mode_main_cli, ["--help"])
    assert root.exit_code == 0
    assert "broker" in root.output
    group = runner.invoke(voice_mode_main_cli, ["broker", "--help"])
    assert group.exit_code == 0
    assert all(command in group.output for command in ("run", "converse", "status", "stop"))


def test_missing_status_has_stable_human_and_json_output(tmp_path):
    runner = CliRunner()
    path = str(tmp_path / "missing.sock")
    human = runner.invoke(voice_mode_main_cli, ["broker", "status", "--socket", path])
    assert human.exit_code == 1
    assert human.output == "Broker is not running\n"
    machine = runner.invoke(voice_mode_main_cli, ["broker", "status", "--json", "--socket", path])
    assert machine.exit_code == 1
    assert machine.output == '{"running": false}\n'


def test_missing_stop_is_idempotent(tmp_path):
    result = CliRunner().invoke(
        voice_mode_main_cli,
        ["broker", "stop", "--socket", str(tmp_path / "missing.sock")],
    )
    assert result.exit_code == 0
    assert result.output == "Broker is already stopped\n"


def test_live_status_rendering(monkeypatch):
    payload = {
        "kind": "status",
        "state": "engaged",
        "session": {"session_id": "session-123", "codex_session_id": "codex-12", "repo_root": "/repo"},
        "pending_turns": 0,
        "uptime_seconds": 2.5,
        "protocol_version": 1,
        "shutting_down": False,
    }
    monkeypatch.setattr(broker_module.BrokerClient, "status", lambda self: payload)
    human = CliRunner().invoke(voice_mode_main_cli, ["broker", "status"])
    assert human.exit_code == 0
    assert "Broker is running: engaged" in human.output
    assert "Repository: /repo" in human.output
    assert "Resume: codex resume codex-12" in human.output
    machine = CliRunner().invoke(voice_mode_main_cli, ["broker", "status", "--json"])
    assert machine.exit_code == 0
    assert '"shutting_down": false' in machine.output


def test_run_maps_bind_failure(monkeypatch, tmp_path):
    def fail(_path, **_kwargs):
        raise OSError("occupied")

    monkeypatch.setattr("voice_mode.broker.handsfree.run_handsfree_broker", fail)
    result = CliRunner().invoke(
        voice_mode_main_cli,
        ["broker", "run", "--socket", str(tmp_path / "broker.sock")],
    )
    assert result.exit_code == 1
    assert "Check the path and permissions" in result.output


def test_daemon_only_preserves_socket_only_mode(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(broker_module, "run_broker", lambda path: calls.append(path))
    result = CliRunner().invoke(
        voice_mode_main_cli,
        ["broker", "run", "--daemon-only", "--socket", str(tmp_path / "broker.sock")],
    )
    assert result.exit_code == 0
    assert calls == [tmp_path / "broker.sock"]


def test_converse_alias_starts_handsfree_loop(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "voice_mode.broker.handsfree.run_handsfree_broker",
        lambda path, **kwargs: calls.append((path, kwargs)),
    )
    result = CliRunner().invoke(
        voice_mode_main_cli,
        ["broker", "converse", "--repo", str(tmp_path), "--socket", str(tmp_path / "broker.sock")],
    )
    assert result.exit_code == 0
    assert calls[0][0] == tmp_path / "broker.sock"
    assert calls[0][1]["repo_root"] == tmp_path
