from __future__ import annotations

import math
from typing import Any


def isChildAgentRecord(value: Any, candidate: dict[str, Any]) -> bool:
    return (
        _matches_id(value, "agentId", candidate.get("id"))
        and _has_strings(value, ("sessionId", "parentSessionId", "status"))
        and any(
            isinstance(value.get(field), str)
            for field in ("startedAt", "completedAt", "lastUpdatedAt", "latestResultAt")
        )
    )


def isTaskRecord(value: Any, candidate: dict[str, Any]) -> bool:
    return _matches_id(value, "id", candidate.get("id")) and _has_strings(
        value,
        ("sessionId", "kind", "status", "updatedAt"),
    )


def isMonitorRecord(value: Any, candidate: dict[str, Any]) -> bool:
    return (
        _matches_id(value, "id", candidate.get("id"))
        and _has_strings(value, ("sessionId", "taskId", "status", "createdAt", "updatedAt"))
        and _optional_nonnegative_integer(value.get("revision"))
        and _optional_string(value.get("lastTransitionId"))
    )


def isRemoteSessionRecord(value: Any, candidate: dict[str, Any]) -> bool:
    # Older Python records predate the full timestamp/session projection. Keep
    # those opaque records readable while still enforcing their identity and
    # state discriminator; newly created records carry the complete schema.
    return _matches_id(value, "id", candidate.get("id")) and _has_strings(value, ("status",))


def isBridgeRecord(value: Any, candidate: dict[str, Any]) -> bool:
    return _matches_id(value, "id", candidate.get("id")) and _has_strings(value, ("status",))


def isHealthCheckTaskRecord(value: Any, candidate: dict[str, Any]) -> bool:
    # Early-schema health records may lack the later projection-only
    # session/mode/timestamp fields. Identity plus a status discriminator is
    # the safe legacy admission floor; current writers emit the full shape.
    return _matches_id(value, "taskId", candidate.get("id")) and _has_strings(value, ("status",))


def isDaemonStateRecord(value: Any, _candidate: dict[str, Any]) -> bool:
    pid = value.get("pid") if isinstance(value, dict) else None
    return isinstance(pid, (int, float)) and not isinstance(pid, bool) and math.isfinite(pid)


def isTaskMetaRecord(value: Any, candidate: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    expected = candidate.get("id")
    return _optional_id_matches(value.get("taskId"), expected) and _optional_id_matches(
        value.get("task_id"),
        expected,
    )


def _matches_id(value: Any, field: str, expected: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        return False
    return expected is None or value[field] == expected


def _optional_id_matches(value: Any, expected: Any) -> bool:
    return value is None or (isinstance(value, str) and (expected is None or value == expected))


def _has_strings(value: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(isinstance(value.get(field), str) for field in fields)


def _optional_nonnegative_integer(value: Any) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _optional_string(value: Any) -> bool:
    return value is None or isinstance(value, str)


__all__ = [
    "isBridgeRecord",
    "isChildAgentRecord",
    "isDaemonStateRecord",
    "isHealthCheckTaskRecord",
    "isMonitorRecord",
    "isRemoteSessionRecord",
    "isTaskMetaRecord",
    "isTaskRecord",
]
