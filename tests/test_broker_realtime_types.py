import json

import pytest

from voice_mode.broker.realtime import (
    LOOPBACK_HOST,
    MAX_PUBLIC_ERROR_BYTES,
    MAX_TASK_TEXT_BYTES,
    AllowedRepoRoot,
    ArbiterAction,
    ArbiterActionKind,
    CodexJobId,
    CodexRequestId,
    DeliveryState,
    HostThreadId,
    HostTurnId,
    IdentifierFactory,
    JobSnapshot,
    JobState,
    PrivateRealtimeCallId,
    PublicError,
    PublicStatus,
    RealtimeFunctionCallId,
    RealtimeItemId,
    RealtimeResponseId,
    RealtimeSessionId,
    ResponseState,
    RolloverState,
    SessionState,
    SpeechState,
    ToolCallRequest,
    TranscriptEvent,
    TransportState,
    WorkerDeliveryId,
    build_loopback_authority,
    build_loopback_origin,
    public_json_dumps,
    redact_public_error_text,
    validate_capability_token,
    validate_loopback_authority,
    validate_loopback_bind_host,
    validate_loopback_origin,
    validate_output_speed,
)


@pytest.mark.parametrize(
    "constructor",
    (
        RealtimeSessionId,
        RealtimeItemId,
        RealtimeResponseId,
        RealtimeFunctionCallId,
        CodexJobId,
        CodexRequestId,
        HostThreadId,
        HostTurnId,
        WorkerDeliveryId,
        PrivateRealtimeCallId,
    ),
)
@pytest.mark.parametrize("bad_value", ("", "bad space", "bad/slash", "é" * 200))
def test_identifier_wrappers_reject_empty_overlong_and_unsafe_values(constructor, bad_value):
    with pytest.raises((TypeError, ValueError)):
        constructor(bad_value)


def test_private_call_identifier_is_not_rendered():
    private = PrivateRealtimeCallId("rtc_private_fixture_001")
    assert repr(private) == "PrivateRealtimeCallId()"
    assert private.raw() == "rtc_private_fixture_001"


@pytest.mark.parametrize("value", ("call_fixture_001", "rtc.fixture_001", "rtc:fixture_001"))
def test_private_call_identifier_requires_the_provider_namespace(value):
    with pytest.raises(ValueError):
        PrivateRealtimeCallId(value)


def test_identifier_factory_enforces_stable_safe_prefix_and_suffix():
    factory = IdentifierFactory("job", CodexJobId, lambda: "0007")
    value = factory.new()
    assert value == CodexJobId("job_0007")

    with pytest.raises(ValueError):
        IdentifierFactory("bad prefix", CodexJobId, lambda: "1")
    with pytest.raises(ValueError):
        IdentifierFactory("job", CodexJobId, lambda: "bad suffix").new()
    with pytest.raises(TypeError):
        IdentifierFactory("job", str, lambda: "0007")


def test_tool_call_and_structured_types_refuse_mixed_identifier_kinds():
    with pytest.raises(TypeError):
        ToolCallRequest(
            call_id=RealtimeItemId("item_1"),
            item_id=RealtimeItemId("item_1"),
            name="delegate_codex",
            arguments_json="{}",
        )

    with pytest.raises(TypeError):
        JobSnapshot(
            job_id=CodexJobId("job_1"),
            request_id=CodexRequestId("request_1"),
            thread_id=HostTurnId("turn_1"),
            state=JobState.RUNNING,
        )

    with pytest.raises(TypeError):
        ArbiterAction(
            kind=ArbiterActionKind.CANCEL_RESPONSE,
            response_id=RealtimeItemId("item_1"),
        )


def test_public_serialization_is_deterministic_and_omits_private_values():
    status = PublicStatus(
        transport=TransportState.CONNECTED,
        session=SessionState.READY,
        speech=SpeechState.USER_ACTIVE,
        response=ResponseState.ACTIVE,
        rollover=RolloverState.REQUESTED,
        session_id=RealtimeSessionId("session_1"),
        active_response_id=RealtimeResponseId("response_1"),
        jobs=(
            JobSnapshot(
                job_id=CodexJobId("job_1"),
                request_id=CodexRequestId("request_1"),
                thread_id=HostThreadId("thread_1"),
                turn_id=HostTurnId("turn_1"),
                state=JobState.WAITING_APPROVAL,
                summary="approve deploy",
                private_completion_ref="host-completion-opaque",
            ),
        ),
        last_error=PublicError(
            code="transport_lost",
            message="Bearer topsecret rtc_private_fixture_001",
        ),
    )
    first = public_json_dumps(status)
    second = public_json_dumps(status)

    assert first == second
    assert "host-completion-opaque" not in first
    assert "rtc_private_fixture_001" not in first
    assert "topsecret" not in first
    assert json.loads(first)["jobs"][0]["thread_id"] == "thread_1"


