from __future__ import annotations

import copy
from typing import Any


ORIGIN_METADATA_KEY = "agentCoreOrigin"
ORIGIN_SCHEMA_VERSION = 1

_VALID_ACTORS = {
    "user",
    "runtime",
    "tool",
    "subagent",
    "task",
    "recovery",
    "host",
}
_VALID_INTENT_AUTHORITIES = {"request", "response", "evidence", "none"}

CONTINUATION_BRIDGE_MESSAGE = (
    "Continuation context: earlier history and older parts of the current "
    "tool-driven turn were compacted into the working context provided in "
    "the system prompt. Continue from the recent raw messages below."
)
_PARTIAL_COMPACTION_PREFIX = (
    "Partial continuation summary for older raw context in this session:"
)
_DYNAMIC_CONTEXT_PREFIX = "[Dynamic continuation context]"
_SUBAGENT_RECONCILIATION_PREFIX = "[Background subagent updates]"
_HISTORICAL_TOOL_CONTEXT_PREFIX = (
    "Historical tool context preserved from an earlier retained message:"
)


def create_message_origin(
    *,
    actor: str,
    kind: str,
    intentAuthority: str = "none",
    synthetic: bool = True,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_actor = str(actor or "").strip()
    normalized_kind = str(kind or "").strip()
    normalized_authority = str(intentAuthority or "").strip()
    if normalized_actor not in _VALID_ACTORS:
        raise ValueError(f"Unsupported message origin actor: {normalized_actor}")
    if not normalized_kind:
        raise ValueError("Message origin kind must not be empty.")
    if normalized_authority not in _VALID_INTENT_AUTHORITIES:
        raise ValueError(
            f"Unsupported message intent authority: {normalized_authority}"
        )
    origin = {
        "schemaVersion": ORIGIN_SCHEMA_VERSION,
        "actor": normalized_actor,
        "kind": normalized_kind,
        "intentAuthority": normalized_authority,
        "synthetic": bool(synthetic),
    }
    if lineage:
        origin["lineage"] = _bounded_lineage(lineage)
    return origin


def message_with_origin(
    message: dict[str, Any],
    *,
    actor: str,
    kind: str,
    intentAuthority: str = "none",
    synthetic: bool = True,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(message)
    raw_metadata = result.get("metadata")
    metadata: dict[str, Any] = (
        copy.deepcopy(raw_metadata)
        if isinstance(raw_metadata, dict)
        else {}
    )
    metadata[ORIGIN_METADATA_KEY] = create_message_origin(
        actor=actor,
        kind=kind,
        intentAuthority=intentAuthority,
        synthetic=synthetic,
        lineage=lineage,
    )
    result["metadata"] = metadata
    return result


def user_request_message(
    content: Any,
    *,
    kind: str = "user_request",
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return message_with_origin(
        {"role": "user", "content": copy.deepcopy(content)},
        actor="user",
        kind=kind,
        intentAuthority="request",
        synthetic=False,
        lineage=lineage,
    )


def synthetic_user_message(
    content: Any,
    *,
    kind: str,
    actor: str = "runtime",
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return message_with_origin(
        {"role": "user", "content": copy.deepcopy(content)},
        actor=actor,
        kind=kind,
        intentAuthority="none",
        synthetic=True,
        lineage=lineage,
    )


def get_message_origin(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return None
    origin = metadata.get(ORIGIN_METADATA_KEY)
    return origin if isinstance(origin, dict) else None


def is_runtime_synthetic_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    origin = get_message_origin(message)
    if origin is not None:
        return origin.get("synthetic") is True
    return _is_legacy_synthetic_message(message)


def is_genuine_user_request(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    origin = get_message_origin(message)
    if origin is not None:
        return origin.get("intentAuthority") == "request"
    return not _is_legacy_synthetic_message(message)


def is_plain_user_turn_anchor(message: Any) -> bool:
    return is_genuine_user_request(message) and not _message_has_tool_results(message)


def message_intent_authority(message: Any) -> str:
    origin = get_message_origin(message)
    if origin is not None:
        authority = origin.get("intentAuthority")
        if authority in _VALID_INTENT_AUTHORITIES:
            return str(authority)
    return "request" if is_genuine_user_request(message) else "none"


def message_for_stable_fingerprint(message: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(message)
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return result
    origin = metadata.get(ORIGIN_METADATA_KEY)
    if isinstance(origin, dict):
        origin.pop("lineage", None)
    return result


def _is_legacy_synthetic_message(message: dict[str, Any]) -> bool:
    if _message_has_tool_results(message):
        return True
    text = _message_text(message).strip()
    if not text:
        return False
    if text == CONTINUATION_BRIDGE_MESSAGE:
        return True
    if text.startswith(_PARTIAL_COMPACTION_PREFIX):
        return True
    if text.startswith(_DYNAMIC_CONTEXT_PREFIX) and text.endswith(
        "[/Dynamic continuation context]"
    ):
        return True
    if text.startswith(_SUBAGENT_RECONCILIATION_PREFIX) and text.endswith(
        "[/Background subagent updates]"
    ):
        return True
    if text.startswith(_HISTORICAL_TOOL_CONTEXT_PREFIX):
        return True
    if text.startswith("[PARENT MESSAGE]") and text.endswith(
        "[/PARENT MESSAGE]"
    ):
        return True
    if text.startswith("[SUPERVISOR MESSAGE]") and text.endswith(
        "[/SUPERVISOR MESSAGE]"
    ):
        return True
    return False


def _message_has_tool_results(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _bounded_lineage(value: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in (
        "logicalTurnId",
        "taskId",
        "childAgentId",
        "parentSessionId",
        "parentTurnId",
        "parentToolUseId",
        "launchGroupId",
        "artifactId",
        "providerResponseId",
        "recoveryReceiptId",
        "messageId",
    ):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = item[:256]
    return result


