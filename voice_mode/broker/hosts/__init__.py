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
]
