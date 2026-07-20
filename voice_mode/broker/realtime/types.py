"""Provider-neutral value types for the realtime operator."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Final, Generic, TypeVar

from .security import (
    canonical_allowed_repo_root,
    ensure_bounded_bytes,
    redact_public_error_text,
    validate_private_call_id,
)

MAX_SDP_BYTES: Final[int] = 64 * 1024
MAX_SIDEBAND_EVENT_BYTES: Final[int] = 256 * 1024
MAX_SIDEBAND_EVENT_DEPTH: Final[int] = 16
MAX_TOOL_ARGUMENT_BYTES: Final[int] = 16 * 1024
MAX_TASK_TEXT_BYTES: Final[int] = 8 * 1024
MAX_INSTRUCTION_TEXT_BYTES: Final[int] = 16 * 1024
MAX_SUMMARY_TEXT_BYTES: Final[int] = 4 * 1024
MAX_THREAD_STRING_BYTES: Final[int] = 512
MAX_REPOSITORY_STRING_BYTES: Final[int] = 4096
MAX_LOCAL_CONTROL_BYTES: Final[int] = 32 * 1024
MAX_LOCAL_EVENT_BACKLOG: Final[int] = 256
MAX_REMEMBERED_IDS: Final[int] = 512
MIN_OUTPUT_SPEED: Final[float] = 0.25
MAX_OUTPUT_SPEED: Final[float] = 1.5
SHUTDOWN_GRACE_SECONDS: Final[float] = 5.0

IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LOCAL_IDENTIFIER_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TransportState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    LOST = "lost"
    CLOSED = "closed"


class SessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    ROLLING_OVER = "rolling_over"
    FAILED = "failed"
    CLOSED = "closed"


class SpeechState(str, Enum):
    IDLE = "idle"
    USER_ACTIVE = "user_active"
    OPERATOR_ACTIVE = "operator_active"
    INTERRUPTING = "interrupting"


class ResponseState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class JobState(str, Enum):
    ACCEPTED = "accepted"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    UNCERTAIN = "uncertain"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class DeliveryState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SENT = "sent"
    DELIVERED = "delivered"
    UNCERTAIN = "uncertain"
    DROPPED = "dropped"


class RolloverState(str, Enum):
    IDLE = "idle"
    REQUESTED = "requested"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class ArbiterActionKind(str, Enum):
    CREATE_RESPONSE = "create_response"
    CANCEL_RESPONSE = "cancel_response"
    SEND_FUNCTION_OUTPUT = "send_function_output"
    CREATE_WORKER_RESPONSE = "create_worker_response"
    START_ROLLOVER = "start_rollover"
    PUBLISH_STATUS = "publish_status"


@dataclass(frozen=True)
class _Identifier:
    value: str

    def __post_init__(self) -> None:
        ensure_bounded_bytes(self.value, label=type(self).__name__, max_bytes=128)
        if IDENTIFIER_PATTERN.fullmatch(self.value) is None:
            raise ValueError(f"{type(self).__name__} contains unsafe characters")

    def to_public(self) -> str:
        return self.value


@dataclass(frozen=True)
class RealtimeSessionId(_Identifier):
    pass


@dataclass(frozen=True)
class RealtimeItemId(_Identifier):
    pass


@dataclass(frozen=True)
class RealtimeResponseId(_Identifier):
    pass


@dataclass(frozen=True)
class RealtimeFunctionCallId(_Identifier):
    pass


@dataclass(frozen=True)
class CodexJobId(_Identifier):
    pass


@dataclass(frozen=True)
class CodexRequestId(_Identifier):
    pass


@dataclass(frozen=True)
class HostThreadId(_Identifier):
    pass


@dataclass(frozen=True)
class HostTurnId(_Identifier):
    pass


@dataclass(frozen=True)
class WorkerDeliveryId(_Identifier):
    pass


@dataclass(frozen=True)
class PrivateRealtimeCallId:
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        validate_private_call_id(self.value)

    def raw(self) -> str:
        return self.value


IdT = TypeVar("IdT")


@dataclass(frozen=True)
class IdentifierFactory(Generic[IdT]):
    prefix: str
    wrapper_type: type[IdT]
    suffix_factory: Callable[[], str]

    def __post_init__(self) -> None:
        if not isinstance(self.wrapper_type, type) or not issubclass(
            self.wrapper_type, _Identifier
        ):
            raise TypeError("wrapper_type must be a public identifier wrapper")
        ensure_bounded_bytes(self.prefix, label="identifier prefix", max_bytes=32)
        if LOCAL_IDENTIFIER_SUFFIX_PATTERN.fullmatch(self.prefix) is None:
            raise ValueError("identifier prefix contains unsafe characters")

    def new(self) -> IdT:
        suffix = self.suffix_factory()
        ensure_bounded_bytes(suffix, label="identifier suffix", max_bytes=64)
        if LOCAL_IDENTIFIER_SUFFIX_PATTERN.fullmatch(suffix) is None:
            raise ValueError("identifier suffix contains unsafe characters")
        return self.wrapper_type(f"{self.prefix}_{suffix}")


@dataclass(frozen=True)
class AllowedRepoRoot:
    canonical_path: str

    def __post_init__(self) -> None:
        canonical = canonical_allowed_repo_root(self.canonical_path)
        if self.canonical_path != canonical:
            raise ValueError("repository root must already be canonical")

    @classmethod
    def from_candidate(cls, candidate: str | Path) -> "AllowedRepoRoot":
        return cls(canonical_allowed_repo_root(candidate))

    @property
    def name(self) -> str:
        return Path(self.canonical_path).name


@dataclass(frozen=True)
class PublicError:
    code: str
    message: str
    retryable: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        ensure_bounded_bytes(self.code, label="error code", max_bytes=128)
        object.__setattr__(
            self,
            "message",
            redact_public_error_text(self.message),
        )
        if self.detail is not None:
            object.__setattr__(
                self,
                "detail",
                redact_public_error_text(self.detail),
            )


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: RealtimeFunctionCallId
    item_id: RealtimeItemId
    name: str
    arguments_json: str
    request_id: CodexRequestId | None = None

    def __post_init__(self) -> None:
        _require_type("call_id", self.call_id, RealtimeFunctionCallId)
        _require_type("item_id", self.item_id, RealtimeItemId)
        _require_optional_type("request_id", self.request_id, CodexRequestId)
        ensure_bounded_bytes(self.name, label="tool name", max_bytes=128)
        ensure_bounded_bytes(
            self.arguments_json,
            label="tool arguments",
            max_bytes=MAX_TOOL_ARGUMENT_BYTES,
        )


@dataclass(frozen=True)
class JobSnapshot:
    job_id: CodexJobId
    request_id: CodexRequestId
    thread_id: HostThreadId
    state: JobState
    turn_id: HostTurnId | None = None
    summary: str | None = None
    error: PublicError | None = None
    private_completion_ref: str | None = field(
        default=None,
        repr=False,
        compare=False,
        metadata={"public": False},
    )

    def __post_init__(self) -> None:
        _require_type("job_id", self.job_id, CodexJobId)
        _require_type("request_id", self.request_id, CodexRequestId)
        _require_type("thread_id", self.thread_id, HostThreadId)
        _require_optional_type("turn_id", self.turn_id, HostTurnId)
        if not isinstance(self.state, JobState):
            raise TypeError("state must be a JobState")
        if self.summary is not None:
            ensure_bounded_bytes(
                self.summary,
                label="job summary",
                max_bytes=MAX_SUMMARY_TEXT_BYTES,
            )
        if self.error is not None and not isinstance(self.error, PublicError):
            raise TypeError("error must be a PublicError")
        if self.private_completion_ref is not None:
            ensure_bounded_bytes(
                self.private_completion_ref,
                label="private completion ref",
                max_bytes=256,
            )


@dataclass(frozen=True)
class TranscriptEvent:
    item_id: RealtimeItemId
    delivery: DeliveryState
    speaker: str
    text: str
    response_id: RealtimeResponseId | None = None
    final: bool = False

    def __post_init__(self) -> None:
        _require_type("item_id", self.item_id, RealtimeItemId)
        _require_optional_type("response_id", self.response_id, RealtimeResponseId)
        if not isinstance(self.delivery, DeliveryState):
            raise TypeError("delivery must be a DeliveryState")
        ensure_bounded_bytes(self.speaker, label="speaker", max_bytes=32)
        ensure_bounded_bytes(self.text, label="transcript text", max_bytes=MAX_TASK_TEXT_BYTES)


@dataclass(frozen=True)
class ArbiterAction:
    kind: ArbiterActionKind
    response_id: RealtimeResponseId | None = None
    item_id: RealtimeItemId | None = None
    function_call_id: RealtimeFunctionCallId | None = None
    payload_json: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArbiterActionKind):
            raise TypeError("kind must be an ArbiterActionKind")
        _require_optional_type("response_id", self.response_id, RealtimeResponseId)
        _require_optional_type("item_id", self.item_id, RealtimeItemId)
        _require_optional_type("function_call_id", self.function_call_id, RealtimeFunctionCallId)
        if self.payload_json is not None:
            ensure_bounded_bytes(
                self.payload_json,
                label="arbiter payload",
                max_bytes=MAX_LOCAL_CONTROL_BYTES,
            )


@dataclass(frozen=True)
class PublicStatus:
    transport: TransportState
    session: SessionState
    speech: SpeechState
    response: ResponseState
    rollover: RolloverState
    jobs: tuple[JobSnapshot, ...] = ()
    session_id: RealtimeSessionId | None = None
    active_response_id: RealtimeResponseId | None = None
    last_error: PublicError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transport, TransportState):
            raise TypeError("transport must be a TransportState")
        if not isinstance(self.session, SessionState):
            raise TypeError("session must be a SessionState")
        if not isinstance(self.speech, SpeechState):
            raise TypeError("speech must be a SpeechState")
        if not isinstance(self.response, ResponseState):
            raise TypeError("response must be a ResponseState")
        if not isinstance(self.rollover, RolloverState):
            raise TypeError("rollover must be a RolloverState")
        _require_optional_type("session_id", self.session_id, RealtimeSessionId)
        _require_optional_type("active_response_id", self.active_response_id, RealtimeResponseId)
        if self.last_error is not None and not isinstance(self.last_error, PublicError):
            raise TypeError("last_error must be a PublicError")
        if not isinstance(self.jobs, tuple):
            raise TypeError("jobs must be a tuple")
        if len(self.jobs) > MAX_LOCAL_EVENT_BACKLOG:
            raise ValueError(f"jobs exceeds the {MAX_LOCAL_EVENT_BACKLOG} item backlog")
        for job in self.jobs:
            if not isinstance(job, JobSnapshot):
                raise TypeError("jobs must contain JobSnapshot values")


def validate_output_speed(speed: float) -> float:
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise TypeError("speed must be numeric")
    value = float(speed)
    if not MIN_OUTPUT_SPEED <= value <= MAX_OUTPUT_SPEED:
        raise ValueError(
            f"speed must be between {MIN_OUTPUT_SPEED} and {MAX_OUTPUT_SPEED}"
        )
    return value


def public_json_dumps(value: Any) -> str:
    return json.dumps(
        _to_public_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _to_public_value(value: Any) -> Any:
    if isinstance(value, PrivateRealtimeCallId):
        raise TypeError("private realtime call IDs do not have a public serializer")
    if isinstance(value, _Identifier):
        return value.to_public()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        document: dict[str, Any] = {}
        for dataclass_field in fields(value):
            if dataclass_field.metadata.get("public", True) is False:
                continue
            document[dataclass_field.name] = _to_public_value(getattr(value, dataclass_field.name))
        return document
    if isinstance(value, tuple):
        return [_to_public_value(item) for item in value]
    if isinstance(value, list):
        return [_to_public_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_public_value(item) for key, item in value.items()}
    return value


def _require_type(name: str, value: Any, expected: type[Any]) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be a {expected.__name__}")


def _require_optional_type(name: str, value: Any, expected: type[Any]) -> None:
    if value is not None:
        _require_type(name, value, expected)
