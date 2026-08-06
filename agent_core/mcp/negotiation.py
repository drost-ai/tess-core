from __future__ import annotations

from typing import Any


PREFERRED_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LEGACY_PROTOCOL_VERSION = "2024-11-05"


def resolve_negotiated_protocol(value: Any) -> str:
    """Validate a server's initialize revision.

    Older Python MCP fixtures and deployed servers omitted protocolVersion.
    Treat that omission as the oldest supported revision; an explicit unknown
    or future revision is always rejected.
    """

    if isinstance(value, dict) and isinstance(value.get("protocolVersion"), str):
        negotiated = value["protocolVersion"]
    else:
        negotiated = LEGACY_PROTOCOL_VERSION
    if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
        supported = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
        raise RuntimeError(
            f"MCP server requires unsupported protocol version {negotiated}; "
            f"this runtime supports {supported}."
        )
    return negotiated


def bounded_capabilities(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:128]: _bounded_capability_value(item, depth=0)
        for key, item in list(value.items())[:64]
        if isinstance(key, str)
    }


def _bounded_capability_value(value: Any, *, depth: int) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")[:512].decode("utf-8", errors="ignore")
    if depth >= 3:
        return "[bounded]"
    if isinstance(value, dict):
        return {
            str(key)[:128]: _bounded_capability_value(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
            if isinstance(key, str)
        }
    if isinstance(value, list):
        return [_bounded_capability_value(item, depth=depth + 1) for item in value[:32]]
    return str(value)[:256]
