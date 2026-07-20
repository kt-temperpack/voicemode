"""Pure OpenAI realtime wire decoding and client-event builders."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from itertools import count
from typing import Any, Callable, Final, Mapping, Sequence
from uuid import uuid4

from .security import ensure_bounded_bytes, redact_public_error_text
from .types import (
    MAX_INSTRUCTION_TEXT_BYTES,
    MAX_REMEMBERED_IDS,
    MAX_REPOSITORY_STRING_BYTES,
    MAX_SIDEBAND_EVENT_BYTES,
    MAX_SIDEBAND_EVENT_DEPTH,
    MAX_TASK_TEXT_BYTES,
    MAX_THREAD_STRING_BYTES,
    MAX_TOOL_ARGUMENT_BYTES,
    RealtimeFunctionCallId,
    RealtimeItemId,
    RealtimeResponseId,
    RealtimeSessionId,
    ToolCallRequest,
    WorkerDeliveryId,
    validate_output_speed,
)

JSONDict = dict[str, Any]
_MAX_EVENT_COLLECTION_ITEMS: Final[int] = 128
_MAX_IDENTIFIER_BYTES: Final[int] = 128
_MAX_ERROR_DETAIL_BYTES: Final[int] = 1024
_MAX_CLOSE_REASON_BYTES: Final[int] = 256
_OUTPUT_AUDIO_FORMAT: Final[JSONDict] = {"type": "audio/pcm", "rate": 24000}
_INPUT_AUDIO_FORMAT: Final[JSONDict] = {"type": "audio/pcm", "rate": 24000}
_SUPPORTED_FUNCTIONS: Final[tuple[str, ...]] = (
    "delegate_codex",
    "get_codex_job",
    "steer_codex",
    "interrupt_codex",
)
_RESPONSE_DONE_STATUSES: Final[frozenset[str]] = frozenset(
    {"completed", "cancelled", "failed", "incomplete"}
)
_FUNCTION_CALL_ITEM_TYPE: Final[str] = "function_call"
_FUNCTION_CALL_OUTPUT_ITEM_TYPE: Final[str] = "function_call_output"
_MESSAGE_ITEM_TYPE: Final[str] = "message"


class RealtimeProtocolError(ValueError):
    """Typed protocol error that never includes raw payload data."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        event_type: str | None = None,
        event_id: str | None = None,
    ) -> None:
        self.code = code
        self.event_type = event_type
        self.event_id = event_id
        super().__init__(message)


@dataclass(frozen=True)
class RealtimeSessionConfig:
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    output_speed: float = 1.25
    transcription_model: str = "gpt-4o-mini-transcribe"
    language: str | None = "en"
    instructions: str = "You are the voice operator for Codex. Be brief, conversational, and accurate."
    vad_eagerness: str = "low"
    turn_detection_enabled: bool = True

    def __post_init__(self) -> None:
        ensure_bounded_bytes(self.model, label="model", max_bytes=128)
        ensure_bounded_bytes(self.voice, label="voice", max_bytes=64)
        ensure_bounded_bytes(
            self.transcription_model,
            label="transcription model",
            max_bytes=128,
        )
        if self.language is not None:
            ensure_bounded_bytes(self.language, label="language", max_bytes=32)
        ensure_bounded_bytes(
            self.instructions,
            label="instructions",
            max_bytes=MAX_INSTRUCTION_TEXT_BYTES,
        )
        ensure_bounded_bytes(self.vad_eagerness, label="vad eagerness", max_bytes=32)
        if self.vad_eagerness not in {"low", "medium", "high", "auto"}:
            raise ValueError("vad eagerness must be a documented semantic-VAD setting")
        if not isinstance(self.turn_detection_enabled, bool):
            raise TypeError("turn_detection_enabled must be a boolean")
        object.__setattr__(self, "output_speed", validate_output_speed(self.output_speed))


@dataclass(frozen=True)
class BuiltClientEvent:
    event_id: str
    mutation: str
    payload: JSONDict


@dataclass(frozen=True)
class FunctionCallOutput:
    request: ToolCallRequest


@dataclass(frozen=True)
class SessionServerEvent:
    event_id: str
    kind: str
    session_id: RealtimeSessionId
    config: RealtimeSessionConfig


@dataclass(frozen=True)
class InputAudioBufferEvent:
    event_id: str
    kind: str
    item_id: RealtimeItemId | None = None
    previous_item_id: RealtimeItemId | None = None
    audio_ms: int | None = None


@dataclass(frozen=True)
class ConversationItemServerEvent:
    event_id: str
    lifecycle: str
    item_id: RealtimeItemId
    item_type: str
    status: str | None
    role: str | None
    previous_item_id: RealtimeItemId | None
    transcript: str | None = None
    output_json: str | None = None
    call_id: RealtimeFunctionCallId | None = None


