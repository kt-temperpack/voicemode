#!/usr/bin/env python3
"""Aggregate and qualify monotonic VoiceMode broker latency observations."""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Budget:
    limit_ms: float | None
    required: bool = True
    percentile: str = "p95_ms"


BUDGETS = {
    "wake_ack": Budget(250),
    "endpoint_delay": Budget(900),
    "submission_state": Budget(100),
    "host_dispatch": Budget(200),
    "playback_cancel": Budget(150),
    "acoustic_barge_in": Budget(400, required=False),
    "device_reopen": Budget(None),
    "first_tts_audio": Budget(600),
}


class QualificationError(ValueError):
    pass


def monotonic_duration_ms(start_ns: int, end_ns: int | None = None) -> float:
    """Convert one monotonic boundary pair into the observation wire format."""

    end_ns = time.perf_counter_ns() if end_ns is None else end_ns
    if end_ns < start_ns:
        raise QualificationError("monotonic end precedes start")
    return (end_ns - start_ns) / 1_000_000


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile, stable for small hardware sample sets."""

    if not samples:
        raise QualificationError("cannot summarize an empty sample set")
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def load_observations(path: Path | None) -> list[dict[str, Any]]:
    text = (path.read_text(encoding="utf-8") if path else sys.stdin.read()).strip()
    if not text:
        raise QualificationError("no observations supplied")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        records = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise QualificationError(
                    f"invalid JSON on line {line_number}: {error.msg}"
                ) from error
        return records
    if isinstance(decoded, list):
        return decoded
    if isinstance(decoded, dict) and isinstance(decoded.get("observations"), list):
        return decoded["observations"]
    if isinstance(decoded, dict):
        return [decoded]
    raise QualificationError("input must be a JSON object, array, or JSONL stream")


def normalize_observations(
    records: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], set[str]]:
    samples = {name: [] for name in BUDGETS}
    unsupported: set[str] = set()
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise QualificationError(f"observation {index} must be an object")
        metric = record.get("metric")
        if metric not in BUDGETS:
            raise QualificationError(f"observation {index} has unknown metric {metric!r}")
        if record.get("supported") is False:
            if BUDGETS[metric].required:
                raise QualificationError(f"required metric {metric} cannot be unsupported")
            unsupported.add(metric)
            continue
        duration = record.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise QualificationError(
                f"observation {index} requires numeric duration_ms"
            )
        duration = float(duration)
        if duration < 0 or not math.isfinite(duration):
            raise QualificationError(
                f"observation {index} has invalid duration_ms"
            )
        samples[metric].append(duration)
    return samples, unsupported


def run_audio_owner_soak(turns: int = 100) -> dict[str, int | bool]:
    """Exercise serialized reopen/close without touching real audio hardware."""

    from voice_mode.broker.audio_session import AudioSession

    live = 0
    maximum = 0

    class Stream:
        active = True

        def start(self):
            nonlocal live, maximum
            live += 1
            maximum = max(maximum, live)

        def stop(self):
            self.active = False

        def close(self):
            nonlocal live
            live -= 1

    session = AudioSession(input_factory=Stream, input_kwargs={})
    session.start()
    for _ in range(turns):
        session.reopen()
    session.close()
    return {
        "final_live_streams": live,
        "max_live_streams": maximum,
        "passed": live == 0 and maximum == 1,
        "turns": turns,
    }


def qualify(
    records: list[dict[str, Any]], *, soak_turns: int = 100
) -> dict[str, Any]:
    samples, unsupported = normalize_observations(records)
    metrics: dict[str, Any] = {}
    passed = True
    for name, budget in BUDGETS.items():
        values = samples[name]
        if not values:
            optional_unsupported = not budget.required and name in unsupported
            metric_passed = optional_unsupported
            metrics[name] = {
                "budget_ms": budget.limit_ms,
                "count": 0,
                "passed": metric_passed,
                "reason": "unsupported_use_hotkey" if optional_unsupported else "missing",
                "supported": False if optional_unsupported else None,
            }
            passed = passed and metric_passed
            continue
        summary = {
            "budget_ms": budget.limit_ms,
            "count": len(values),
            "max_ms": max(values),
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
            "supported": True,
        }
        observed = summary[budget.percentile]
        metric_passed = budget.limit_ms is None or observed < budget.limit_ms
        summary["passed"] = metric_passed
        metrics[name] = summary
        passed = passed and metric_passed

    soak = run_audio_owner_soak(soak_turns)
    passed = passed and bool(soak["passed"])
    return {
        "environment": {
            "machine": platform.machine(),
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python": platform.python_version(),
        },
        "metrics": metrics,
        "passed": passed,
        "schema_version": 1,
        "soak": soak,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qualify broker latency JSON without recording audio."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="JSON or JSONL observations; reads stdin when omitted",
    )
    parser.add_argument("--output", type=Path, help="also write the report here")
    parser.add_argument("--soak-turns", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.soak_turns < 1:
        print("error: --soak-turns must be positive", file=sys.stderr)
        return 2
    try:
        report = qualify(load_observations(args.input), soak_turns=args.soak_turns)
    except (OSError, QualificationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