def test_public_serializer_refuses_private_call_values():
    with pytest.raises(TypeError):
        public_json_dumps(PrivateRealtimeCallId("rtc_private_fixture_002"))


@pytest.mark.parametrize(
    ("source", "expected_fragments"),
    (
        (
            "Authorization: Bearer secret-token sk-secret-123 rtc_private_fixture_010",
            ("Bearer [redacted-token]", "[redacted-openai-key]", "[redacted-call-id]"),
        ),
        (
            "api_key=supersecret https://127.0.0.1:43111/#capability v=0\na=ice-ufrag:private",
            ("api_key=[redacted]", "#[redacted]", "[redacted-sdp]"),
        ),
    ),
)
def test_redaction_scrubs_credentials_fragments_sdp_and_private_call_ids(source, expected_fragments):
    redacted = redact_public_error_text(
        source,
        private_values=("rtc_private_fixture_010",),
    )
    for fragment in expected_fragments:
        assert fragment in redacted
    assert "supersecret" not in redacted
    assert "secret-token" not in redacted
    assert "ice-ufrag" not in redacted


def test_redaction_clamps_on_utf8_bytes():
    source = "é" * (MAX_PUBLIC_ERROR_BYTES + 20)
    redacted = redact_public_error_text(source)
    assert len(redacted.encode("utf-8")) <= MAX_PUBLIC_ERROR_BYTES
    assert redacted.endswith("…")


def test_public_error_clamps_oversized_untrusted_input_without_raising():
    error = PublicError(
        code="remote_error",
        message="a" * 9000 + " sk-secret-should-never-surface",
    )

    assert len(error.message.encode("utf-8")) <= MAX_PUBLIC_ERROR_BYTES
    assert error.message.endswith("…")
    assert "secret" not in error.message


@pytest.mark.parametrize(
    ("token", "ok"),
    (
        ("A" * 24, True),
        ("safe.token_value-0123456789", True),
        ("short", False),
        ("bad token with spaces", False),
    ),
)
def test_capability_token_validation(token, ok):
    if ok:
        assert validate_capability_token(token) == token
    else:
        with pytest.raises(ValueError):
            validate_capability_token(token)


def test_loopback_host_authority_and_origin_are_exact():
    assert validate_loopback_bind_host(LOOPBACK_HOST) == LOOPBACK_HOST
    authority = build_loopback_authority(43111)
    origin = build_loopback_origin(43111)

    assert validate_loopback_authority(authority, expected=authority) == authority
    assert validate_loopback_origin(origin, expected=origin) == origin

    for bad_host in ("localhost", "0.0.0.0", "127.0.0.2"):
        with pytest.raises(ValueError):
            validate_loopback_bind_host(bad_host)

    for bad_authority in ("127.0.0.1", "localhost:43111", "127.0.0.1:0", "127.0.0.1:99999", "127.0.0.1:43111/path"):
        with pytest.raises((TypeError, ValueError)):
            validate_loopback_authority(bad_authority)

    for bad_origin in (
        "https://127.0.0.1:43111",
        "http://localhost:43111",
        "http://127.0.0.1:43111/path",
        "http://127.0.0.1:43111?x=1",
        "http://127.0.0.1:43111#frag",
    ):
        with pytest.raises(ValueError):
            validate_loopback_origin(bad_origin)


def test_allowed_repo_root_uses_existing_canonical_helper(tmp_path):
    allowed = AllowedRepoRoot.from_candidate(tmp_path / "repo" / "..")
    assert allowed.canonical_path == str(tmp_path.resolve())
    assert allowed.name == tmp_path.name

    with pytest.raises(ValueError):
        AllowedRepoRoot("relative/repository")


def test_unicode_string_limits_are_measured_in_utf8_bytes():
    too_large = "é" * (MAX_TASK_TEXT_BYTES // 2 + 1)
    with pytest.raises(ValueError):
        TranscriptEvent(
            item_id=RealtimeItemId("item_1"),
            delivery=DeliveryState.DELIVERED,
            speaker="user",
            text=too_large,
        )


def test_output_speed_validation_is_strict():
    assert validate_output_speed(0.25) == 0.25
    assert validate_output_speed(1.5) == 1.5
    with pytest.raises(ValueError):
        validate_output_speed(0.2)
    with pytest.raises(ValueError):
        validate_output_speed(2.0)
