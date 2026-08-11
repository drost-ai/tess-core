from __future__ import annotations

from ..compaction import build_messages_for_working_context
from ..durable_memory import render_retrieved_memory_prompt, retrieve_relevant_durable_memory
from .relief import apply_context_relief_modes
from .tokens import classify_context_pressure
from .rendering import render_context_relief_prompt, render_continuation_overlay
from .tokens import estimateTextTokens
from typing import Any
from ..compaction.projection import _build_compaction_system_prompt_projection


async def build_continuation_context(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    prompt = args.get("prompt", "")
    aggressive = bool(args.get("aggressive", False))
    reactive_attempt = max(0, int(args.get("reactiveAttempt") if args.get("reactiveAttempt") is not None else (1 if aggressive else 0)))

    checkpoint_projection = await _build_compaction_system_prompt_projection(session)
    compaction_context = checkpoint_projection["text"]
    retrieved_memory = await retrieve_relevant_durable_memory({"session": session, "prompt": prompt})
    continuation_overlay = render_continuation_overlay(session)
    base_messages = build_messages_for_working_context(session)
    relief = apply_context_relief_modes(
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
    memory_prompt = render_retrieved_memory_prompt(retrieved_memory)
    relief_prompt = render_context_relief_prompt(relief["summary"])
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
        "pressure": classify_context_pressure({"session": session, "messages": relief["messages"], "systemContext": system_context}),
        "retrievedMemory": retrieved_memory,
        "relief": relief["summary"],
        "preflightSegments": preflight_segments,
    }
