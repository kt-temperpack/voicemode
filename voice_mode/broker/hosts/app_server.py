"""Codex app-server host adapter for capability and thread management."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..types import (
    HostCapability,
    HostCompletion,
    HostDisposition,
    HostErrorKind,
    HostProbe,
    HostRecoveryEvidence,
    HostThreadSummary,
    HostTurn,
    HostTurnState,
)
from .app_server_transport import (
    AppServerClosed,
    AppServerProtocolFault,
    AppServerRemoteError,
    AppServerRequestCancelled,
    AppServerRequestTimeout,
    AppServerTransport,
    AppServerTransportError,
)
from .base import HostAdapter, HostAdapterError, HostEventSink, Unsubscribe
from .events import AppServerEventMapper


_CAPABILITIES = frozenset(
    {
        HostCapability.LIST_THREADS,
        HostCapability.READ_THREAD,
        HostCapability.ATTACH_THREAD,
        HostCapability.CREATE_THREAD,
        HostCapability.START_TURN,
        HostCapability.STEER_TURN,
        HostCapability.INTERRUPT_TURN,
        HostCapability.SUBSCRIBE_EVENTS,
        HostCapability.QUERY_DISPOSITION,
    }
)

_METHOD_CAPABILITIES = {
    "thread/list": HostCapability.LIST_THREADS,
    "thread/read": HostCapability.READ_THREAD,
    "thread/resume": HostCapability.ATTACH_THREAD,
    "thread/start": HostCapability.CREATE_THREAD,
    "turn/start": HostCapability.START_TURN,
    "turn/steer": HostCapability.STEER_TURN,
    "turn/interrupt": HostCapability.INTERRUPT_TURN,
}


class AppServerHostAdapter(HostAdapter):
    """Translate current Codex app-server thread methods into stable host types."""

    def __init__(
        self,
        transport: AppServerTransport,
        initialize_result: dict[str, Any],
        *,
        request_timeout: float = 10.0,
    ) -> None:
        self._transport = transport
        self._initialize_result = initialize_result
        self._request_timeout = request_timeout
        self._probe: HostProbe | None = None
        self._broker_owned: set[str] = set()
        self._events = AppServerEventMapper()
        self._transport_unsubscribe: Unsubscribe | None = None

    @classmethod
    def connect(
        cls,
        transport: AppServerTransport,
        *,
        client_name: str = "voicemode",
        client_version: str = "development",
        timeout: float = 10.0,
    ) -> AppServerHostAdapter:
        try:
            result = transport.initialize(
                {"name": client_name, "version": client_version}, timeout=timeout
            )
        except BaseException:
            transport.close()
            raise
        return cls(transport, result, request_timeout=timeout)

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return self._transport.request(method, params, timeout=self._request_timeout)
        except AppServerRemoteError as error:
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                method,
                f"Codex rejected {method}: {error}",
            ) from error
        except (AppServerClosed, AppServerRequestTimeout, AppServerRequestCancelled) as error:
            raise HostAdapterError(
                HostErrorKind.RETRYABLE_TRANSPORT,
                method,
                f"Codex app-server transport failed during {method}: {error}",
            ) from error
        except (AppServerProtocolFault, AppServerTransportError) as error:
            raise HostAdapterError(
                HostErrorKind.UNAVAILABLE,
                method,
                f"Codex app-server protocol failed during {method}: {error}",
            ) from error

    def _unsupported(self, operation: HostCapability):
        raise HostAdapterError(
            HostErrorKind.UNSUPPORTED,
            operation.value,
            f"Codex app-server adapter does not yet support {operation.value}",
        )

    def probe(self) -> HostProbe:
        if self._probe is not None:
            return self._probe
        version = self._initialize_result.get("userAgent")
        try:
            self._request("thread/list", {"limit": 1, "useStateDbOnly": True})
        except HostAdapterError as error:
            available = error.kind is HostErrorKind.HOST_REJECTION
            self._probe = HostProbe(
                "app-server",
                available,
                frozenset(),
                str(version) if version else None,
                str(error),
            )
        else:
            capabilities = self._declared_capabilities()
            self._probe = HostProbe(
                "app-server",
                True,
                capabilities,
                str(version) if version else None,
            )
        return self._probe

    def _declared_capabilities(self) -> frozenset[HostCapability]:
        """Use live method declarations when Codex provides them.

        Older app-server versions do not advertise a method list, so their
        successful thread probe retains the established capability set.
        """
        declared = self._initialize_result.get("capabilities")
        methods = declared.get("methods") if isinstance(declared, dict) else None
        if not isinstance(methods, list) or not all(
            isinstance(method, str) for method in methods
        ):
            return _CAPABILITIES
        capabilities = {
            capability
            for method, capability in _METHOD_CAPABILITIES.items()
            if method in methods
        }
        if HostCapability.START_TURN in capabilities:
            capabilities.add(HostCapability.SUBSCRIBE_EVENTS)
        if HostCapability.READ_THREAD in capabilities:
            capabilities.add(HostCapability.QUERY_DISPOSITION)
        return frozenset(capabilities)

    def list_threads(self, repo_root: str | None = None) -> tuple[HostThreadSummary, ...]:
        params: dict[str, Any] = {
            "limit": 100,
            "sortKey": "updated_at",
            "sortDirection": "desc",
        }
        if repo_root is not None:
            params["cwd"] = str(Path(repo_root).resolve(strict=False))
        summaries = []
        seen_cursors = set()
        for _page in range(10):
            result = self._request("thread/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise HostAdapterError(
                    HostErrorKind.HOST_REJECTION,
                    "thread/list",
                    "Codex returned an invalid thread list",
                )
            summaries.extend(self._summary(item) for item in result["data"])
            cursor = result.get("nextCursor")
            if cursor is None:
                return tuple(summaries)
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise HostAdapterError(
                    HostErrorKind.AMBIGUOUS,
                    "thread/list",
                    "Codex returned an invalid pagination cursor",
                )
            seen_cursors.add(cursor)
            params["cursor"] = cursor
        raise HostAdapterError(
            HostErrorKind.AMBIGUOUS,
            "thread/list",
            "Codex thread listing exceeded the bounded page limit",
        )

    def read_thread(self, thread_id: str) -> HostThreadSummary:
        result = self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        return self._response_summary(result, "thread/read")

    def attach_thread(self, thread_id: str) -> HostThreadSummary:
        result = self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        return self._response_summary(result, "thread/resume")

    def create_thread(self, repo_root: str, label: str) -> HostThreadSummary:
        canonical_root = str(Path(repo_root).resolve(strict=False))
        result = self._request(
            "thread/start",
            {"cwd": canonical_root, "threadSource": "appServer"},
        )
        summary = self._response_summary(result, "thread/start")
        self._request("thread/setName", {"threadId": summary.thread_id, "name": label})
        self._broker_owned.add(summary.thread_id)
        return replace(summary, title=label, broker_owned=True)

    def start_turn(self, *, request_id: str, thread_id: str, prompt: str) -> HostTurn:
        self._ensure_event_subscription()
        result = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "clientUserMessageId": request_id,
            },
        )
        turn_id = self._response_turn_id(result, "turn/start")
        self._events.register_turn(request_id, thread_id, turn_id)
        return HostTurn(request_id, thread_id, turn_id, HostTurnState.STARTED)

    def steer_turn(
        self,
        *,
        request_id: str,
        thread_id: str,
        host_turn_id: str,
        prompt: str,
    ) -> HostTurn:
        self._ensure_event_subscription()
        result = self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": host_turn_id,
                "input": [{"type": "text", "text": prompt}],
                "clientUserMessageId": request_id,
            },
        )
        if not isinstance(result, dict) or result.get("turnId") != host_turn_id:
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                "turn/steer",
                "Codex returned an invalid steer response",
            )
        return HostTurn(request_id, thread_id, host_turn_id, HostTurnState.STEERED)

    def interrupt_turn(
        self,
        *,
        request_id: str,
        thread_id: str,
        host_turn_id: str,
    ) -> HostTurn:
        self._ensure_event_subscription()
        self._request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": host_turn_id},
        )
        disposition = self._events.wait_for_terminal(
            host_turn_id, self._request_timeout
        )
        if disposition is not HostDisposition.CANCELLED:
            raise HostAdapterError(
                HostErrorKind.AMBIGUOUS,
                "turn/interrupt",
                "Codex did not confirm interruption before the deadline",
            )
        return HostTurn(request_id, thread_id, host_turn_id, HostTurnState.CANCELLED)

    def subscribe(self, sink: HostEventSink) -> Unsubscribe:
        self._ensure_event_subscription()
        return self._events.subscribe(sink)

    def query_disposition(self, *, request_id: str, thread_id: str) -> HostDisposition:
        return self._events.disposition(request_id, thread_id)

    def recover_request(
        self,
        *,
        request_id: str,
        thread_id: str,
    ) -> HostRecoveryEvidence:
        """Query durable thread history instead of an empty new event mapper."""

        result = self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if not isinstance(turns, list):
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                "thread/read",
                "Codex returned thread history without a turns list",
            )
        matches = [turn for turn in turns if self._turn_request_id(turn) == request_id]
        if not matches:
            return HostRecoveryEvidence(
                HostDisposition.ABSENT,
                "thread history contains no turn for the broker request ID",
            )
        if len(matches) != 1:
            return HostRecoveryEvidence(
                HostDisposition.UNCERTAIN,
                "thread history contains multiple turns for the broker request ID",
            )
        turn = matches[0]
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            return HostRecoveryEvidence(
                HostDisposition.UNCERTAIN,
                "correlated thread history has no stable host turn ID",
            )
        status = turn.get("status")
        status_type = status.get("type") if isinstance(status, dict) else status
        if status_type in {"inProgress", "in_progress", "active"}:
            return HostRecoveryEvidence(
                HostDisposition.IN_PROGRESS,
                "the correlated Codex turn is still in progress",
            )
        if status_type in {"interrupted", "cancelled", "canceled"}:
            return HostRecoveryEvidence(
                HostDisposition.CANCELLED,
                "the correlated Codex turn has terminal cancellation evidence",
            )
        if status_type != "completed":
            return HostRecoveryEvidence(
                HostDisposition.UNCERTAIN,
                f"the correlated Codex turn has unrecognized status {status_type!r}",
            )
        text = AppServerEventMapper.agent_text(turn.get("items"))
        if not text:
            return HostRecoveryEvidence(
                HostDisposition.UNCERTAIN,
                "the correlated completed turn has no canonical agent response",
            )
        completed_at = turn.get("completedAt")
        completed = (
            datetime.fromtimestamp(completed_at, timezone.utc)
            if isinstance(completed_at, (int, float))
            else datetime.now(timezone.utc)
        )
        return HostRecoveryEvidence(
            HostDisposition.COMPLETED,
            "thread history proves the correlated Codex turn completed",
            HostCompletion(
                request_id,
                thread_id,
                turn_id,
                text,
                text,
                completed,
            ),
        )

    @staticmethod
    def _turn_request_id(turn: Any) -> str | None:
        if not isinstance(turn, dict):
            return None
        direct = turn.get("clientUserMessageId")
        if isinstance(direct, str):
            return direct
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(
                    item.get("clientUserMessageId"), str
                ):
                    return item["clientUserMessageId"]
        return None

    def close(self) -> None:
        if self._transport_unsubscribe is not None:
            self._transport_unsubscribe()
            self._transport_unsubscribe = None
        self._transport.close()

    def _ensure_event_subscription(self) -> None:
        if self._transport_unsubscribe is None:
            self._transport_unsubscribe = self._transport.subscribe(self._events.consume)

    def _response_turn_id(self, result: Any, operation: str) -> str:
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                operation,
                f"Codex returned an invalid response for {operation}",
            )
        return turn_id

    def _response_summary(self, result: Any, operation: str) -> HostThreadSummary:
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                operation,
                f"Codex returned an invalid response for {operation}",
            )
        return self._summary(result["thread"])

    def _summary(self, payload: Any) -> HostThreadSummary:
        if not isinstance(payload, dict):
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                "thread/parse",
                "Codex returned an invalid thread entry",
            )
        thread_id = payload.get("id")
        cwd = payload.get("cwd")
        if not isinstance(thread_id, str) or not isinstance(cwd, str):
            raise HostAdapterError(
                HostErrorKind.HOST_REJECTION,
                "thread/parse",
                "Codex thread identity is incomplete",
            )
        updated = payload.get("updatedAt")
        updated_at = (
            datetime.fromtimestamp(updated, timezone.utc)
            if isinstance(updated, (int, float))
            else None
        )
        status = payload.get("status")
        status_type = status.get("type") if isinstance(status, dict) else None
        title = payload.get("name") or payload.get("preview") or None
        return HostThreadSummary(
            thread_id=thread_id,
            repo_root=str(Path(cwd).resolve(strict=False)),
            title=str(title) if title else None,
            updated_at=updated_at,
            active=status_type in {"active", "idle"},
            broker_owned=thread_id in self._broker_owned,
        )
