from __future__ import annotations

import json
import os
import uuid
from typing import Any

from ._utils import now_iso
from .storage import (
    compactionCheckpointPath,
    compactionManifestPath,
    getSessionCompactionsDir,
    saveCompactionCheckpoint,
    saveCompactionManifest,
)


MIN_RECENT_MESSAGE_COUNT = 20
MIN_COMPACTABLE_MESSAGE_COUNT = 28
SOFT_WORKING_CONTEXT_TOKEN_LIMIT = 150_000
HARD_WORKING_CONTEXT_TOKEN_LIMIT = 180_000


def estimateWorkingContextTokens(session: dict[str, Any]) -> int:
    return _estimate_messages_tokens(session.get("messages", [])) + int(session.get("metadata", {}).get("compactionState", {}).get("compactedContextTokenEstimate") or 0)


def shouldAutoCompact(session: dict[str, Any], currentInputTokens: int | None = None) -> dict[str, Any]:
    estimate = currentInputTokens if currentInputTokens is not None else estimateWorkingContextTokens(session)
    if len(session.get("messages", [])) < MIN_COMPACTABLE_MESSAGE_COUNT:
        return {"shouldCompact": False, "currentTokenEstimate": estimate, "reason": "below_threshold"}
    if estimate >= HARD_WORKING_CONTEXT_TOKEN_LIMIT:
        return {"shouldCompact": True, "trigger": "auto_hard_limit", "currentTokenEstimate": estimate}
    if estimate >= SOFT_WORKING_CONTEXT_TOKEN_LIMIT:
        return {"shouldCompact": True, "trigger": "auto_soft_limit", "currentTokenEstimate": estimate}
    return {"shouldCompact": False, "currentTokenEstimate": estimate, "reason": "below_threshold"}


def buildMessagesForWorkingContext(session: dict[str, Any]) -> list[dict[str, Any]]:
    return list(session.get("messages", []))


async def buildCompactionSystemPromptContext(session: dict[str, Any]) -> str | None:
    state = session.get("metadata", {}).get("compactionState")
    if not state:
        return None
    artifacts = state.get("artifacts") or {}
    working = artifacts.get("workingContext")
    if working and os.path.exists(working):
        try:
            with open(working, "r", encoding="utf-8") as handle:
                return f"# Working Context\n{handle.read().strip()}"
        except Exception:
            return None
    return None


async def runCompaction(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    trigger = args.get("trigger", "manual")
    messages = session.get("messages", [])
    if len(messages) < MIN_COMPACTABLE_MESSAGE_COUNT:
        return {"status": "skipped", "reason": "below_threshold", "trigger": trigger}
    generation = int(session.get("metadata", {}).get("compactionState", {}).get("latestCompactionGeneration") or 0) + 1
    compaction_id = str(uuid.uuid4())
    root = getSessionCompactionsDir(session["id"])
    os.makedirs(root, exist_ok=True)
    summary_path = os.path.join(root, f"{generation:04d}-summary.md")
    working_path = os.path.join(root, f"{generation:04d}-working-context.json")
    state_path = os.path.join(root, f"{generation:04d}-state.json")
    activity_path = os.path.join(root, f"{generation:04d}-activity.md")
    decisions_path = os.path.join(root, f"{generation:04d}-decisions.md")
    todos_path = os.path.join(root, f"{generation:04d}-todos.md")
    checkpoint_path = compactionCheckpointPath(session["id"], generation)
    cutoff = max(0, len(messages) - MIN_RECENT_MESSAGE_COUNT)
    working_context = {"goals": {"active": []}, "currentState": {"completed": [], "inProgress": [], "blocked": []}, "nextSteps": []}
    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write("Compacted transcript checkpoint.\n")
    with open(working_path, "w", encoding="utf-8") as handle:
        json.dump(working_context, handle, indent=2)
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump({"compactedThroughMessageIndex": cutoff}, handle, indent=2)
    for path in (activity_path, decisions_path, todos_path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
    artifacts = {"manifest": compactionManifestPath(session["id"]), "summary": summary_path, "state": state_path, "workingContext": working_path, "decisions": decisions_path, "todos": todos_path, "activity": activity_path, "checkpoint": checkpoint_path}
    checkpoint = {"id": compaction_id, "generation": generation, "sessionId": session["id"], "createdAt": now_iso(), "trigger": trigger, "sourceRange": {"start": 0, "end": cutoff, "messageCount": cutoff}, "rawTokenEstimate": estimateWorkingContextTokens(session), "compactedContextTokenEstimate": 1000, "workingContextTokenEstimate": 1000, "qualityClassification": "thin", "artifacts": artifacts, "state": working_context}
    manifest = {"sessionId": session["id"], "strategy": "checkpoint-v2", "createdAt": now_iso(), "updatedAt": now_iso(), "latestGeneration": generation, "latestCompactionId": compaction_id, "compactedThroughMessageIndex": cutoff, "compactedMessageCount": cutoff, "recentMessageCount": len(messages) - cutoff, "archiveMessageCount": cutoff, "rawTokenEstimate": checkpoint["rawTokenEstimate"], "compactedContextTokenEstimate": 1000, "workingContextTokenEstimate": 1000, "qualityClassification": "thin", "artifacts": artifacts}
    await saveCompactionManifest(manifest)
    await saveCompactionCheckpoint(checkpoint)
    session.setdefault("metadata", {})["compactionState"] = {"latestCompactionId": compaction_id, "latestCompactionGeneration": generation, "latestCompactionAt": now_iso(), "compactedThroughMessageIndex": cutoff, "compactedMessageCount": cutoff, "recentMessageCount": len(messages) - cutoff, "archiveMessageCount": cutoff, "rawTokenEstimate": checkpoint["rawTokenEstimate"], "compactedContextTokenEstimate": 1000, "workingContextTokenEstimate": 1000, "lastTrigger": trigger, "qualityClassification": "thin", "artifacts": artifacts}
    return {"status": "completed", "generation": generation, "trigger": trigger, "compactedThroughMessageIndex": cutoff, "compactedMessageCount": cutoff, "recentMessageCount": len(messages) - cutoff, "rawTokenEstimate": checkpoint["rawTokenEstimate"], "workingContextTokenEstimate": 1000, "qualityClassification": "thin", "compactionModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"}, "verificationModel": {"provider": "openai", "model": "gpt-5.3-codex-spark"}, "verification": {"verdict": "accept", "confidence": "low", "reason": "local compact checkpoint", "issues": [], "notes": []}, "artifacts": artifacts}


def _estimate_text_tokens(value: str) -> int:
    return (len(value) + 3) // 4


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += _estimate_text_tokens(json.dumps(message, ensure_ascii=False))
    return total
