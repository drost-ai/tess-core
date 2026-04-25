from __future__ import annotations

import json
from typing import Any

from .compaction import buildCompactionSystemPromptContext, buildMessagesForWorkingContext
from .durable_memory import renderRetrievedMemoryPrompt, retrieveRelevantDurableMemory


def _estimate_text_tokens(value: str) -> int:
    return (len(value) + 3) // 4


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_text_tokens(json.dumps(message, ensure_ascii=False)) for message in messages)


async def buildContinuationContext(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    prompt = args.get("prompt", "")
    compaction_context = await buildCompactionSystemPromptContext(session)
    retrieved = await retrieveRelevantDurableMemory({"session": session, "prompt": prompt})
    messages = buildMessagesForWorkingContext(session)
    relief = applyContextReliefModes({"session": session, "config": args.get("config", {}), "prompt": prompt, "messages": messages, "retrievedMemory": retrieved, "aggressive": args.get("aggressive", False), "reactiveAttempt": args.get("reactiveAttempt", 0)})
    memory_prompt = renderRetrievedMemoryPrompt(retrieved)
    system_context = "\n\n".join(part for part in (compaction_context, memory_prompt) if part and part.strip()) or None
    return {"systemContext": system_context, "messages": relief["messages"], "pressure": classifyContextPressure({"session": session, "messages": relief["messages"], "systemContext": system_context}), "retrievedMemory": retrieved, "relief": relief["summary"]}


def classifyContextPressure(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    messages = args.get("messages", [])
    system_context = args.get("systemContext") or ""
    categories = {"oldHistoryTokens": args.get("session", {}).get("metadata", {}).get("compactionState", {}).get("compactedContextTokenEstimate", 0), "currentTurnToolPayloadTokens": 0, "lowValueTailTokens": 0, "repeatedFileContentTokens": 0, "planTodoNoiseTokens": _estimate_text_tokens(system_context)}
    return {"totalTokenEstimate": _estimate_messages_tokens(messages) + _estimate_text_tokens(system_context), "categories": categories}


def applyLightweightContextRelief(messages: list[dict[str, Any]], session: dict[str, Any]) -> dict[str, Any]:
    if len(messages) <= 12:
        return {"messages": messages, "summary": _empty_relief()}
    preserved = max(0, len(messages) - 12)
    compacted = [{"role": "user", "content": "[Earlier transcript compacted for continuation.]"}]
    next_messages = compacted + messages[preserved:]
    return {"messages": next_messages, "summary": {**_empty_relief(), "applied": True, "selectedMode": "microcompact", "appliedModes": ["microcompact"], "plannedModes": ["microcompact"], "pressureLevel": "elevated", "preservedRawStartIndex": preserved, "compactedMessageCount": len(compacted), "droppedMessageCount": preserved - len(compacted), "tokensSavedEstimate": max(0, _estimate_messages_tokens(messages) - _estimate_messages_tokens(next_messages))}}


def applyContextReliefModes(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    messages = args["messages"]
    if args.get("aggressive") or args.get("reactiveAttempt", 0) > 0:
        return applyLightweightContextRelief(messages, args["session"])
    return {"messages": messages, "summary": _empty_relief(args.get("reactiveAttempt", 0))}


def _empty_relief(reactive_attempt: int = 0) -> dict[str, Any]:
    return {"applied": False, "selectedMode": "none", "appliedModes": [], "plannedModes": [], "pressureLevel": "low", "reactiveAttempt": reactive_attempt, "steps": [], "preservedRawStartIndex": 0, "compactedMessageCount": 0, "droppedMessageCount": 0, "tokensSavedEstimate": 0, "reinjectionLines": [], "reinjectionSummary": None, "detail": None}
