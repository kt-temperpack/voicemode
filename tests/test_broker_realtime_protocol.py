import json
from pathlib import Path

import pytest

from voice_mode.broker.realtime.protocol import (
    ConversationItemServerEvent,
    FunctionArgumentsDoneServerEvent,
    InputAudioBufferEvent,
    InputTranscriptServerEvent,
    OpenAIRealtimeCodec,
    OutputTranscriptServerEvent,
    RateLimitsUpdatedServerEvent,
    RealtimeProtocolError,
    RealtimeSessionConfig,
    ResponseCreatedServerEvent,
    ResponseDoneServerEvent,
    SessionServerEvent,
    StructuredErrorServerEvent,
    UnknownServerEvent,
    deterministic_event_id_factory,
)
from voice_mode.broker.realtime.types import (
    MAX_INSTRUCTION_TEXT_BYTES,
    MAX_REMEMBERED_IDS,
    RealtimeFunctionCallId,
    RealtimeItemId,
    RealtimeResponseId,
    WorkerDeliveryId,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "realtime"


def _fixture_document(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text())


def _codec() -> OpenAIRealtimeCodec:
    return OpenAIRealtimeCodec(
        session_config=RealtimeSessionConfig(
            instructions="[synthetic] bounded operator policy",
        ),
        event_id_factory=deterministic_event_id_factory(),
    )


def test_decode_official_server_fixture_inventory_into_typed_events():
    codec = _codec()
    document = _fixture_document("openai-server-events.json")
    by_case = {
        entry["fixture_case"]: codec.decode_server_event(json.dumps(entry["wire"]))
        for entry in document["events"]
    }

    assert isinstance(by_case["session_created"], SessionServerEvent)
    assert by_case["session_created"].config.model == "gpt-realtime-2.1"
    assert by_case["session_created"].config.output_speed == 1.25

    assert isinstance(by_case["speech_started"], InputAudioBufferEvent)
    assert by_case["speech_started"].kind == "speech_started"
    assert by_case["speech_started"].audio_ms == 0

    assert isinstance(by_case["speech_stopped"], InputAudioBufferEvent)
    assert by_case["speech_stopped"].audio_ms == 1200

    assert isinstance(by_case["audio_committed"], InputAudioBufferEvent)
    assert by_case["audio_committed"].item_id == RealtimeItemId("item_fixture_user_0001")

    assert isinstance(by_case["audio_cleared"], InputAudioBufferEvent)
    assert by_case["audio_cleared"].kind == "cleared"

    assert isinstance(by_case["conversation_item_added"], ConversationItemServerEvent)
    assert by_case["conversation_item_added"].transcript == "[synthetic] delegate the fixture task"

    assert isinstance(by_case["conversation_item_done"], ConversationItemServerEvent)
    assert by_case["conversation_item_done"].lifecycle == "done"

    assert isinstance(by_case["conversation_item_created_compatibility"], ConversationItemServerEvent)
    assert by_case["conversation_item_created_compatibility"].lifecycle == "created"

    assert isinstance(by_case["input_transcript_delta"], InputTranscriptServerEvent)
    assert by_case["input_transcript_delta"].final is False

    assert isinstance(by_case["input_transcript_completed"], InputTranscriptServerEvent)
    assert by_case["input_transcript_completed"].final is True

    assert isinstance(by_case["response_created"], ResponseCreatedServerEvent)
    assert by_case["response_created"].response_id == RealtimeResponseId("response_fixture_0001")

    assert isinstance(by_case["output_transcript_delta"], OutputTranscriptServerEvent)
    assert by_case["output_transcript_delta"].final is False

    assert isinstance(by_case["output_transcript_done"], OutputTranscriptServerEvent)
    assert by_case["output_transcript_done"].final is True

    assert isinstance(by_case["function_call_arguments_done"], FunctionArgumentsDoneServerEvent)
    assert by_case["function_call_arguments_done"].request.name == "delegate_codex"

    assert isinstance(by_case["response_done_completed_function_call"], ResponseDoneServerEvent)
    assert by_case["response_done_completed_function_call"].status == "completed"
    assert [call.request.name for call in by_case["response_done_completed_function_call"].function_calls] == [
        "delegate_codex"
    ]

    assert isinstance(by_case["response_done_cancelled"], ResponseDoneServerEvent)
    assert by_case["response_done_cancelled"].status == "cancelled"
    assert by_case["response_done_cancelled"].status_detail == "[synthetic] client cancellation"

    assert isinstance(by_case["rate_limits_updated"], RateLimitsUpdatedServerEvent)
    assert by_case["rate_limits_updated"].limits[0].remaining == 99

    assert isinstance(by_case["structured_error_correlated"], StructuredErrorServerEvent)
    assert by_case["structured_error_correlated"].correlated_event_id == "client_event_fixture_0001"

    assert isinstance(by_case["unknown_additive_event"], UnknownServerEvent)
    assert by_case["unknown_additive_event"].event_type == "realtime.fixture_future_event"


def test_decode_response_done_supports_multiple_completed_function_calls_in_stable_order():
    codec = _codec()
    raw = {
        "type": "response.done",
        "event_id": "event_fixture_multi_call",
        "response": {
            "id": "response_fixture_0009",
            "status": "completed",
            "status_details": None,
            "output": [
                {
                    "type": "message",
                    "id": "item_fixture_message_0001",
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "id": "item_fixture_function_0001",
                    "call_id": "function_call_fixture_0001",
                    "name": "delegate_codex",
                    "status": "completed",
                    "arguments": "{\"task\":\"[synthetic] first\"}",
                },
                {
                    "type": "function_call",
                    "id": "item_fixture_function_0002",
                    "call_id": "function_call_fixture_0002",
                    "name": "interrupt_codex",
                    "status": "completed",
                    "arguments": "{\"job_id\":\"job_fixture_0001\"}",
                },
            ],
        },
    }

    event = codec.decode_server_event(json.dumps(raw))

    assert isinstance(event, ResponseDoneServerEvent)
    assert [call.request.name for call in event.function_calls] == [
        "delegate_codex",
        "interrupt_codex",
    ]
    assert [call.request.call_id.to_public() for call in event.function_calls] == [
        "function_call_fixture_0001",
        "function_call_fixture_0002",
    ]


def test_decode_response_done_rejects_duplicate_call_ids_and_unknown_terminal_status():
    codec = _codec()
    duplicate = {
        "type": "response.done",
        "event_id": "event_fixture_duplicate_call",
        "response": {
            "id": "response_fixture_duplicate",
            "status": "completed",
            "status_details": None,
            "output": [
                {
                    "type": "function_call",
                    "id": "item_fixture_function_0001",
                    "call_id": "function_call_fixture_0001",
                    "name": "delegate_codex",
                    "status": "completed",
                    "arguments": "{\"task\":\"[synthetic] first\"}",
                },
                {
                    "type": "function_call",
                    "id": "item_fixture_function_0002",
                    "call_id": "function_call_fixture_0001",
                    "name": "interrupt_codex",
                    "status": "completed",
                    "arguments": "{\"job_id\":\"job_fixture_0001\"}",
                },
            ],
        },
    }
    unknown_status = {
        "type": "response.done",
        "event_id": "event_fixture_unknown_status",
        "response": {
            "id": "response_fixture_unknown",
            "status": "aborted",
            "status_details": None,
            "output": [],
        },
    }

    with pytest.raises(RealtimeProtocolError, match="duplicate function call IDs"):
        codec.decode_server_event(json.dumps(duplicate))

    with pytest.raises(RealtimeProtocolError, match="unsupported response terminal status"):
        codec.decode_server_event(json.dumps(unknown_status))


@pytest.mark.parametrize("status", ("cancelled", "failed", "incomplete"))
def test_non_completed_response_done_never_emits_executable_function_calls(status: str):
    codec = _codec()
    raw = {
        "type": "response.done",
        "event_id": f"event_fixture_{status}",
        "response": {
            "id": "response_fixture_terminal",
            "status": status,
            "status_details": {"reason": "[synthetic] terminal"},
            "output": [
                {
                    "type": "function_call",
                    "id": "item_fixture_function_0001",
                    "call_id": "function_call_fixture_0001",
                    "name": "delegate_codex",
                    "status": "completed",
                    "arguments": "{\"task\":\"[synthetic] should not execute\"}",
                }
            ],
        },
    }

    event = codec.decode_server_event(json.dumps(raw))

    assert isinstance(event, ResponseDoneServerEvent)
    assert event.function_calls == ()


@pytest.mark.parametrize(
    "raw",
    (
        {
            "type": "response.function_call_arguments.done",
            "event_id": "event_fixture_bad_tool_args",
            "response_id": "response_fixture_0001",
            "item_id": "item_fixture_function_0001",
            "output_index": 0,
            "call_id": "function_call_fixture_0001",
            "name": "delete_everything",
            "arguments": "{\"task\":\"[synthetic] invalid\"}",
        },
        {
            "type": "response.done",
            "event_id": "event_fixture_bad_tool_done",
            "response": {
                "id": "response_fixture_0001",
                "status": "completed",
                "status_details": None,
                "output": [
                    {
                        "type": "function_call",
                        "id": "item_fixture_function_0001",
                        "call_id": "function_call_fixture_0001",
                        "name": "delete_everything",
                        "status": "completed",
                        "arguments": "{\"task\":\"[synthetic] invalid\"}",
                    }
                ],
            },
        },
    ),
)
def test_unsupported_realtime_tool_names_are_rejected(raw: dict[str, object]):
    with pytest.raises(RealtimeProtocolError, match="unsupported realtime function tool"):
        _codec().decode_server_event(json.dumps(raw))


def test_decode_malformed_error_wrapper_and_oversized_payloads_fail_closed():
    codec = _codec()

    with pytest.raises(RealtimeProtocolError, match="error must be an object"):
        codec.decode_server_event(json.dumps({"type": "error", "event_id": "event_1", "error": "bad"}))

    deep = []
    for _ in range(20):
        deep = [deep]
    with pytest.raises(RealtimeProtocolError, match="nesting limit"):
        codec.decode_server_event(json.dumps({"type": "realtime.future", "event_id": "event_2", "deep": deep}))

    with pytest.raises(RealtimeProtocolError, match="byte limit"):
        codec.decode_server_event(b"x" * (MAX_INSTRUCTION_TEXT_BYTES * 20))

    too_long_transcript = {
        "type": "conversation.item.input_audio_transcription.completed",
        "event_id": "event_fixture_too_long_transcript",
        "item_id": "item_fixture_user_0001",
        "content_index": 0,
        "transcript": "x" * (MAX_INSTRUCTION_TEXT_BYTES + 1),
    }
    with pytest.raises(RealtimeProtocolError):
        codec.decode_server_event(json.dumps(too_long_transcript))


def test_client_event_builders_are_deterministic_and_correlate_errors():
    codec = _codec()

    session_update = codec.build_session_update(turn_detection_enabled=True)
    ptt_update = codec.build_session_update(turn_detection_enabled=False)
    response_create = codec.build_response_create(
        cause_item_id=RealtimeItemId("item_fixture_user_0001"),
        instructions="[synthetic] answer briefly",
    )
    response_cancel = codec.build_response_cancel(
        active_response_id=RealtimeResponseId("response_fixture_0001")
    )
    function_output = codec.build_function_output(
        call_id=RealtimeFunctionCallId("function_call_fixture_0001"),
        output={"status": "accepted", "job_id": "job_fixture_0001"},
    )
    worker_response = codec.build_worker_response(
        delivery_id=WorkerDeliveryId("delivery_fixture_0001"),
        worker_data={
            "job_id": "job_fixture_0001",
            "summary": "[synthetic] worker completed cleanly",
            "detail": "dangerous \"quotes\"\nnewlines are data",
        },
    )
    clear_event = codec.build_input_audio_clear()
    commit_event = codec.build_input_audio_commit()

    assert [event.event_id for event in (
        session_update,
        ptt_update,
        response_create,
        response_cancel,
        function_output,
        worker_response,
        clear_event,
        commit_event,
    )] == [
        "evt_fixture_0001",
        "evt_fixture_0002",
        "evt_fixture_0003",
        "evt_fixture_0004",
        "evt_fixture_0005",
        "evt_fixture_0006",
        "evt_fixture_0007",
        "evt_fixture_0008",
    ]

    assert session_update.payload == {
        "type": "session.update",
        "event_id": "evt_fixture_0001",
        "session": {
            "type": "realtime",
            "model": "gpt-realtime-2.1",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "language": "en",
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": False,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": "marin",
                    "speed": 1.25,
                },
            },
            "tools": codec.function_tools(),
            "tool_choice": "auto",
            "instructions": "[synthetic] bounded operator policy",
        },
    }
    assert ptt_update.payload["session"]["audio"]["input"]["turn_detection"] is None
    assert response_create.mutation == "response.create:item_fixture_user_0001"
    assert response_cancel.payload["response_id"] == "response_fixture_0001"
    assert function_output.payload["item"]["output"] == "{\"job_id\":\"job_fixture_0001\",\"status\":\"accepted\"}"
    assert worker_response.payload["response"]["conversation"] == "none"
    assert worker_response.payload["response"]["tool_choice"] == "none"
    assert worker_response.payload["response"]["tools"] == []
    worker_text = worker_response.payload["response"]["input"][0]["content"][0]["text"]
    assert worker_text.startswith("[worker_result]\n{\"detail\":\"dangerous")
    assert "\\\"quotes\\\"" in worker_text
    assert "\n[/worker_result]" in worker_text
    assert clear_event.payload == {"type": "input_audio_buffer.clear", "event_id": "evt_fixture_0007"}
    assert commit_event.payload == {"type": "input_audio_buffer.commit", "event_id": "evt_fixture_0008"}

    correlated_error = codec.decode_server_event(
        json.dumps(
            {
                "type": "error",
                "event_id": "event_fixture_error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_request_error",
                    "message": "[synthetic] rejected builder event",
                    "event_id": "evt_fixture_0003",
                    "param": None,
                },
            }
        )
    )
    assert isinstance(correlated_error, StructuredErrorServerEvent)
    assert correlated_error.correlated_mutation == "response.create:item_fixture_user_0001"


