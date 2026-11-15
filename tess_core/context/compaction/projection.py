"""Compaction policy and system-prompt projection: when to compact and how
the persisted working context projects into a session's prompt."""
from __future__ import annotations

import os
from typing import Any

from ...storage.message_origin import synthetic_user_message as _synthetic_user_message
from ...session.tool_boundaries import adjust_start_to_tool_boundary as _adjust_start_to_tool_boundary
from ...storage import get_session_compactions_dir
from .documents import load_working_context_artifact
from .prompts import render_working_context_prompt_context
from .shared import CONTINUATION_BRIDGE_MESSAGE, compaction_knob
from .utils import estimate_message_tokens, estimate_text_tokens

def estimate_working_context_tokens(session: dict[str, Any]) -> int:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return estimate_message_tokens(session.get("messages", []))
    cutoff = int(state.get("compactedThroughMessageIndex") or 0)
    recent_messages = session.get("messages", [])[cutoff:]
    return int(state.get("compactedContextTokenEstimate") or 0) + estimate_message_tokens(recent_messages)


def should_auto_compact(
    session: dict[str, Any],
    currentInputTokens: int | None = None,
    tokenLimit: int | None = None,
    targetTokenLimit: int | None = None,
) -> dict[str, Any]:
    current_token_estimate = currentInputTokens if currentInputTokens is not None else estimate_working_context_tokens(session)
    limit = int(tokenLimit or compaction_knob("compaction.working_context_token_limit"))
    target = int(targetTokenLimit or limit)
    if current_token_estimate >= limit:
        return {
            "shouldCompact": True,
            "trigger": "auto_hard_limit",
            "currentTokenEstimate": current_token_estimate,
        }
    if current_token_estimate > target:
        return {
            "shouldCompact": True,
            "trigger": "auto_target_headroom",
            "currentTokenEstimate": current_token_estimate,
        }
    return {
        "shouldCompact": False,
        "currentTokenEstimate": current_token_estimate,
    }


def build_messages_for_working_context(session: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages a request is built from: those after the session's compaction cutoff.

    The start is adjusted so the list never begins inside a tool exchange.
    """
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return list(session.get("messages", []))
    messages = list(session.get("messages", []))
    # Index drift (e.g. a manifest refresh) can land the stored cutoff inside
    # a tool exchange; back it up to a boundary so the working set never
    # starts with an orphan tool_result.
    cutoff = _adjust_start_to_tool_boundary(
        messages, int(state.get("compactedThroughMessageIndex") or 0)
    )
    recent_messages = messages[cutoff:]
    if not recent_messages or recent_messages[0].get("role") == "user":
        return recent_messages
    return [
        _synthetic_user_message(
            CONTINUATION_BRIDGE_MESSAGE,
            kind="continuation_bridge",
        ),
        *recent_messages,
    ]


async def build_compaction_system_prompt_context(session: dict[str, Any]) -> str | None:
    projection = await _build_compaction_system_prompt_projection(session)
    return projection["text"]


async def _build_compaction_system_prompt_projection(
    session: dict[str, Any],
) -> dict[str, Any]:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return {
            "text": None,
            "applied": False,
            "originalTokens": 0,
            "projectedTokens": 0,
        }
    artifacts = state.get("artifacts") or {}
    working_context_path = artifacts.get("workingContext")
    if not _is_session_compaction_artifact_path(session, working_context_path):
        return {
            "text": None,
            "applied": False,
            "originalTokens": 0,
            "projectedTokens": 0,
        }
    working_context = await load_working_context_artifact(working_context_path)
    if not working_context or working_context.get("sessionId") != session.get("id"):
        return {
            "text": None,
            "applied": False,
            "originalTokens": 0,
            "projectedTokens": 0,
        }
    original = render_working_context_prompt_context(working_context)
    original_tokens = estimate_text_tokens(original)
    return {
        "text": original,
        "applied": False,
        "originalTokens": original_tokens,
        "projectedTokens": original_tokens,
    }


def _is_session_compaction_artifact_path(
    session: dict[str, Any],
    artifactPath: Any,
) -> bool:
    session_id = session.get("id")
    if not isinstance(session_id, str) or not isinstance(artifactPath, str) or not artifactPath:
        return False
    try:
        session_dir = os.path.realpath(get_session_compactions_dir(session_id))
        resolved_artifact = os.path.realpath(artifactPath)
        return resolved_artifact != session_dir and os.path.commonpath(
            [session_dir, resolved_artifact]
        ) == session_dir
    except (OSError, RuntimeError, ValueError):
        return False
