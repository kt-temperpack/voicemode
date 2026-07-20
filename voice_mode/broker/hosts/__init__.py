"""Conversational host contracts and adapters."""

from .base import (
    HostAdapter,
    HostAdapterError,
    HostEventSink,
    Unsubscribe,
    require_capability,
)

__all__ = [
    "HostAdapter",
    "HostAdapterError",
    "HostEventSink",
    "Unsubscribe",
    "require_capability",
]
