from __future__ import annotations

from .shared import *
from ..compaction.orchestration import (
    _buildCompactionSystemPromptProjection,
)


async def buildContinuationContext(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    prompt = args.get("prompt", "")
    aggressive = bool(args.get("aggressive", False))
    reactive_attempt = max(0, int(args.get("reactiveAttempt") if args.get("reactiveAttempt") is not None else (1 if aggressive else 0)))

    checkpoint_projection = await _buildCompactionSystemPromptProjection(session)
    compaction_context = checkpoint_projection["text"]
    retrieved_memory = await retrieveRelevantDurableMemory({"session": session, "prompt": prompt})
    continuation_overlay = renderContinuationOverlay(session)
    base_messages = buildMessagesForWorkingContext(session)
    relief = applyContextReliefModes(
        {
            "session": session,
            "config": args.get("config", {}),
            "prompt": prompt,
            "messages": base_messages,
            "retrievedMemory": retrieved_memory,
            "aggressive": aggressive,
            "reactiveAttempt": reactive_attempt,
        }
    )
    memory_prompt = renderRetrievedMemoryPrompt(retrieved_memory)
    relief_prompt = renderContextReliefPrompt(relief["summary"])
    system_context = "\n\n".join(
        part.strip()
        for part in (compaction_context, continuation_overlay, memory_prompt, relief_prompt)
        if isinstance(part, str) and part.strip()
    ) or None
    preflight_segments = {
        "checkpointTokens": estimateTextTokens(compaction_context or ""),
        "continuationOverlayTokens": estimateTextTokens(continuation_overlay or ""),
        "retrievedMemoryTokens": estimateTextTokens(memory_prompt or ""),
        "reliefTokens": estimateTextTokens(relief_prompt or ""),
    }
    return {
        "systemContext": system_context,
        "messages": relief["messages"],
        "pressure": classifyContextPressure({"session": session, "messages": relief["messages"], "systemContext": system_context}),
        "retrievedMemory": retrieved_memory,
        "relief": relief["summary"],
        "preflightSegments": preflight_segments,
    }


def classifyContextPressure(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {}
    messages = args.get("messages") or []
    system_context = args.get("systemContext") or ""
    categories = {
        "oldHistoryTokens": ((session.get("metadata") or {}).get("compactionState") or {}).get("compactedContextTokenEstimate") or 0,
        "currentTurnToolPayloadTokens": 0,
        "lowValueTailTokens": 0,
        "repeatedFileContentTokens": 0,
        "planTodoNoiseTokens": estimateTextTokens(system_context) + estimateTodoAndPlanTokens(session),
    }

    tool_uses: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_uses[str(block.get("id") or "")] = str(block.get("name") or "")
                continue
            if block.get("type") != "tool_result":
                continue
            content_text = _tool_result_content_string(block.get("content"))
            tool_name = tool_uses.get(str(block.get("tool_use_id") or ""), "UnknownTool")
            token_estimate = estimateTextTokens(content_text)
            categories["currentTurnToolPayloadTokens"] += token_estimate
            if tool_name in LOW_VALUE_TOOL_NAMES:
                categories["lowValueTailTokens"] += token_estimate
            if re.search(r'"content":|"filePath":|"startLine":|"totalLines":', content_text):
                categories["repeatedFileContentTokens"] += token_estimate

    return {
        "totalTokenEstimate": estimateMessagesTokens(messages) + estimateTextTokens(system_context),
        "categories": categories,
    }
