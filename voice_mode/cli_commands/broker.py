"""CLI lifecycle commands for the experimental conversation broker."""

from __future__ import annotations

import json
from pathlib import Path

import click

from voice_mode.broker import BrokerError
from voice_mode.broker.client import BrokerClient, BrokerUnavailable
from voice_mode.broker.server import run_broker
from voice_mode.config import (
    BROKER_CODEX_EXECUTABLE,
    BROKER_CODEX_ADAPTER,
    BROKER_CODEX_MODEL,
    BROKER_CODEX_REASONING_EFFORT,
    BROKER_CODEX_SANDBOX,
    BROKER_CODEX_THREAD_ID,
    BROKER_LISTEN_DURATION_SECONDS,
    BROKER_MIN_LISTEN_DURATION_SECONDS,
    BROKER_SILENCE_THRESHOLD_MS,
    BROKER_SOCKET_PATH,
    BROKER_VOICE,
    BROKER_VOICE_SPEED,
    BROKER_WAKE_PHRASE,
)


@click.group(name="broker")
@click.help_option("-h", "--help", help="Show this message and exit")
def broker():
    """Run and inspect the experimental local conversation broker."""


def _socket_option(function):
    return click.option(
        "--socket",
        "socket_path",
        type=click.Path(path_type=Path),
        default=BROKER_SOCKET_PATH,
        show_default=True,
        help="Unix socket path.",
    )(function)


@broker.command("run")
@click.option(
    "--daemon-only",
    is_flag=True,
    help="Run only the local socket broker, without microphone or Codex.",
)
@click.option("--repo", "repo_root", type=click.Path(path_type=Path), default=Path.cwd, show_default="current directory")
@click.option("--wake-phrase", default=BROKER_WAKE_PHRASE, show_default=True)
@click.option("--voice", default=BROKER_VOICE, show_default=True)
@click.option("--listen-duration", type=float, default=BROKER_LISTEN_DURATION_SECONDS, show_default=True)
@click.option(
    "--adapter",
    "codex_adapter",
    type=click.Choice(["auto", "app-server", "exec"]),
    default=BROKER_CODEX_ADAPTER,
    show_default=True,
    help="Codex host adapter.",
)
@click.option(
    "--thread",
    "codex_thread_id",
    default=BROKER_CODEX_THREAD_ID,
    help="Exact Codex thread ID. Defaults to the calling Codex session when available.",
)
@click.option("--new-thread", is_flag=True, help="Create a separate Codex thread.")
@_socket_option
def broker_run(
    socket_path: Path,
    listen_duration: float,
    voice: str,
    wake_phrase: str,
    repo_root: Path,
    daemon_only: bool,
    codex_adapter: str,
    codex_thread_id: str | None,
    new_thread: bool,
):
    """Run hands-free Codex in the foreground."""
    try:
        if daemon_only:
            run_broker(socket_path)
        else:
            from voice_mode.broker.handsfree import run_handsfree_broker

            run_handsfree_broker(
                socket_path,
                repo_root=repo_root,
                wake_phrase=wake_phrase,
                voice=voice,
                voice_speed=BROKER_VOICE_SPEED,
                listen_duration=listen_duration,
                min_duration=BROKER_MIN_LISTEN_DURATION_SECONDS,
                codex_executable=BROKER_CODEX_EXECUTABLE,
                codex_sandbox=BROKER_CODEX_SANDBOX,
                codex_model=BROKER_CODEX_MODEL,
                codex_reasoning_effort=BROKER_CODEX_REASONING_EFFORT,
                silence_threshold_ms=BROKER_SILENCE_THRESHOLD_MS,
                codex_adapter=codex_adapter,
                codex_thread_id=codex_thread_id,
                new_thread=new_thread,
            )
    except (OSError, RuntimeError) as error:
        raise click.ClickException(
            f"could not start broker at {socket_path}: {error}. Check the path and permissions."
        ) from error


@broker.command("converse")
@click.option("--repo", "repo_root", type=click.Path(path_type=Path), default=Path.cwd, show_default="current directory")
@click.option("--wake-phrase", default=BROKER_WAKE_PHRASE, show_default=True)
@click.option("--voice", default=BROKER_VOICE, show_default=True)
@click.option("--listen-duration", type=float, default=BROKER_LISTEN_DURATION_SECONDS, show_default=True)
@click.option(
    "--adapter",
    "codex_adapter",
    type=click.Choice(["auto", "app-server", "exec"]),
    default=BROKER_CODEX_ADAPTER,
    show_default=True,
)
@click.option("--thread", "codex_thread_id", default=BROKER_CODEX_THREAD_ID)
@click.option("--new-thread", is_flag=True)
@_socket_option
def broker_converse(
    socket_path: Path,
    listen_duration: float,
    voice: str,
    wake_phrase: str,
    repo_root: Path,
    codex_adapter: str,
    codex_thread_id: str | None,
    new_thread: bool,
):
    """Explicit alias for the foreground hands-free Codex loop."""
    broker_run.callback(
        socket_path=socket_path,
        listen_duration=listen_duration,
        voice=voice,
        wake_phrase=wake_phrase,
        repo_root=repo_root,
        daemon_only=False,
        codex_adapter=codex_adapter,
        codex_thread_id=codex_thread_id,
        new_thread=new_thread,
    )


@broker.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@_socket_option
def broker_status(as_json: bool, socket_path: Path):
    """Show the broker's current state."""
    try:
        result = BrokerClient(socket_path).status()
    except BrokerUnavailable:
        if as_json:
            click.echo(json.dumps({"running": False}, sort_keys=True))
        else:
            click.echo("Broker is not running")
        raise click.exceptions.Exit(1)
    except BrokerError as error:
        raise click.ClickException(str(error)) from error

    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
        return
    session = result["session"]
    click.echo(f"Broker is running: {result['state']}")
    click.echo(f"Protocol: v{result['protocol_version']}  Uptime: {result['uptime_seconds']:.1f}s")
    click.echo(f"Pending turns: {result['pending_turns']}")
    if session:
        click.echo(
            f"Session: {session['session_id'][:8]}  Repository: {session['repo_root']}"
        )
        click.echo(f"Codex thread: {session['codex_session_id']}")
        if session["codex_session_id"] != "handsfree":
            click.echo(f"Resume: codex resume {session['codex_session_id']}")
    else:
        click.echo("Session: none")


@broker.command("stop")
@_socket_option
def broker_stop(socket_path: Path):
    """Gracefully stop a running broker."""
    try:
        BrokerClient(socket_path).stop()
    except BrokerUnavailable:
        click.echo("Broker is already stopped")
        return
    except BrokerError as error:
        raise click.ClickException(str(error)) from error
    click.echo("Broker is stopping")