@dataclass(frozen=True)
class InputTranscriptServerEvent:
    event_id: str
    item_id: RealtimeItemId
    content_index: int
    text: str
    final: bool


@dataclass(frozen=True)
class ResponseCreatedServerEvent:
    event_id: str
    response_id: RealtimeResponseId


@dataclass(frozen=True)
class OutputTranscriptServerEvent:
    event_id: str
    response_id: RealtimeResponseId
    item_id: RealtimeItemId
    output_index: int
    content_index: int
    text: str
    final: bool


@dataclass(frozen=True)
class FunctionArgumentsDoneServerEvent:
    event_id: str
    response_id: RealtimeResponseId
    item_id: RealtimeItemId
    output_index: int
    request: ToolCallRequest


@dataclass(frozen=True)
class ResponseDoneServerEvent:
    event_id: str
    response_id: RealtimeResponseId
    status: str
    status_detail: str | None
    function_calls: tuple[FunctionCallOutput, ...]


@dataclass(frozen=True)
class RateLimitState:
    name: str
    remaining: int
    limit: int
    reset_seconds: float


@dataclass(frozen=True)
class RateLimitsUpdatedServerEvent:
    event_id: str
    limits: tuple[RateLimitState, ...]


@dataclass(frozen=True)
class StructuredErrorServerEvent:
    event_id: str
    code: str
    message: str
    error_type: str
    correlated_event_id: str | None
    correlated_mutation: str | None


@dataclass(frozen=True)
class UnknownServerEvent:
    event_id: str | None
    event_type: str


ServerEvent = (
    SessionServerEvent
    | InputAudioBufferEvent
    | ConversationItemServerEvent
    | InputTranscriptServerEvent
    | ResponseCreatedServerEvent
    | OutputTranscriptServerEvent
    | FunctionArgumentsDoneServerEvent
    | ResponseDoneServerEvent
    | RateLimitsUpdatedServerEvent
    | StructuredErrorServerEvent
    | UnknownServerEvent
)


