"""Validation and redaction helpers for the realtime operator boundary."""

from __future__ import annotations

import re
import secrets
import socket
from pathlib import Path
from typing import Final, Mapping
from urllib.parse import urlsplit

from ..hosts.selection import canonical_repo_root

LOOPBACK_HOST: Final[str] = "127.0.0.1"
LOOPBACK_SCHEME: Final[str] = "http"
MIN_CAPABILITY_TOKEN_BYTES: Final[int] = 24
MAX_CAPABILITY_TOKEN_BYTES: Final[int] = 128
MAX_PUBLIC_ERROR_BYTES: Final[int] = 2048
MAX_PUBLIC_ERROR_INPUT_BYTES: Final[int] = MAX_PUBLIC_ERROR_BYTES * 4
MAX_HEADER_BYTES: Final[int] = 4096
FORWARDED_HEADER_NAMES: Final[tuple[str, ...]] = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Port",
    "X-Forwarded-Proto",
)

CAPABILITY_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._~-]+$")
PRIVATE_CALL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\brtc_[A-Za-z0-9_-]{1,128}\b")
PRIVATE_CALL_ID_FULL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^rtc_[A-Za-z0-9_-]{1,124}$"
)
OPENAI_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bsk-[A-Za-z0-9_-]+\b")
BEARER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~-]+",
    re.IGNORECASE,
)
KEY_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<label>\b(?:api[_-]?key|token|secret)\b\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
RAW_SDP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?ms)(?:^|\n)(?:v=0|o=-\s|s=-$|t=0 0|m=audio\s|"
    r"a=(?:candidate|fingerprint|ice-pwd|ice-ufrag|mid|rtpmap|setup):).*$"
)


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def ensure_bounded_bytes(
    value: str,
    *,
    label: str,
    max_bytes: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    if utf8_len(value) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return value


def validate_loopback_bind_host(host: str) -> str:
    if host != LOOPBACK_HOST:
        raise ValueError(f"loopback host must be {LOOPBACK_HOST}")
    return host


def validate_port(port: int, *, allow_zero: bool = False) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    if allow_zero and port == 0:
        return port
    if 1 <= port <= 65535:
        return port
    raise ValueError("port must be between 1 and 65535")


def build_loopback_authority(port: int) -> str:
    return f"{validate_loopback_bind_host(LOOPBACK_HOST)}:{validate_port(port)}"


def build_loopback_origin(port: int) -> str:
    return f"{LOOPBACK_SCHEME}://{build_loopback_authority(port)}"


def validate_loopback_authority(authority: str, *, expected: str | None = None) -> str:
    ensure_bounded_bytes(authority, label="authority", max_bytes=256)
    if "/" in authority or "@" in authority or "?" in authority or "#" in authority:
        raise ValueError("authority must not contain path, userinfo, query, or fragment")
    host, separator, port_text = authority.partition(":")
    if separator != ":":
        raise ValueError("authority must include a port")
    validate_loopback_bind_host(host)
    validate_port(int(port_text))
    normalized = f"{host}:{int(port_text)}"
    if expected is not None and normalized != expected:
        raise ValueError("authority does not match the expected loopback authority")
    return normalized


def validate_loopback_origin(origin: str, *, expected: str | None = None) -> str:
    ensure_bounded_bytes(origin, label="origin", max_bytes=256)
    parsed = urlsplit(origin)
    if parsed.scheme != LOOPBACK_SCHEME:
        raise ValueError(f"origin scheme must be {LOOPBACK_SCHEME}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("origin must be an origin-only loopback URL")
    authority = validate_loopback_authority(parsed.netloc)
    normalized = f"{LOOPBACK_SCHEME}://{authority}"
    if expected is not None and normalized != expected:
        raise ValueError("origin does not match the expected loopback origin")
    return normalized


def validate_capability_token(token: str) -> str:
    ensure_bounded_bytes(
        token,
        label="capability token",
        max_bytes=MAX_CAPABILITY_TOKEN_BYTES,
    )
    if utf8_len(token) < MIN_CAPABILITY_TOKEN_BYTES:
        raise ValueError(
            f"capability token must be at least {MIN_CAPABILITY_TOKEN_BYTES} UTF-8 bytes"
        )
    if CAPABILITY_TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("capability token contains unsafe characters")
    return token


def generate_capability_token(*, num_bytes: int = 32) -> str:
    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an integer")
    if num_bytes < MIN_CAPABILITY_TOKEN_BYTES:
        raise ValueError(
            f"num_bytes must be at least {MIN_CAPABILITY_TOKEN_BYTES} bytes"
        )
    token = secrets.token_urlsafe(num_bytes)
    return validate_capability_token(token)


def compare_capability_token(expected: str, observed: str) -> bool:
    return secrets.compare_digest(
        validate_capability_token(expected),
        validate_capability_token(observed),
    )


def validate_private_call_id(call_id: str) -> str:
    ensure_bounded_bytes(call_id, label="private realtime call ID", max_bytes=128)
    if PRIVATE_CALL_ID_FULL_PATTERN.fullmatch(call_id) is None:
        raise ValueError("private realtime call ID must use the rtc_ provider namespace")
    return call_id


def canonical_allowed_repo_root(candidate: str | Path) -> str:
    if isinstance(candidate, Path):
        candidate = str(candidate)
    ensure_bounded_bytes(candidate, label="repository root", max_bytes=4096)
    canonical = canonical_repo_root(candidate)
    if not Path(canonical).is_absolute():
        raise ValueError("repository root must canonicalize to an absolute path")
    return canonical


def validate_forwarded_headers(headers: Mapping[str, str]) -> None:
    for name in FORWARDED_HEADER_NAMES:
        value = headers.get(name)
        if value:
            ensure_bounded_bytes(value, label=name, max_bytes=MAX_HEADER_BYTES)
            raise ValueError("forwarding headers are not allowed on the loopback server")


def extract_bearer_token(header_value: str) -> str:
    ensure_bounded_bytes(header_value, label="authorization header", max_bytes=MAX_HEADER_BYTES)
    scheme, separator, token = header_value.partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        raise ValueError("authorization header must use Bearer")
    return validate_capability_token(token)


def validate_loopback_socket_binding(bound_socket: socket.socket) -> tuple[str, int]:
    if not isinstance(bound_socket, socket.socket):
        raise TypeError("bound_socket must be a socket")
    if bound_socket.family != socket.AF_INET:
        raise ValueError("loopback server requires an IPv4 TCP socket")
    sockname = bound_socket.getsockname()
    if not isinstance(sockname, tuple) or len(sockname) < 2:
        raise ValueError("socket binding must expose an IPv4 host and port")
    host = validate_loopback_bind_host(str(sockname[0]))
    port = validate_port(int(sockname[1]), allow_zero=True)
    return host, port


def redact_public_error_text(text: str, *, private_values: tuple[str, ...] = ()) -> str:
    if not isinstance(text, str):
        raise TypeError("public error text must be a string")
    bounded = _truncate_utf8(text, MAX_PUBLIC_ERROR_INPUT_BYTES)
    redacted = OPENAI_KEY_PATTERN.sub("[redacted-openai-key]", bounded)
    redacted = BEARER_PATTERN.sub("Bearer [redacted-token]", redacted)
    redacted = KEY_VALUE_PATTERN.sub(r"\g<label>[redacted]", redacted)
    redacted = RAW_SDP_PATTERN.sub("[redacted-sdp]", redacted)
    redacted = re.sub(r"(https?://[^\s#]+)#[^\s]*", r"\1#[redacted]", redacted)
    redacted = PRIVATE_CALL_ID_PATTERN.sub("[redacted-call-id]", redacted)
    for private_value in private_values:
        if private_value:
            redacted = redacted.replace(private_value, "[redacted-call-id]")
    return _truncate_utf8(redacted, MAX_PUBLIC_ERROR_BYTES)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    marker = "…"
    budget = max_bytes - len(marker.encode("utf-8"))
    prefix = raw[:budget].decode("utf-8", errors="ignore").rstrip()
    return prefix + marker
