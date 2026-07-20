"""Manual broker hardware qualification helpers.

Run only after collecting monotonic boundary observations from a warmed broker:

    uv run python scripts/benchmark-broker-audio.py --input /tmp/broker-latency.jsonl

This module deliberately contains no automatic microphone or speaker test. A
test runner must never seize the user's audio devices or persist ambient audio.
"""

from pathlib import Path


SCENARIOS = (
    "built-in microphone and speakers",
    "USB microphone",
    "wired headphones",
    "Bluetooth headset",
    "input device rotation while idle",
    "input device rotation while listening",
    "fan noise",
    "music or television in the room",
    "speaker playback with acoustic barge-in",
)


def qualification_command(observations: Path, report: Path) -> list[str]:
    return [
        "uv",
        "run",
        "python",
        "scripts/benchmark-broker-audio.py",
        "--input",
        str(observations),
        "--output",
        str(report),
    ]


def test_manual_matrix_is_complete():
    assert len(SCENARIOS) == 9
    assert any("Bluetooth" in scenario for scenario in SCENARIOS)
    assert any("device rotation" in scenario for scenario in SCENARIOS)
    assert any("barge-in" in scenario for scenario in SCENARIOS)