def test_decode_session_update_preserves_push_to_talk_state():
    codec = _codec()
    raw = _fixture_document("openai-server-events.json")["events"][0]["wire"]
    raw = json.loads(json.dumps(raw))
    raw["type"] = "session.updated"
    raw["event_id"] = "event_fixture_ptt"
    raw["session"]["audio"]["input"]["turn_detection"] = None

    event = codec.decode_server_event(json.dumps(raw))

    assert isinstance(event, SessionServerEvent)
    assert event.config.turn_detection_enabled is False


def test_initial_call_session_honors_push_to_talk_configuration():
    codec = OpenAIRealtimeCodec(
        session_config=RealtimeSessionConfig(turn_detection_enabled=False)
    )

    assert codec.build_call_session()["audio"]["input"]["turn_detection"] is None


def test_client_event_correlation_is_bounded_and_rejects_duplicate_ids():
    counter = iter([*(f"evt_{index}" for index in range(MAX_REMEMBERED_IDS + 1)), "evt_1"])
    codec = OpenAIRealtimeCodec(
        session_config=RealtimeSessionConfig(),
        event_id_factory=lambda: next(counter),
    )

    for _ in range(MAX_REMEMBERED_IDS + 1):
        codec.build_input_audio_clear()

    assert len(codec._client_mutations) == MAX_REMEMBERED_IDS
    assert "evt_0" not in codec._client_mutations
    with pytest.raises(ValueError, match="unique"):
        codec.build_input_audio_clear()


