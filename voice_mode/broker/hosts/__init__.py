"""Conversational host contracts and adapters."""

from .base import (
    HostAdapter,
    HostAdapterError,
    HostEventSink,
    Unsubscribe,
    require_capability,
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
from .app_server import AppServerHostAdapter
from .events import AppServerEventMapper
from .selection import (
    ThreadSelection,
    ThreadSelectionSource,
    canonical_repo_root,
    select_thread,
)

__all__ = [
    "HostAdapter",
    "HostAdapterError",
    "HostEventSink",
    "Unsubscribe",
    "require_capability",
    "AppServerClosed",
    "AppServerProtocolFault",
    "AppServerRemoteError",
    "AppServerRequestCancelled",
    "AppServerRequestTimeout",
    "AppServerTransport",
    "AppServerTransportError",
    "AppServerHostAdapter",
    "AppServerEventMapper",
    "ThreadSelection",
    "ThreadSelectionSource",
    "canonical_repo_root",
    "select_thread",
]
