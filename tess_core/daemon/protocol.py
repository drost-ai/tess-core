from __future__ import annotations

import hmac
import json
import os
from typing import Any


DAEMON_PROTOCOL_VERSION = 2
DAEMON_MAX_REQUEST_FRAME_BYTES = 1024 * 1024
DAEMON_MAX_RESPONSE_FRAME_BYTES = 2 * 1024 * 1024
DAEMON_MAX_PENDING_PER_CLIENT = 128
DAEMON_MAX_PENDING_GLOBAL = 1_024
DAEMON_MAX_REQUEST_IDS_PER_CONNECTION = 65_536
DAEMON_MAX_REQUEST_ID_BYTES = 128
_MAX_PROTOCOL_DIAGNOSTIC_BYTES = 512


class DaemonProtocolError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(bounded_protocol_diagnostic(message))
        self.name = "DaemonProtocolError"
        self.code = code


class DaemonLineDecoder:
    """Incremental newline framing with a ceiling enforced before parsing."""

    def __init__(self, maxFrameBytes: int):
        if isinstance(maxFrameBytes, bool) or not isinstance(maxFrameBytes, int) or maxFrameBytes < 1:
            raise ValueError("Daemon frame limit must be a positive integer.")
        self.maxFrameBytes = maxFrameBytes
        self._buffer = bytearray()

    @property
    def bufferedBytes(self) -> int:
        return len(self._buffer)

    def push(self, value: bytes | bytearray | memoryview | str) -> list[bytes]:
        incoming = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        frames: list[bytes] = []
        offset = 0
        while True:
            newline = incoming.find(b"\n", offset)
            if newline < 0:
                break
            self._append(incoming[offset:newline])
            frames.append(bytes(self._buffer))
            self.reset()
            offset = newline + 1
        self._append(incoming[offset:])
        return frames

    def reset(self) -> None:
        self._buffer.clear()

    def _append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(self._buffer) + len(chunk) > self.maxFrameBytes:
            self.reset()
            raise DaemonProtocolError(
                "daemon.frame_oversized",
                f"Daemon frame exceeded {self.maxFrameBytes} bytes.",
            )
        self._buffer.extend(chunk)


def encode_daemon_frame(value: Any, maxFrameBytes: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception as error:
        raise DaemonProtocolError(
            "daemon.frame_malformed",
            f"Daemon payload could not be serialized: {error}",
        ) from error
    if len(payload) > maxFrameBytes:
        raise DaemonProtocolError(
            "daemon.frame_oversized",
            f"Daemon frame exceeded {maxFrameBytes} bytes.",
        )
    return payload + b"\n"


def parse_daemon_frame(frame: bytes | bytearray | memoryview) -> Any:
    try:
        text = bytes(frame).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DaemonProtocolError(
            "daemon.frame_malformed",
            "Daemon frame is not valid UTF-8.",
        ) from error
    try:
        return json.loads(text)
    except Exception as error:
        raise DaemonProtocolError(
            "daemon.frame_malformed",
            f"Daemon frame is not valid JSON: {error}",
        ) from error


def parse_daemon_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DaemonProtocolError("daemon.frame_malformed", "Daemon request is not an object.")
    request_id = value.get("id")
    method = value.get("method")
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id.encode("utf-8")) <= DAEMON_MAX_REQUEST_ID_BYTES
        or not isinstance(method, str)
        or not method
    ):
        raise DaemonProtocolError(
            "daemon.frame_malformed",
            "Daemon request envelope is invalid.",
        )
    params = value.get("params")
    if params is not None and not isinstance(params, dict):
        raise DaemonProtocolError(
            "daemon.frame_malformed",
            "Daemon request params are invalid.",
        )
    return {"id": request_id, "method": method, "params": params or {}}


def is_daemon_auth_request(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "auth"
        and value.get("protocolVersion") == DAEMON_PROTOCOL_VERSION
        and isinstance(value.get("token"), str)
    )


def is_daemon_auth_response(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "auth_ok"
        and value.get("protocolVersion") == DAEMON_PROTOCOL_VERSION
        and isinstance(value.get("epoch"), str)
    )


def is_daemon_response(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("ok"), bool)
        and (value.get("ok") is True or isinstance(value.get("error"), str))
    )


def is_daemon_event_message(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "event"
        and isinstance(value.get("record"), dict)
    )


def daemon_tokens_equal(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


def resolve_daemon_rpc_timeout_ms(override: int | None = None) -> int:
    from ..config._knobs import knob_value

    return _resolve_timeout(override, None, int(knob_value("daemon.rpc_timeout_ms")), 25, 300_000)


def resolve_daemon_auth_timeout_ms(override: int | None = None) -> int:
    from ..config._knobs import knob_value

    return _resolve_timeout(override, None, int(knob_value("daemon.auth_timeout_ms")), 25, 30_000)


def bounded_protocol_diagnostic(value: str) -> str:
    normalized = " ".join(str(value).replace("\t", " ").splitlines()).strip()
    raw = normalized.encode("utf-8")
    if len(raw) <= _MAX_PROTOCOL_DIAGNOSTIC_BYTES:
        return normalized
    prefix = raw[: _MAX_PROTOCOL_DIAGNOSTIC_BYTES - 3]
    return prefix.decode("utf-8", errors="ignore") + "…"


def _resolve_timeout(
    override: int | None,
    environment: str | None,
    fallback: int,
    minimum: int,
    maximum: int,
) -> int:
    candidate: Any = override
    if candidate is None and environment:
        try:
            candidate = int(environment)
        except ValueError:
            candidate = None
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return min(maximum, max(minimum, candidate))
    return fallback
