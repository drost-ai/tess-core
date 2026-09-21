from __future__ import annotations

import math
from typing import Any


def is_child_agent_record(value: Any, candidate: dict[str, Any]) -> bool:
    return (
        _matches_id(value, "agentId", candidate.get("id"))
        and _has_strings(value, ("sessionId", "parentSessionId", "status"))
        and any(
            isinstance(value.get(field), str)
            for field in ("startedAt", "completedAt", "lastUpdatedAt", "latestResultAt")
        )
    )


def is_task_record(value: Any, candidate: dict[str, Any]) -> bool:
    return _matches_id(value, "id", candidate.get("id")) and _has_strings(
        value,
        ("sessionId", "kind", "status", "updatedAt"),
    )


def is_monitor_record(value: Any, candidate: dict[str, Any]) -> bool:
    return (
        _matches_id(value, "id", candidate.get("id"))
        and _has_strings(value, ("sessionId", "taskId", "status", "createdAt", "updatedAt"))
        and _optional_nonnegative_integer(value.get("revision"))
        and _optional_string(value.get("lastTransitionId"))
        and _valid_monitor_delivery(value.get("delivery"))
    )


def _valid_monitor_delivery(state: Any) -> bool:
    if state is None:
        return True
    if not isinstance(state, dict) or state.get("version") != 1:
        return False
    if state.get("mode") not in {"notification", "continuation"} or not isinstance(state.get("runtimeHome"), str):
        return False
    queue = state.get("queue")
    if not isinstance(queue, list) or len(queue) > 2:
        return False
    for key in ("sequence", "generation", "processedBytes", "processedGeneration", "publishedBytes", "publishedGeneration"):
        if type(state.get(key)) is not int or state[key] < 0:
            return False
    for item in queue:
        if not isinstance(item, dict) or not _has_strings(item, ("id", "state", "reason", "taskStatus", "observedAt")):
            return False
        if item["state"] not in {"pending", "claimed", "authored", "retryable", "failed"}:
            return False
        for key in ("attempts", "generation", "fromByte", "toByte", "availableAt"):
            if type(item.get(key)) is not int or item[key] < 0:
                return False
        if item["fromByte"] > item["toByte"]:
            return False
    return True


def is_remote_session_record(value: Any, candidate: dict[str, Any]) -> bool:
    # Older Python records predate the full timestamp/session projection. Keep
    # those opaque records readable while still enforcing their identity and
    # state discriminator; newly created records carry the complete schema.
    return _matches_id(value, "id", candidate.get("id")) and _has_strings(value, ("status",))


def is_bridge_record(value: Any, candidate: dict[str, Any]) -> bool:
    return _matches_id(value, "id", candidate.get("id")) and _has_strings(value, ("status",))


def is_health_check_task_record(value: Any, candidate: dict[str, Any]) -> bool:
    # Early-schema health records may lack the later projection-only
    # session/mode/timestamp fields. Identity plus a status discriminator is
    # the safe legacy admission floor; current writers emit the full shape.
    return _matches_id(value, "taskId", candidate.get("id")) and _has_strings(value, ("status",))


def is_daemon_state_record(value: Any, _candidate: dict[str, Any]) -> bool:
    pid = value.get("pid") if isinstance(value, dict) else None
    return isinstance(pid, (int, float)) and not isinstance(pid, bool) and math.isfinite(pid)


def is_task_meta_record(value: Any, candidate: dict[str, Any]) -> bool:
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
    "is_bridge_record",
    "is_child_agent_record",
    "is_daemon_state_record",
    "is_health_check_task_record",
    "is_monitor_record",
    "is_remote_session_record",
    "is_task_meta_record",
    "is_task_record",
]
