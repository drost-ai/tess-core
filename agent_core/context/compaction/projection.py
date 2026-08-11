"""Compaction policy and system-prompt projection: when to compact and how
the persisted working context projects into a session's prompt."""
from __future__ import annotations

import os
from typing import Any

from ...storage.message_origin import syntheticUserMessage as _synthetic_user_message
from ...session.tool_boundaries import adjustStartToToolBoundary as _adjust_start_to_tool_boundary
from ...storage import getSessionCompactionsDir
from .documents import loadWorkingContextArtifact
from .prompts import renderWorkingContextPromptContext
from .shared import CONTINUATION_BRIDGE_MESSAGE, compaction_knob
from .utils import estimateMessageTokens, estimateTextTokens

def estimateWorkingContextTokens(session: dict[str, Any]) -> int:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return estimateMessageTokens(session.get("messages", []))
    cutoff = int(state.get("compactedThroughMessageIndex") or 0)
    recent_messages = session.get("messages", [])[cutoff:]
    return int(state.get("compactedContextTokenEstimate") or 0) + estimateMessageTokens(recent_messages)


def shouldAutoCompact(
    session: dict[str, Any],
    currentInputTokens: int | None = None,
    tokenLimit: int | None = None,
    targetTokenLimit: int | None = None,
) -> dict[str, Any]:
    current_token_estimate = currentInputTokens if currentInputTokens is not None else estimateWorkingContextTokens(session)
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


def buildMessagesForWorkingContext(session: dict[str, Any]) -> list[dict[str, Any]]:
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


async def buildCompactionSystemPromptContext(session: dict[str, Any]) -> str | None:
    projection = await _buildCompactionSystemPromptProjection(session)
    return projection["text"]


async def _buildCompactionSystemPromptProjection(
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
    if not _isSessionCompactionArtifactPath(session, working_context_path):
        return {
            "text": None,
            "applied": False,
            "originalTokens": 0,
            "projectedTokens": 0,
        }
    working_context = await loadWorkingContextArtifact(working_context_path)
    if not working_context or working_context.get("sessionId") != session.get("id"):
        return {
            "text": None,
            "applied": False,
            "originalTokens": 0,
            "projectedTokens": 0,
        }
    original = renderWorkingContextPromptContext(working_context)
    original_tokens = estimateTextTokens(original)
    return {
        "text": original,
        "applied": False,
        "originalTokens": original_tokens,
        "projectedTokens": original_tokens,
    }


def _isSessionCompactionArtifactPath(
    session: dict[str, Any],
    artifactPath: Any,
) -> bool:
    session_id = session.get("id")
    if not isinstance(session_id, str) or not isinstance(artifactPath, str) or not artifactPath:
        return False
    try:
        session_dir = os.path.realpath(getSessionCompactionsDir(session_id))
        resolved_artifact = os.path.realpath(artifactPath)
        return resolved_artifact != session_dir and os.path.commonpath(
            [session_dir, resolved_artifact]
        ) == session_dir
    except (OSError, RuntimeError, ValueError):
        return False
