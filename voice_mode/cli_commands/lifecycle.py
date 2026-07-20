"""Top-level lifecycle commands for the supervised hands-free broker."""

from __future__ import annotations

import json
from enum import IntEnum

import click

from voice_mode.broker.supervisor import BrokerSupervisor, LifecycleResult


class LifecycleExit(IntEnum):
    """Stable process exit categories for people and automation."""

    SUCCESS = 0
    USER_INPUT = 2
    SAFETY_REFUSAL = 3
    ENVIRONMENT_FAILURE = 4
    UPSTREAM_FAILURE = 5
    CONFLICT = 6


class SafetyRefusal(RuntimeError):
    """Raised when an operation would violate a lifecycle safety invariant."""


class LifecycleConflict(RuntimeError):
    """Raised when another owner prevents the requested state transition."""


def _payload(result: LifecycleResult) -> dict[str, object]:
    return {
        "action": result.action,
        "changed": result.changed,
        "message": result.message,
        "service_path": str(result.service_path),
        "state": result.state.value,
    }


def _emit(result: LifecycleResult, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(_payload(result), sort_keys=True))
        return
    click.echo(result.message)
    click.echo(f"State: {result.state.value}")


def _emit_error(
    *, action: str, error: Exception, exit_code: LifecycleExit, as_json: bool
) -> None:
    message = str(error) or error.__class__.__name__
    if as_json:
        click.echo(
            json.dumps(
                {
                    "action": action,
                    "error": message,
                    "exit_category": exit_code.name.lower(),
                    "exit_code": int(exit_code),
                },
                sort_keys=True,
            ),
            err=True,
        )
    else:
        click.echo(f"Could not {action} VoiceMode: {message}", err=True)
    raise click.exceptions.Exit(int(exit_code))


def _run(action: str, *, as_json: bool) -> None:
    try:
        result = getattr(BrokerSupervisor(), action)()
    except SafetyRefusal as error:
        _emit_error(
            action=action,
            error=error,
            exit_code=LifecycleExit.SAFETY_REFUSAL,
            as_json=as_json,
        )
    except LifecycleConflict as error:
        _emit_error(
            action=action,
            error=error,
            exit_code=LifecycleExit.CONFLICT,
            as_json=as_json,
        )
    except (ValueError, OSError) as error:
        _emit_error(
            action=action,
            error=error,
            exit_code=LifecycleExit.ENVIRONMENT_FAILURE,
            as_json=as_json,
        )
    except RuntimeError as error:
        _emit_error(
            action=action,
            error=error,
            exit_code=LifecycleExit.UPSTREAM_FAILURE,
            as_json=as_json,
        )
    else:
        _emit(result, as_json=as_json)


def _json_option(function):
    return click.option(
        "--json",
        "as_json",
        is_flag=True,
        help="Emit one machine-readable JSON object.",
    )(function)


@click.command("start")
@_json_option
def start(as_json: bool) -> None:
    """Install if needed and start hands-free VoiceMode."""
    _run("start", as_json=as_json)


@click.command("stop")
@_json_option
def stop(as_json: bool) -> None:
    """Stop hands-free VoiceMode without deleting user data."""
    _run("stop", as_json=as_json)


@click.command("restart")
@_json_option
def restart(as_json: bool) -> None:
    """Restart the supervised hands-free VoiceMode service."""
    _run("restart", as_json=as_json)