def test_worker_delivery_ids_are_typed_and_close_reasons_truncate_safely():
    codec = _codec()

    with pytest.raises(TypeError, match="WorkerDeliveryId"):
        codec.build_worker_response(delivery_id="delivery_fixture_0001", worker_data={})  # type: ignore[arg-type]

    closed = codec.bound_close_reason("ø" * 2048)
    assert len(closed.encode("utf-8")) <= 256
    assert closed.endswith("…")


def test_function_tool_schema_is_closed_and_exact():
    tools = _codec().function_tools()

    assert [tool["name"] for tool in tools] == [
        "delegate_codex",
        "get_codex_job",
        "steer_codex",
        "interrupt_codex",
    ]
    for tool in tools:
        parameters = tool["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert isinstance(parameters["required"], list)
        assert parameters["required"]
        for property_schema in parameters["properties"].values():
            assert property_schema["type"] == "string"
            assert property_schema["minLength"] == 1
            assert property_schema["maxLength"] >= 128


def test_close_reason_and_protocol_errors_are_secret_safe():
    codec = _codec()
    closed = codec.bound_close_reason(
        "Bearer supersecret sk-hidden rtc_private_fixture_001 because the session ended"
    )
    assert "supersecret" not in closed
    assert "sk-hidden" not in closed
    assert "rtc_private_fixture_001" not in closed
    assert len(closed.encode("utf-8")) <= 256

    bad = {
        "type": "response.function_call_arguments.done",
        "event_id": "event_fixture_secret_error",
        "response_id": "response_fixture_0001",
        "item_id": "item_fixture_function_0001",
        "output_index": 0,
        "call_id": "function_call_fixture_0001",
        "name": "delegate_codex",
        "arguments": "Bearer supersecret sk-hidden",
    }
    with pytest.raises(RealtimeProtocolError) as caught:
        codec.decode_server_event(json.dumps(bad))
    rendered = str(caught.value)
    assert "supersecret" not in rendered
    assert "sk-hidden" not in rendered
