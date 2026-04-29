from __future__ import annotations

from .shared import *


def mergeCompactionUsageIntoSession(session: dict[str, Any], usage: dict[str, Any]) -> None:
    metadata = session.setdefault("metadata", {})
    current = metadata.setdefault(
        "usage",
        {"turns": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0},
    )
    current["inputTokens"] = int(current.get("inputTokens") or 0) + int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
    current["outputTokens"] = int(current.get("outputTokens") or 0) + int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
    current["cacheCreationInputTokens"] = int(current.get("cacheCreationInputTokens") or 0) + int(usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens") or 0)
    current["cacheReadInputTokens"] = int(current.get("cacheReadInputTokens") or 0) + int(usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens") or 0)


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
        handle.write(value)


def _write_json(path: str, value: Any) -> None:
    _write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
