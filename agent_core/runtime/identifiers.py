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


def parse_session_id(value: Any) -> str:
    return _parse_runtime_id(value, "session")


def parse_task_id(value: Any) -> str:
    return _parse_runtime_id(value, "task")


def parse_monitor_id(value: Any) -> str:
    return _parse_runtime_id(value, "monitor")


def parse_agent_id(value: Any) -> str:
    return _parse_runtime_id(value, "agent")


def parse_bridge_id(value: Any) -> str:
    return _parse_runtime_id(value, "bridge")


def parse_remote_session_id(value: Any) -> str:
    return _parse_runtime_id(value, "remote session")


def parse_artifact_id(value: Any) -> str:
    return _parse_runtime_id(value, "artifact")


def parse_task_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, str):
        raise RuntimeIdentifierError("task reference", value, "expected a string")
    if value.startswith("monitor:"):
        return {"family": "monitor", "id": parse_monitor_id(value[len("monitor:") :])}
    if ":" in value:
        prefix = value.split(":", 1)[0]
        raise RuntimeIdentifierError("task reference", value, f"unsupported family prefix {prefix!r}")
    return {"family": "task", "id": parse_task_id(value)}


def format_monitor_task_reference(monitorId: Any) -> str:
    return f"monitor:{parse_monitor_id(monitorId)}"


def format_task_reference(reference: dict[str, Any]) -> str:
    family = reference.get("family")
    if family == "monitor":
        return format_monitor_task_reference(reference.get("id"))
    if family != "task":
        raise RuntimeIdentifierError("task reference", reference, "unsupported reference family")
    return parse_task_id(reference.get("id"))


_T = TypeVar("_T")


def _try_parse(value: Any, parser: Callable[[Any], _T]) -> _T | None:
    try:
        return parser(value)
    except RuntimeIdentifierError:
        return None


def try_parse_session_id(value: Any) -> str | None:
    return _try_parse(value, parse_session_id)


def try_parse_task_id(value: Any) -> str | None:
    return _try_parse(value, parse_task_id)


def try_parse_task_reference(value: Any) -> dict[str, str] | None:
    return _try_parse(value, parse_task_reference)


def try_parse_monitor_id(value: Any) -> str | None:
    return _try_parse(value, parse_monitor_id)


def try_parse_agent_id(value: Any) -> str | None:
    return _try_parse(value, parse_agent_id)


def try_parse_bridge_id(value: Any) -> str | None:
    return _try_parse(value, parse_bridge_id)


def try_parse_remote_session_id(value: Any) -> str | None:
    return _try_parse(value, parse_remote_session_id)