class OpenAIRealtimeCodec:
    """Strict OpenAI realtime protocol boundary."""

    def __init__(
        self,
        *,
        session_config: RealtimeSessionConfig,
        event_id_factory: Callable[[], str] | None = None,
        max_event_bytes: int = MAX_SIDEBAND_EVENT_BYTES,
        max_event_depth: int = MAX_SIDEBAND_EVENT_DEPTH,
    ) -> None:
        self.session_config = session_config
        self._event_id_factory = event_id_factory or self._default_event_id_factory
        self._max_event_bytes = max_event_bytes
        self._max_event_depth = max_event_depth
        self._client_mutations: dict[str, str] = {}
        self._client_mutation_order: deque[str] = deque()

    def build_call_session(self) -> JSONDict:
        return self._session_document(turn_detection_enabled=True)

    def build_session_update(self, *, turn_detection_enabled: bool) -> BuiltClientEvent:
        session = self._session_document(turn_detection_enabled=turn_detection_enabled)
        event_id = self._new_event_id()
        mutation = (
            "session.update:hands_free"
            if turn_detection_enabled
            else "session.update:push_to_talk"
        )
        return self._register_event(
            event_id=event_id,
            mutation=mutation,
            payload={
                "type": "session.update",
                "event_id": event_id,
                "session": session,
            },
        )

    def build_response_create(
        self,
        *,
        cause_item_id: RealtimeItemId,
        instructions: str | None = None,
    ) -> BuiltClientEvent:
        response: JSONDict = {
            "tools": self.function_tools(),
            "tool_choice": "auto",
        }
        if instructions is not None:
            response["instructions"] = ensure_bounded_bytes(
                instructions,
                label="response instructions",
                max_bytes=MAX_INSTRUCTION_TEXT_BYTES,
            )
        event_id = self._new_event_id()
        return self._register_event(
            event_id=event_id,
            mutation=f"response.create:{cause_item_id.to_public()}",
            payload={
                "type": "response.create",
                "event_id": event_id,
                "response": response,
            },
        )

    def build_response_cancel(
        self,
        *,
        active_response_id: RealtimeResponseId | None,
    ) -> BuiltClientEvent:
        event_id = self._new_event_id()
        payload: JSONDict = {
            "type": "response.cancel",
            "event_id": event_id,
        }
        mutation = "response.cancel"
        if active_response_id is not None:
            payload["response_id"] = active_response_id.to_public()
            mutation = f"response.cancel:{active_response_id.to_public()}"
        return self._register_event(event_id=event_id, mutation=mutation, payload=payload)

    def build_function_output(
        self,
        *,
        call_id: RealtimeFunctionCallId,
        output: Mapping[str, Any],
    ) -> BuiltClientEvent:
        canonical_output = self._canonical_json(_json_object(output, label="function output"))
        event_id = self._new_event_id()
        return self._register_event(
            event_id=event_id,
            mutation=f"function_output:{call_id.to_public()}",
            payload={
                "type": "conversation.item.create",
                "event_id": event_id,
                "item": {
                    "type": _FUNCTION_CALL_OUTPUT_ITEM_TYPE,
                    "call_id": call_id.to_public(),
                    "output": canonical_output,
                },
            },
        )

    def build_worker_response(
        self,
        *,
        delivery_id: WorkerDeliveryId,
        worker_data: Mapping[str, Any],
    ) -> BuiltClientEvent:
        if not isinstance(delivery_id, WorkerDeliveryId):
            raise TypeError("delivery_id must be a WorkerDeliveryId")
        public_delivery_id = delivery_id.to_public()
        canonical_data = self._canonical_json(_json_object(worker_data, label="worker data"))
        bounded_text = ensure_bounded_bytes(
            f"[worker_result]\n{canonical_data}\n[/worker_result]",
            label="worker response input",
            max_bytes=MAX_INSTRUCTION_TEXT_BYTES,
        )
        event_id = self._new_event_id()
        return self._register_event(
            event_id=event_id,
            mutation=f"worker_response:{public_delivery_id}",
            payload={
                "type": "response.create",
                "event_id": event_id,
                "response": {
                    "conversation": "none",
                    "tool_choice": "none",
                    "tools": [],
                    "input": [
                        {
                            "type": _MESSAGE_ITEM_TYPE,
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": bounded_text,
                                }
                            ],
                        }
                    ],
                },
            },
        )

    def build_input_audio_clear(self) -> BuiltClientEvent:
        event_id = self._new_event_id()
        return self._register_event(
            event_id=event_id,
            mutation="input_audio_buffer.clear",
            payload={"type": "input_audio_buffer.clear", "event_id": event_id},
        )

    def build_input_audio_commit(self) -> BuiltClientEvent:
        event_id = self._new_event_id()
        return self._register_event(
            event_id=event_id,
            mutation="input_audio_buffer.commit",
            payload={"type": "input_audio_buffer.commit", "event_id": event_id},
        )

    def bound_close_reason(self, reason: str) -> str:
        if not isinstance(reason, str):
            raise TypeError("close reason must be a string")
        return _truncate_utf8(redact_public_error_text(reason), _MAX_CLOSE_REASON_BYTES)

    def function_tools(self) -> list[JSONDict]:
        return [
            self._tool_definition(
                name="delegate_codex",
                description=(
                    "Start a background Codex job for the user's request and return as soon as "
                    "the job is accepted or refused."
                ),
                required=("task",),
                properties={
                    "task": self._string_schema(
                        "The bounded task to delegate to Codex.",
                        MAX_TASK_TEXT_BYTES,
                    ),
                    "repo_root": self._string_schema(
                        "Optional repository hint resolved against the configured allowlist.",
                        MAX_REPOSITORY_STRING_BYTES,
                    ),
                    "thread_id": self._string_schema(
                        "Optional existing Codex thread hint to continue instead of starting a new thread.",
                        MAX_THREAD_STRING_BYTES,
                    ),
                    "client_request_id": self._string_schema(
                        "Optional idempotency key chosen by the model for safe retries.",
                        _MAX_IDENTIFIER_BYTES,
                    ),
                },
            ),
            self._tool_definition(
                name="get_codex_job",
                description="Read the current state of a known Codex job without changing it.",
                required=("job_id",),
                properties={
                    "job_id": self._string_schema(
                        "The Codex job identifier returned by delegate_codex.",
                        _MAX_IDENTIFIER_BYTES,
                    )
                },
            ),
            self._tool_definition(
                name="steer_codex",
                description=(
                    "Send a new instruction to a running Codex job without interrupting the "
                    "operator's own spoken response."
                ),
                required=("job_id", "instruction"),
                properties={
                    "job_id": self._string_schema(
                        "The Codex job identifier to steer.",
                        _MAX_IDENTIFIER_BYTES,
                    ),
                    "instruction": self._string_schema(
                        "The bounded steering instruction for the running job.",
                        MAX_INSTRUCTION_TEXT_BYTES,
                    ),
                    "client_request_id": self._string_schema(
                        "Optional idempotency key chosen by the model for safe retries.",
                        _MAX_IDENTIFIER_BYTES,
                    ),
                },
            ),
            self._tool_definition(
                name="interrupt_codex",
                description=(
                    "Interrupt a background Codex job. This stops the job itself, not the "
                    "operator's current spoken audio response."
                ),
                required=("job_id",),
                properties={
                    "job_id": self._string_schema(
                        "The Codex job identifier to interrupt.",
                        _MAX_IDENTIFIER_BYTES,
                    ),
                    "client_request_id": self._string_schema(
                        "Optional idempotency key chosen by the model for safe retries.",
                        _MAX_IDENTIFIER_BYTES,
                    ),
                },
            ),
        ]

    def decode_server_event(self, raw_event: bytes | str) -> ServerEvent:
        document = self._decode_json_document(raw_event)
        event_type = self._require_string(document, "type", label="event type")
        event_id = self._optional_string(document, "event_id", label="event ID")

        if event_type in {"session.created", "session.updated"}:
            return self._decode_session_event(document, event_type=event_type, event_id=event_id)
        if event_type == "input_audio_buffer.speech_started":
            return InputAudioBufferEvent(
                event_id=self._require_present(event_id, "event ID"),
                kind="speech_started",
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                audio_ms=self._require_int(document, "audio_start_ms", label="audio start"),
            )
        if event_type == "input_audio_buffer.speech_stopped":
            return InputAudioBufferEvent(
                event_id=self._require_present(event_id, "event ID"),
                kind="speech_stopped",
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                audio_ms=self._require_int(document, "audio_end_ms", label="audio end"),
            )
        if event_type == "input_audio_buffer.committed":
            return InputAudioBufferEvent(
                event_id=self._require_present(event_id, "event ID"),
                kind="committed",
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                previous_item_id=self._optional_identifier(
                    document,
                    "previous_item_id",
                    RealtimeItemId,
                ),
            )
        if event_type == "input_audio_buffer.cleared":
            return InputAudioBufferEvent(
                event_id=self._require_present(event_id, "event ID"),
                kind="cleared",
            )
        if event_type in {
            "conversation.item.added",
            "conversation.item.done",
            "conversation.item.created",
        }:
            return self._decode_conversation_item_event(
                document,
                lifecycle=event_type.rsplit(".", 1)[1],
                event_id=self._require_present(event_id, "event ID"),
            )
        if event_type == "conversation.item.input_audio_transcription.delta":
            return InputTranscriptServerEvent(
                event_id=self._require_present(event_id, "event ID"),
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                content_index=self._require_int(document, "content_index", label="content index"),
                text=self._bounded_string(document, "delta", label="input transcript delta"),
                final=False,
            )
        if event_type == "conversation.item.input_audio_transcription.completed":
            return InputTranscriptServerEvent(
                event_id=self._require_present(event_id, "event ID"),
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                content_index=self._require_int(document, "content_index", label="content index"),
                text=self._bounded_string(
                    document,
                    "transcript",
                    label="input transcript",
                ),
                final=True,
            )
        if event_type == "response.created":
            response = self._object(document, "response", label="response")
            return ResponseCreatedServerEvent(
                event_id=self._require_present(event_id, "event ID"),
                response_id=self._identifier(response, "id", RealtimeResponseId),
            )
        if event_type == "response.output_audio_transcript.delta":
            return OutputTranscriptServerEvent(
                event_id=self._require_present(event_id, "event ID"),
                response_id=self._identifier(document, "response_id", RealtimeResponseId),
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                output_index=self._require_int(document, "output_index", label="output index"),
                content_index=self._require_int(document, "content_index", label="content index"),
                text=self._bounded_string(
                    document,
                    "delta",
                    label="output transcript delta",
                ),
                final=False,
            )
        if event_type == "response.output_audio_transcript.done":
            return OutputTranscriptServerEvent(
                event_id=self._require_present(event_id, "event ID"),
                response_id=self._identifier(document, "response_id", RealtimeResponseId),
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                output_index=self._require_int(document, "output_index", label="output index"),
                content_index=self._require_int(document, "content_index", label="content index"),
                text=self._bounded_string(document, "transcript", label="output transcript"),
                final=True,
            )
        if event_type == "response.function_call_arguments.done":
            return self._decode_function_arguments_done(document, event_id=event_id)
        if event_type == "response.done":
            return self._decode_response_done(document, event_id=event_id)
        if event_type == "rate_limits.updated":
            return self._decode_rate_limits(document, event_id=event_id)
        if event_type == "error":
            return self._decode_structured_error(document, event_id=event_id)
        return UnknownServerEvent(event_id=event_id, event_type=event_type)

    def _decode_session_event(
        self,
        document: JSONDict,
        *,
        event_type: str,
        event_id: str | None,
    ) -> SessionServerEvent:
        session = self._object(document, "session", label="session")
        output_modalities = self._list(session, "output_modalities", label="output modalities")
        if output_modalities != ["audio"]:
            raise self._error(
                "invalid_event",
                "session output modalities must be ['audio']",
                event_type=event_type,
                event_id=event_id,
            )
        audio = self._object(session, "audio", label="session audio")
        input_audio = self._object(audio, "input", label="session input audio")
        output_audio = self._object(audio, "output", label="session output audio")
        transcription = self._object(
            input_audio,
            "transcription",
            label="session transcription",
        )
        turn_detection_raw = input_audio.get("turn_detection", "__missing__")
        if turn_detection_raw == "__missing__":
            raise self._error(
                "invalid_event",
                "session turn_detection is required",
                event_type=event_type,
                event_id=event_id,
            )
        if turn_detection_raw is None:
            vad_eagerness = self.session_config.vad_eagerness
            turn_detection_enabled = False
        else:
            turn_detection = self._object(
                input_audio,
                "turn_detection",
                label="session turn detection",
            )
            if self._require_string(turn_detection, "type", label="turn detection type") != "semantic_vad":
                raise self._error(
                    "invalid_event",
                    "turn detection type must be semantic_vad",
                    event_type=event_type,
                    event_id=event_id,
                )
            if self._require_bool(turn_detection, "create_response") is not False:
                raise self._error(
                    "invalid_event",
                    "turn detection create_response must be false",
                    event_type=event_type,
                    event_id=event_id,
                )
            if self._require_bool(turn_detection, "interrupt_response") is not True:
                raise self._error(
                    "invalid_event",
                    "turn detection interrupt_response must be true",
                    event_type=event_type,
                    event_id=event_id,
                )
            vad_eagerness = self._require_string(
                turn_detection,
                "eagerness",
                label="turn detection eagerness",
            )
            turn_detection_enabled = True
        config = RealtimeSessionConfig(
            model=self._require_string(session, "model", label="session model"),
            voice=self._require_string(output_audio, "voice", label="session voice"),
            output_speed=float(self._require_number(output_audio, "speed", label="session output speed")),
            transcription_model=self._require_string(
                transcription,
                "model",
                label="transcription model",
            ),
            language=self._optional_string(transcription, "language", label="transcription language"),
            instructions=self.session_config.instructions,
            vad_eagerness=vad_eagerness,
            turn_detection_enabled=turn_detection_enabled,
        )
        return SessionServerEvent(
            event_id=self._require_present(event_id, "event ID"),
            kind="created" if event_type == "session.created" else "updated",
            session_id=self._identifier(session, "id", RealtimeSessionId),
            config=config,
        )

    def _decode_conversation_item_event(
        self,
        document: JSONDict,
        *,
        lifecycle: str,
        event_id: str,
    ) -> ConversationItemServerEvent:
        item = self._object(document, "item", label="conversation item")
        item_type = self._require_string(item, "type", label="item type")
        status = self._optional_string(item, "status", label="item status")
        role = self._optional_string(item, "role", label="item role")
        transcript: str | None = None
        output_json: str | None = None
        call_id: RealtimeFunctionCallId | None = None

        if item_type == _MESSAGE_ITEM_TYPE:
            transcript = self._extract_message_transcript(item)
        elif item_type == _FUNCTION_CALL_OUTPUT_ITEM_TYPE:
            call_id = self._identifier(item, "call_id", RealtimeFunctionCallId)
            output_json = self._bounded_string(item, "output", label="function output payload")
        elif item_type == _FUNCTION_CALL_ITEM_TYPE:
            call_id = self._identifier(item, "call_id", RealtimeFunctionCallId)

        return ConversationItemServerEvent(
            event_id=event_id,
            lifecycle=lifecycle,
            item_id=self._identifier(item, "id", RealtimeItemId),
            item_type=item_type,
            status=status,
            role=role,
            previous_item_id=self._optional_identifier(document, "previous_item_id", RealtimeItemId),
            transcript=transcript,
            output_json=output_json,
            call_id=call_id,
        )

    def _decode_function_arguments_done(
        self,
        document: JSONDict,
        *,
        event_id: str | None,
    ) -> FunctionArgumentsDoneServerEvent:
        name = self._require_string(document, "name", label="tool name")
        self._require_supported_tool_name(name, event_type="response.function_call_arguments.done", event_id=event_id)
        arguments_json = self._bounded_string(document, "arguments", label="function arguments")
        self._validate_tool_arguments(arguments_json)
        return FunctionArgumentsDoneServerEvent(
            event_id=self._require_present(event_id, "event ID"),
            response_id=self._identifier(document, "response_id", RealtimeResponseId),
            item_id=self._identifier(document, "item_id", RealtimeItemId),
            output_index=self._require_int(document, "output_index", label="output index"),
            request=ToolCallRequest(
                call_id=self._identifier(document, "call_id", RealtimeFunctionCallId),
                item_id=self._identifier(document, "item_id", RealtimeItemId),
                name=name,
                arguments_json=arguments_json,
            ),
        )

    def _decode_response_done(
        self,
        document: JSONDict,
        *,
        event_id: str | None,
    ) -> ResponseDoneServerEvent:
        response = self._object(document, "response", label="response")
        status = self._require_string(response, "status", label="response status")
        if status not in _RESPONSE_DONE_STATUSES:
            raise self._error(
                "invalid_event",
                f"unsupported response terminal status {status!r}",
                event_type="response.done",
                event_id=event_id,
            )
        status_detail = self._extract_status_detail(response.get("status_details"))
        function_calls: list[FunctionCallOutput] = []
        seen_call_ids: set[str] = set()
        outputs = self._list(response, "output", label="response output")
        if status == "completed":
            for output in outputs:
                if not isinstance(output, dict):
                    raise self._error(
                        "invalid_event",
                        "response output items must be objects",
                        event_type="response.done",
                        event_id=event_id,
                    )
                if output.get("type") != _FUNCTION_CALL_ITEM_TYPE:
                    continue
                name = self._require_string(output, "name", label="tool name")
                self._require_supported_tool_name(name, event_type="response.done", event_id=event_id)
                if self._require_string(output, "status", label="function item status") != "completed":
                    continue
                call_id = self._identifier(output, "call_id", RealtimeFunctionCallId)
                if call_id.to_public() in seen_call_ids:
                    raise self._error(
                        "invalid_event",
                        "response.done contains duplicate function call IDs",
                        event_type="response.done",
                        event_id=event_id,
                    )
                seen_call_ids.add(call_id.to_public())
                arguments_json = self._bounded_string(
                    output,
                    "arguments",
                    label="function arguments",
                )
                self._validate_tool_arguments(arguments_json)
                function_calls.append(
                    FunctionCallOutput(
                        request=ToolCallRequest(
                            call_id=call_id,
                            item_id=self._identifier(output, "id", RealtimeItemId),
                            name=name,
                            arguments_json=arguments_json,
                        )
                    )
                )
        return ResponseDoneServerEvent(
            event_id=self._require_present(event_id, "event ID"),
            response_id=self._identifier(response, "id", RealtimeResponseId),
            status=status,
            status_detail=status_detail,
            function_calls=tuple(function_calls),
        )

    def _decode_rate_limits(
        self,
        document: JSONDict,
        *,
        event_id: str | None,
    ) -> RateLimitsUpdatedServerEvent:
        entries = self._list(document, "rate_limits", label="rate limits")
        limits: list[RateLimitState] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise self._error(
                    "invalid_event",
                    "rate limit entries must be objects",
                    event_type="rate_limits.updated",
                    event_id=event_id,
                )
            limits.append(
                RateLimitState(
                    name=self._require_string(entry, "name", label="rate limit name"),
                    remaining=self._require_int(entry, "remaining", label="rate limit remaining"),
                    limit=self._require_int(entry, "limit", label="rate limit limit"),
                    reset_seconds=float(
                        self._require_number(
                            entry,
                            "reset_seconds",
                            label="rate limit reset_seconds",
                        )
                    ),
                )
            )
        return RateLimitsUpdatedServerEvent(
            event_id=self._require_present(event_id, "event ID"),
            limits=tuple(limits),
        )

    def _decode_structured_error(
        self,
        document: JSONDict,
        *,
        event_id: str | None,
    ) -> StructuredErrorServerEvent:
        error = self._object(document, "error", label="error")
        correlated_event_id = self._optional_string(error, "event_id", label="correlated event ID")
        return StructuredErrorServerEvent(
            event_id=self._require_present(event_id, "event ID"),
            code=self._require_string(error, "code", label="error code"),
            message=self._bounded_string(error, "message", label="error message"),
            error_type=self._require_string(error, "type", label="error type"),
            correlated_event_id=correlated_event_id,
            correlated_mutation=(
                self._client_mutations.get(correlated_event_id)
                if correlated_event_id is not None
                else None
            ),
        )

    def _session_document(self, *, turn_detection_enabled: bool) -> JSONDict:
        transcription: JSONDict = {"model": self.session_config.transcription_model}
        if self.session_config.language is not None:
            transcription["language"] = self.session_config.language
        session: JSONDict = {
            "type": "realtime",
            "model": self.session_config.model,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": dict(_INPUT_AUDIO_FORMAT),
                    "transcription": transcription,
                    "turn_detection": (
                        self._turn_detection_document() if turn_detection_enabled else None
                    ),
                },
                "output": {
                    "format": dict(_OUTPUT_AUDIO_FORMAT),
                    "voice": self.session_config.voice,
                    "speed": self.session_config.output_speed,
                },
            },
            "tools": self.function_tools(),
            "tool_choice": "auto",
            "instructions": self.session_config.instructions,
        }
        return session

    def _turn_detection_document(self) -> JSONDict:
        return {
            "type": "semantic_vad",
            "eagerness": self.session_config.vad_eagerness,
            "create_response": False,
            "interrupt_response": True,
        }

    def _decode_json_document(self, raw_event: bytes | str) -> JSONDict:
        raw_bytes = raw_event.encode("utf-8") if isinstance(raw_event, str) else raw_event
        if not isinstance(raw_bytes, bytes):
            raise self._error("invalid_json", "realtime event must be bytes or text")
        if len(raw_bytes) > self._max_event_bytes:
            raise self._error("event_too_large", "realtime event exceeds the byte limit")
        try:
            document = json.loads(raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error("invalid_json", "realtime event is not valid JSON") from exc
        if not isinstance(document, dict):
            raise self._error("invalid_json", "realtime event root must be an object")
        try:
            self._validate_shape(document, depth=0)
        except (TypeError, ValueError) as exc:
            raise self._error("invalid_json", str(exc)) from exc
        return document

    def _validate_shape(self, value: Any, *, depth: int) -> None:
        if depth > self._max_event_depth:
            raise self._error("invalid_json", "realtime event exceeds the nesting limit")
        if isinstance(value, dict):
            if len(value) > _MAX_EVENT_COLLECTION_ITEMS:
                raise self._error("invalid_json", "realtime event object exceeds the item limit")
            for key, child in value.items():
                if not isinstance(key, str):
                    raise self._error("invalid_json", "realtime event keys must be strings")
                ensure_bounded_bytes(key, label="event key", max_bytes=128)
                self._validate_shape(child, depth=depth + 1)
            return
        if isinstance(value, list):
            if len(value) > _MAX_EVENT_COLLECTION_ITEMS:
                raise self._error("invalid_json", "realtime event list exceeds the item limit")
            for child in value:
                self._validate_shape(child, depth=depth + 1)
            return
        if isinstance(value, str):
            ensure_bounded_bytes(
                value,
                label="event string",
                max_bytes=MAX_INSTRUCTION_TEXT_BYTES,
                allow_empty=True,
            )

    def _extract_message_transcript(self, item: JSONDict) -> str | None:
        content = item.get("content")
        if content is None:
            return None
        if not isinstance(content, list):
            raise self._error("invalid_event", "message content must be a list")
        for entry in content:
            if not isinstance(entry, dict):
                raise self._error("invalid_event", "message content entries must be objects")
            transcript = entry.get("transcript")
            if transcript is not None:
                if not isinstance(transcript, str):
                    raise self._error("invalid_event", "message transcript must be a string")
                ensure_bounded_bytes(
                    transcript,
                    label="message transcript",
                    max_bytes=MAX_TASK_TEXT_BYTES,
                )
                return transcript
        return None

    def _extract_status_detail(self, status_details: Any) -> str | None:
        if status_details is None:
            return None
        if not isinstance(status_details, dict):
            raise self._error("invalid_event", "response status_details must be an object or null")
        for key in ("reason", "error", "message"):
            value = status_details.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise self._error("invalid_event", "response status detail must be a string")
            return ensure_bounded_bytes(
                value,
                label="response status detail",
                max_bytes=_MAX_ERROR_DETAIL_BYTES,
            )
        return self._canonical_json(status_details)[:_MAX_ERROR_DETAIL_BYTES]

    def _validate_tool_arguments(self, arguments_json: str) -> None:
        try:
            parsed = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise self._error("invalid_event", "function arguments must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise self._error("invalid_event", "function arguments must decode to an object")
        self._validate_shape(parsed, depth=0)

    def _register_event(self, *, event_id: str, mutation: str, payload: JSONDict) -> BuiltClientEvent:
        if event_id in self._client_mutations:
            raise ValueError("client event IDs must be unique")
        self._client_mutations[event_id] = mutation
        self._client_mutation_order.append(event_id)
        if len(self._client_mutation_order) > MAX_REMEMBERED_IDS:
            expired_event_id = self._client_mutation_order.popleft()
            self._client_mutations.pop(expired_event_id, None)
        return BuiltClientEvent(event_id=event_id, mutation=mutation, payload=payload)

    def _new_event_id(self) -> str:
        value = self._event_id_factory()
        ensure_bounded_bytes(value, label="client event ID", max_bytes=_MAX_IDENTIFIER_BYTES)
        return value

    @staticmethod
    def _canonical_json(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _default_event_id_factory() -> str:
        return f"evt_local_{uuid4().hex}"

    def _tool_definition(
        self,
        *,
        name: str,
        description: str,
        required: Sequence[str],
        properties: Mapping[str, JSONDict],
    ) -> JSONDict:
        ensure_bounded_bytes(name, label="tool name", max_bytes=64)
        ensure_bounded_bytes(description, label="tool description", max_bytes=512)
        return {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": dict(properties),
                "required": list(required),
            },
        }

    @staticmethod
    def _string_schema(description: str, max_bytes: int) -> JSONDict:
        ensure_bounded_bytes(description, label="schema description", max_bytes=256)
        return {
            "type": "string",
            "description": description,
            "minLength": 1,
            "maxLength": max_bytes,
        }

    def _require_supported_tool_name(
        self,
        name: str,
        *,
        event_type: str,
        event_id: str | None,
    ) -> None:
        if name not in _SUPPORTED_FUNCTIONS:
            raise self._error(
                "invalid_event",
                f"unsupported realtime function tool {name!r}",
                event_type=event_type,
                event_id=event_id,
            )

    def _object(self, value: Mapping[str, Any], key: str, *, label: str) -> JSONDict:
        item = value.get(key)
        if not isinstance(item, dict):
            raise self._error("invalid_event", f"{label} must be an object")
        return item

    def _list(self, value: Mapping[str, Any], key: str, *, label: str) -> list[Any]:
        item = value.get(key)
        if not isinstance(item, list):
            raise self._error("invalid_event", f"{label} must be a list")
        if len(item) > _MAX_EVENT_COLLECTION_ITEMS:
            raise self._error("invalid_event", f"{label} exceeds the item limit")
        return item

    def _bounded_string(self, value: Mapping[str, Any], key: str, *, label: str) -> str:
        item = self._require_string(value, key, label=label)
        max_bytes = MAX_TASK_TEXT_BYTES
        if key == "arguments":
            max_bytes = MAX_TOOL_ARGUMENT_BYTES
        elif key == "output":
            max_bytes = MAX_INSTRUCTION_TEXT_BYTES
        return ensure_bounded_bytes(item, label=label, max_bytes=max_bytes)

    def _require_string(self, value: Mapping[str, Any], key: str, *, label: str) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            raise self._error("invalid_event", f"{label} must be a string")
        return ensure_bounded_bytes(item, label=label, max_bytes=MAX_INSTRUCTION_TEXT_BYTES)

    def _optional_string(self, value: Mapping[str, Any], key: str, *, label: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        if not isinstance(item, str):
            raise self._error("invalid_event", f"{label} must be a string when present")
        return ensure_bounded_bytes(
            item,
            label=label,
            max_bytes=MAX_INSTRUCTION_TEXT_BYTES,
            allow_empty=True,
        )

    def _identifier(self, value: Mapping[str, Any], key: str, wrapper: type[Any]) -> Any:
        raw = self._require_string(value, key, label=key)
        try:
            return wrapper(raw)
        except (TypeError, ValueError) as exc:
            raise self._error("invalid_event", f"{key} is invalid") from exc

    def _optional_identifier(self, value: Mapping[str, Any], key: str, wrapper: type[Any]) -> Any | None:
        raw = value.get(key)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise self._error("invalid_event", f"{key} must be a string when present")
        try:
            return wrapper(raw)
        except (TypeError, ValueError) as exc:
            raise self._error("invalid_event", f"{key} is invalid") from exc

    def _require_number(self, value: Mapping[str, Any], key: str, *, label: str) -> int | float:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise self._error("invalid_event", f"{label} must be numeric")
        return item

    def _require_int(self, value: Mapping[str, Any], key: str, *, label: str) -> int:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            raise self._error("invalid_event", f"{label} must be an integer")
        return item

    def _require_bool(self, value: Mapping[str, Any], key: str) -> bool:
        item = value.get(key)
        if not isinstance(item, bool):
            raise self._error("invalid_event", f"{key} must be a boolean")
        return item

    @staticmethod
    def _require_present(value: str | None, label: str) -> str:
        if value is None:
            raise RealtimeProtocolError("invalid_event", f"{label} is required")
        return value

    def _error(
        self,
        code: str,
        message: str,
        *,
        event_type: str | None = None,
        event_id: str | None = None,
    ) -> RealtimeProtocolError:
        return RealtimeProtocolError(
            code,
            redact_public_error_text(message),
            event_type=event_type,
            event_id=event_id,
        )


def _json_object(value: Mapping[str, Any], *, label: str) -> JSONDict:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _truncate_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    marker = "…"
    budget = max_bytes - len(marker.encode("utf-8"))
    return raw[:budget].decode("utf-8", errors="ignore").rstrip() + marker


def deterministic_event_id_factory(prefix: str = "evt_fixture") -> Callable[[], str]:
    """Stable local event ID factory for tests."""

    ensure_bounded_bytes(prefix, label="event prefix", max_bytes=32)
    counter = count(1)

    def factory() -> str:
        return f"{prefix}_{next(counter):04d}"

    return factory
