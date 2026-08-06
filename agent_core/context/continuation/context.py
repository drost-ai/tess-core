from __future__ import annotations

from ..compaction import buildMessagesForWorkingContext
from ..durable_memory import renderRetrievedMemoryPrompt, retrieveRelevantDurableMemory
from .relief import applyContextReliefModes
from .tokens import classifyContextPressure
from .rendering import renderContextReliefPrompt, renderContinuationOverlay
from .tokens import estimateTextTokens
from typing import Any
from ..compaction.projection import _buildCompactionSystemPromptProjection


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
