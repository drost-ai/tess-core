from __future__ import annotations

import json
import os
from ...storage import (
    compactionCheckpointPath,
    compactionManifestPath,
    getSessionCompactionsDir,
)
from typing import Any
from ...payloads.media_persistence import sanitizeMediaForPersistence as _sanitize_media_for_persistence
from ...providers.usage_costs import mergeUsageIntoSession as _merge_usage_into_session


def mergeCompactionUsageIntoSession(session: dict[str, Any], usage: dict[str, Any]) -> None:
    _merge_usage_into_session(
        session,
        {
            "input_tokens": usage.get("input_tokens", usage.get("inputTokens")),
            "output_tokens": usage.get("output_tokens", usage.get("outputTokens")),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", usage.get("cacheCreationInputTokens")),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", usage.get("cacheReadInputTokens")),
            "cache_creation_5m_input_tokens": usage.get("cache_creation_5m_input_tokens", usage.get("cacheCreation5mInputTokens")),
            "cache_creation_1h_input_tokens": usage.get("cache_creation_1h_input_tokens", usage.get("cacheCreation1hInputTokens")),
            "cache_creation_ttl_detail_complete": usage.get("cache_creation_ttl_detail_complete", usage.get("cacheCreationTtlDetailComplete")),
        },
    )


def buildArtifactPaths(sessionId: str, generation: int) -> dict[str, str]:
    directory = getSessionCompactionsDir(sessionId)
    return {
        "manifest": compactionManifestPath(sessionId),
        "summary": os.path.join(directory, "summary.md"),
        "state": os.path.join(directory, "state.json"),
        "workingContext": os.path.join(directory, "working-context.json"),
        "decisions": os.path.join(directory, "decisions.md"),
        "todos": os.path.join(directory, "todos.json"),
        "plan": os.path.join(directory, "plan.md"),
        "activity": os.path.join(directory, "activity.md"),
        "checkpoint": compactionCheckpointPath(sessionId, generation),
    }


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""


def _write_text(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_sanitize_media_for_persistence(value))


def _write_json(path: str, value: Any) -> None:
    _write_text(
        path,
        json.dumps(
            _sanitize_media_for_persistence(value),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
