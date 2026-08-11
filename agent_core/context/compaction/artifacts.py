from __future__ import annotations

import json
import os
from ...storage import (
    compaction_checkpoint_path,
    compaction_manifest_path,
    get_session_compactions_dir,
)
from typing import Any
from ...payloads.media_persistence import sanitize_media_for_persistence as _sanitize_media_for_persistence
from ...providers.usage_costs import merge_usage_into_session as _merge_usage_into_session


def merge_compaction_usage_into_session(session: dict[str, Any], usage: dict[str, Any]) -> None:
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


def build_artifact_paths(sessionId: str, generation: int) -> dict[str, str]:
    directory = get_session_compactions_dir(sessionId)
    return {
        "manifest": compaction_manifest_path(sessionId),
        "summary": os.path.join(directory, "summary.md"),
        "state": os.path.join(directory, "state.json"),
        "workingContext": os.path.join(directory, "working-context.json"),
        "decisions": os.path.join(directory, "decisions.md"),
        "todos": os.path.join(directory, "todos.json"),
        "activity": os.path.join(directory, "activity.md"),
        "checkpoint": compaction_checkpoint_path(sessionId, generation),
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
