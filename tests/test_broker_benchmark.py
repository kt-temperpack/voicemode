import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark-broker-audio.py"
SPEC = importlib.util.spec_from_file_location("broker_audio_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def observations(**overrides):
    values = {
        "wake_ack": 100,
        "endpoint_delay": 800,
        "submission_state": 50,
        "host_dispatch": 100,
        "playback_cancel": 75,
        "device_reopen": 200,
        "first_tts_audio": 400,
    }
    values.update(overrides)
    records = [
        {"metric": name, "duration_ms": duration}
        for name, duration in values.items()
    ]
    records.append({"metric": "acoustic_barge_in", "supported": False})
    return records


def test_qualification_reports_percentiles_metadata_and_owner_soak():
    report = benchmark.qualify(observations(), soak_turns=100)

    assert report["passed"] is True
    assert report["metrics"]["endpoint_delay"] == {
        "budget_ms": 900,
        "count": 1,
        "max_ms": 800.0,
        "p50_ms": 800.0,
        "p95_ms": 800.0,
        "passed": True,
        "supported": True,
    }
    assert report["metrics"]["acoustic_barge_in"]["reason"] == (
        "unsupported_use_hotkey"
    )
    assert report["environment"]["platform"]
    assert report["soak"] == {
        "final_live_streams": 0,
        "max_live_streams": 1,
        "passed": True,
        "turns": 100,
    }


def test_missing_and_regressed_published_budgets_fail_qualification():
    missing = benchmark.qualify(
        [record for record in observations() if record["metric"] != "wake_ack"],
        soak_turns=1,
    )
    regressed = benchmark.qualify(
        observations(playback_cancel=150), soak_turns=1
    )

    assert missing["passed"] is False
    assert missing["metrics"]["wake_ack"]["reason"] == "missing"
    assert regressed["passed"] is False
    assert regressed["metrics"]["playback_cancel"]["p95_ms"] == 150


def test_nearest_rank_and_monotonic_boundary_validation():
    assert benchmark.percentile([1, 2, 3, 4, 100], 0.95) == 100
    assert benchmark.monotonic_duration_ms(1_000_000, 3_500_000) == 2.5
    with pytest.raises(benchmark.QualificationError, match="precedes"):
        benchmark.monotonic_duration_ms(2, 1)


@pytest.mark.parametrize(
    "record",
    [
        {"metric": "unknown", "duration_ms": 1},
        {"metric": "wake_ack", "duration_ms": -1},
        {"metric": "wake_ack", "duration_ms": "fast"},
        {"metric": "wake_ack", "supported": False},
    ],
)
def test_malformed_observations_fail_closed(record):
    with pytest.raises(benchmark.QualificationError):
        benchmark.qualify([record], soak_turns=1)
