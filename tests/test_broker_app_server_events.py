from voice_mode.broker import HostDisposition, HostEventKind
from voice_mode.broker.hosts.events import AppServerEventMapper


def completed(status="completed", *, text="Finished once."):
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "status": status,
                "completedAt": 1_752_892_800,
                "items": [
                    {"id": "message-1", "type": "agentMessage", "text": text}
                ],
            },
        },
    }


def test_completion_is_canonical_and_duplicate_terminal_event_is_ignored():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)
    mapper.register_turn("request-1", "thread-1", "turn-1")

    mapper.consume(completed())
    mapper.consume(completed(text="Duplicate must not replace the response."))

    assert len(events) == 1
    event = events[0]
    assert event.kind is HostEventKind.TURN_COMPLETED
    assert event.request_id == "request-1"
    assert event.completion is not None
    canonical = event.completion.canonical_response()
    assert canonical.display_text == "Finished once."
    assert canonical.spoken_text == "Finished once."
    assert mapper.disposition(
        "request-1", "thread-1"
    ) is HostDisposition.COMPLETED


def test_out_of_order_completion_is_held_until_request_is_correlated():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)

    mapper.consume(completed())
    assert events == []

    mapper.register_turn("request-1", "thread-1", "turn-1")
    assert [event.kind for event in events] == [HostEventKind.TURN_COMPLETED]
    assert events[0].request_id == "request-1"


def test_started_after_terminal_is_ignored():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)
    mapper.register_turn("request-1", "thread-1", "turn-1")
    mapper.consume(completed())
    mapper.consume(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "items": [], "status": "inProgress"},
            },
        }
    )

    assert [event.kind for event in events] == [HostEventKind.TURN_COMPLETED]


def test_approval_exposes_identity_and_reason_without_command_text():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)
    mapper.register_turn("request-1", "thread-1", "turn-1")

    message = {
        "id": 42,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "command": "print-secret-command",
            "reason": "Needs expanded filesystem access",
        },
    }
    mapper.consume(message)
    mapper.consume(message)

    assert len(events) == 1
    event = events[0]
    assert event.kind is HostEventKind.APPROVAL_REQUIRED
    assert event.approval is not None
    assert event.approval.approval_id == "42"
    assert event.approval.reason == "Needs expanded filesystem access"
    assert "print-secret-command" not in event.approval.reason


def test_interrupted_turn_produces_one_cancellation():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)
    mapper.register_turn("request-1", "thread-1", "turn-1")

    mapper.consume(completed("interrupted"))
    mapper.consume(completed("interrupted"))

    assert [event.kind for event in events] == [HostEventKind.TURN_CANCELLED]
    assert mapper.wait_for_terminal("turn-1", 0.01) is HostDisposition.CANCELLED


def test_failed_turn_is_terminal_and_surfaces_bounded_error():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)
    mapper.register_turn("request-1", "thread-1", "turn-1")
    message = completed("failed")
    message["params"]["turn"]["error"] = {"message": "Synthetic agent failure"}

    mapper.consume(message)

    assert len(events) == 1
    assert events[0].kind is HostEventKind.TURN_COMPLETED
    assert events[0].completion is None
    assert events[0].error == "Synthetic agent failure"
    assert mapper.disposition(
        "request-1", "thread-1"
    ) is HostDisposition.COMPLETED


def test_unknown_or_malformed_messages_are_ignored():
    mapper = AppServerEventMapper()
    events = []
    mapper.subscribe(events.append)

    mapper.consume({"method": "item/agentMessage/delta", "params": {}})
    mapper.consume({"method": 1, "params": []})

    assert events == []
    assert mapper.disposition("missing", "thread-1") is HostDisposition.ABSENT
