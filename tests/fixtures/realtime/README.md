# Realtime operator contract fixtures

These fixtures freeze the external wire assumptions used by the first
GPT-Realtime Codex operator slice. They are synthetic contract examples, not
captured traffic. Identifiers use an obvious `*_fixture_*` namespace, transcript
content begins with `[synthetic]`, timestamps are omitted or zero, and the files
contain no API key, capability, raw SDP, user path, or real conversation.

The OpenAI event names and required field shapes follow the official
GPT-Realtime 2.1 model, WebRTC, server-control, conversation, VAD,
transcription, and Realtime API reference pages checked on 2026-07-20:

- https://developers.openai.com/api/docs/models/gpt-realtime-2.1
- https://developers.openai.com/api/docs/guides/realtime-webrtc
- https://developers.openai.com/api/docs/guides/realtime-server-controls
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://developers.openai.com/api/docs/guides/realtime-vad
- https://developers.openai.com/api/docs/guides/realtime-transcription
- https://developers.openai.com/api/reference/resources/realtime
- https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/hangup

`openai-call-cases.json` records multipart field names, response metadata,
Location parsing cases, and hangup outcomes without storing offer or answer SDP.
`openai-server-events.json` records supported server events plus one unknown
additive event. Fields named `fixture_case` and `contract_source` are local test
metadata and never appear on the provider wire.

The app-server fixtures use the normalized notification and `thread/read`
shapes already exercised by VoiceMode's Codex adapter tests. The
`voicemode/transportLost` notification is a synthetic local extension emitted by
the adapter boundary; it is not a Codex app-server method.

Baseline verification was run twice before production implementation with:

```bash
uv run pytest -q --no-cov tests/test_broker_cli.py tests/test_broker_socket.py tests/test_broker_compatibility.py
```

Both runs completed with `32 passed`; the first recorded run took 6.83 seconds.
The fixtures themselves are checked with:

```bash
uv run pytest -q --no-cov tests/test_broker_realtime_contract_fixtures.py
```

Update a fixture only with the corresponding official contract link and checked
date, then inspect the complete diff. The safety test intentionally rejects
credential-like strings, non-synthetic `rtc_` identifiers, URL fragments, user
home paths, unlabeled transcript content, unstable timestamps, and oversized
payloads.
