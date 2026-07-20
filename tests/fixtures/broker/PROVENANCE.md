# Broker baseline fixtures

These synthetic fixtures freeze VoiceMode broker protocol v1 and the Codex
adapter's single-response contract before the daily-driver kernel refactor.

Generate them from the values in `tests/test_broker_baselines.py`, then review
the complete diff. Dynamic identifiers, timestamps, durations, home paths, and
credentials are deliberately absent. Update only when the corresponding public
contract changes intentionally.

Verification:

```bash
uv run pytest tests/test_broker_baselines.py tests/test_broker_*.py -q
```
