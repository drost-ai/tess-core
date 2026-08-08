from __future__ import annotations

import re
from typing import Any, Callable, TypeVar


MAX_RUNTIME_ID_BYTES = 128
_SAFE_RUNTIME_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class RuntimeIdentifierError(ValueError):
    code = "runtime.invalid_identifier"

    def __init__(self, family: str, value: Any, reason: str):
        super().__init__(f"Invalid {family} id: {reason}")
        self.family = family
        self.value = value
        self.reason = reason


def _parse_runtime_id(value: Any, family: str) -> str:
    if not isinstance(value, str):
        raise RuntimeIdentifierError(family, value, "expected a string")
    if not value:
        raise RuntimeIdentifierError(family, value, "must not be empty")
    if value in {".", ".."} or ".." in value:
        raise RuntimeIdentifierError(family, value, "dot segments are forbidden")
    if _CONTROL_CHARACTERS.search(value):
        raise RuntimeIdentifierError(family, value, "control characters are forbidden")
    if len(value.encode("utf-8")) > MAX_RUNTIME_ID_BYTES:
        raise RuntimeIdentifierError(family, value, f"exceeds {MAX_RUNTIME_ID_BYTES} UTF-8 bytes")
    if not _SAFE_RUNTIME_ID.fullmatch(value):
        raise RuntimeIdentifierError(
            family,
            value,
            "only ASCII letters, digits, underscores, and internal hyphens are allowed",
        )
    return value


def parseSessionId(value: Any) -> str:
    return _parse_runtime_id(value, "session")


def parseTaskId(value: Any) -> str:
    return _parse_runtime_id(value, "task")


def parseMonitorId(value: Any) -> str:
    return _parse_runtime_id(value, "monitor")


def parseAgentId(value: Any) -> str:
    return _parse_runtime_id(value, "agent")


def parseBridgeId(value: Any) -> str:
    return _parse_runtime_id(value, "bridge")


def parseRemoteSessionId(value: Any) -> str:
    return _parse_runtime_id(value, "remote session")


def parseArtifactId(value: Any) -> str:
    return _parse_runtime_id(value, "artifact")


def parseTaskReference(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        raise RuntimeIdentifierError("task reference", value, "expected a string")
    if value.startswith("monitor:"):
        return {"family": "monitor", "id": parseMonitorId(value[len("monitor:") :])}
    if ":" in value:
        prefix = value.split(":", 1)[0]
        raise RuntimeIdentifierError("task reference", value, f"unsupported family prefix {prefix!r}")
    return {"family": "task", "id": parseTaskId(value)}


def formatMonitorTaskReference(monitorId: Any) -> str:
    return f"monitor:{parseMonitorId(monitorId)}"


def formatTaskReference(reference: dict[str, Any]) -> str:
    family = reference.get("family")
    if family == "monitor":
        return formatMonitorTaskReference(reference.get("id"))
    if family != "task":
        raise RuntimeIdentifierError("task reference", reference, "unsupported reference family")
    return parseTaskId(reference.get("id"))


_T = TypeVar("_T")


def _try_parse(value: Any, parser: Callable[[Any], _T]) -> _T | None:
    try:
        return parser(value)
    except RuntimeIdentifierError:
        return None


def tryParseSessionId(value: Any) -> str | None:
    return _try_parse(value, parseSessionId)


def tryParseTaskId(value: Any) -> str | None:
    return _try_parse(value, parseTaskId)


def tryParseTaskReference(value: Any) -> dict[str, str] | None:
    return _try_parse(value, parseTaskReference)


def tryParseMonitorId(value: Any) -> str | None:
    return _try_parse(value, parseMonitorId)


def tryParseAgentId(value: Any) -> str | None:
    return _try_parse(value, parseAgentId)


def tryParseBridgeId(value: Any) -> str | None:
    return _try_parse(value, parseBridgeId)


def tryParseRemoteSessionId(value: Any) -> str | None:
    return _try_parse(value, parseRemoteSessionId)




