from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any, Iterable, Literal, TypedDict


CURSOR_PREFIX = "acp1."
MAX_CURSOR_BYTES = 1_024
PayloadCursorFamily = Literal[
    "read_text",
    "read_notebook",
    "glob",
    "grep",
    "mcp_resource_list",
    "mcp_resource_read",
]


class PayloadCursorPosition(TypedDict, total=False):
    recordOffset: int
    byteOffset: int
    sourceByteOffset: int
    recordBytes: int
    snapshotFingerprint: str


def encodePayloadCursor(
    *,
    family: PayloadCursorFamily,
    sourceFingerprint: str,
    position: PayloadCursorPosition,
) -> str:
    record_offset = _require_offset(position.get("recordOffset"), "record offset")
    byte_offset = _optional_offset(position.get("byteOffset"), "byte offset")
    source_byte_offset = _optional_offset(
        position.get("sourceByteOffset"), "source byte offset"
    )
    record_bytes = _optional_offset(position.get("recordBytes"), "record byte size")
    snapshot = position.get("snapshotFingerprint")
    if snapshot is not None:
        snapshot = _require_fingerprint(snapshot)
    payload = {
        "v": 1,
        "f": family,
        "s": _require_fingerprint(sourceFingerprint),
        "r": record_offset,
        **({"b": byte_offset} if byte_offset else {}),
        **({"a": source_byte_offset} if source_byte_offset else {}),
        **({"n": record_bytes} if record_bytes is not None else {}),
        **({"q": snapshot} if snapshot is not None else {}),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{CURSOR_PREFIX}{encoded}"


def resolvePayloadCursor(
    *,
    family: PayloadCursorFamily,
    sourceFingerprint: str,
    cursor: Any = None,
    legacyOffset: int | float | None = None,
    legacyBase: Literal[0, 1],
) -> dict[str, Any]:
    # Zero-filling providers (Codex-family models) send every optional field:
    # an unused cursor arrives as "" or 0 and an unused offset as 0. A falsy
    # cursor carries no position, and only a positive offset conflicts with a
    # genuine cursor.
    if cursor:
        if _finite_number(legacyOffset) and math.floor(legacyOffset) > 0:
            raise ValueError("Provide cursor or offset, not both.")
        return {
            **_decode_payload_cursor(
                cursor=cursor,
                family=family,
                source_fingerprint=sourceFingerprint,
            ),
            "source": "cursor",
        }
    fallback = legacyBase
    value = math.floor(legacyOffset) if _finite_number(legacyOffset) else fallback
    return {"recordOffset": max(0, value - legacyBase), "source": "legacy"}


def fingerprintPayloadSource(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        value = str(part)
        raw = value.encode("utf-8")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b":")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _decode_payload_cursor(
    *, cursor: Any, family: PayloadCursorFamily, source_fingerprint: str
) -> PayloadCursorPosition:
    if not isinstance(cursor, str) or len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise ValueError(f"Invalid {family} cursor.")
    if not cursor.startswith(CURSOR_PREFIX):
        raise ValueError(f"Unsupported {family} cursor version.")
    encoded = cursor[len(CURSOR_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except Exception as error:
        raise ValueError(f"Invalid {family} cursor encoding.") from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != encoded or len(decoded) > MAX_CURSOR_BYTES:
        raise ValueError(f"Invalid {family} cursor encoding.")
    try:
        value = json.loads(decoded.decode("utf-8"))
    except Exception as error:
        raise ValueError(f"Invalid {family} cursor payload.") from error
    if not isinstance(value, dict) or value.get("v") != 1 or value.get("f") != family:
        raise ValueError(f"Cursor does not belong to {family}.")
    if value.get("s") != _require_fingerprint(source_fingerprint):
        raise ValueError(f"The {family} cursor source changed; restart pagination.")
    result: PayloadCursorPosition = {
        "recordOffset": _require_offset(value.get("r"), "record offset")
    }
    for compact, expanded, label in (
        ("b", "byteOffset", "byte offset"),
        ("a", "sourceByteOffset", "source byte offset"),
        ("n", "recordBytes", "record byte size"),
    ):
        if compact in value:
            result[expanded] = _require_offset(value[compact], label)  # type: ignore[literal-required]
    if "q" in value:
        result["snapshotFingerprint"] = _require_fingerprint(value["q"])
    return result


def _optional_offset(value: Any, label: str) -> int | None:
    return None if value is None else _require_offset(value, label)


def _require_offset(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Invalid payload cursor {label}.")
    return value


def _require_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{24}", value):
        raise ValueError("Invalid payload cursor source fingerprint.")
    return value


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


