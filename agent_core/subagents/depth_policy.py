from __future__ import annotations

from typing import Any


DEFAULT_SUBAGENT_MAX_DEPTH = 2


def resolveSubagentMaxDepth(config: dict[str, Any] | None = None, session: dict[str, Any] | None = None) -> int:
    config = config or {}
    session_metadata = session.get("metadata") if isinstance(session, dict) and isinstance(session.get("metadata"), dict) else {}
    candidates = [
        session_metadata.get("maxSubagentDepth"),
        session_metadata.get("subagentMaxDepth"),
        _nested(config.get("subagents"), "maxDepth"),
        _nested(config.get("subagent"), "maxDepth"),
        config.get("maxSubagentDepth"),
        config.get("subagentMaxDepth"),
    ]
    for candidate in candidates:
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return DEFAULT_SUBAGENT_MAX_DEPTH


def checkSubagentDepthAllowed(session: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    try:
        parent_depth = int(metadata.get("depth") or 0)
    except (TypeError, ValueError):
        parent_depth = 0
    max_depth = resolveSubagentMaxDepth(config, session)
    child_depth = parent_depth + 1
    allowed = child_depth <= max_depth
    message = (
        "Delegation depth limit reached."
        if max_depth == DEFAULT_SUBAGENT_MAX_DEPTH
        else f"Delegation depth limit reached (child depth {child_depth} exceeds max {max_depth})."
    )
    return {
        "allowed": allowed,
        "parentDepth": parent_depth,
        "childDepth": child_depth,
        "maxDepth": max_depth,
        "message": message,
    }


def requireSubagentDepthAllowed(session: dict[str, Any], config: dict[str, Any] | None = None) -> None:
    result = checkSubagentDepthAllowed(session, config)
    if not result["allowed"]:
        raise RuntimeError(result["message"])


def _nested(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


