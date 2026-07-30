from __future__ import annotations

from .shared import *


def applyLightweightContextRelief(messages: list[dict[str, Any]], session: dict[str, Any]) -> dict[str, Any]:
    if len(messages) <= RELIEF_PRESERVE_MIN_MESSAGES:
        return {"messages": messages, "summary": emptyReliefSummary()}
    preserved_raw_start_index = choosePreservedRawStart(messages)
    if preserved_raw_start_index <= 0:
        return {"messages": messages, "summary": emptyReliefSummary()}
    compacted_prefix = compactHistoricalTail(
        messages[:preserved_raw_start_index],
        {
            "messageTextCharLimit": RELIEF_MESSAGE_TEXT_CHAR_LIMIT,
            "toolInputCharLimit": RELIEF_TOOL_INPUT_CHAR_LIMIT,
            "toolResultCharLimit": RELIEF_TOOL_RESULT_CHAR_LIMIT,
            "lowValueToolNames": LOW_VALUE_TOOL_NAMES,
        },
    )
    next_messages = [*compacted_prefix, *messages[preserved_raw_start_index:]]
    tokens_saved_estimate = max(0, estimateMessagesTokens(messages) - estimateMessagesTokens(next_messages))
    dropped_message_count = preserved_raw_start_index - len(compacted_prefix)
    applied = tokens_saved_estimate > 0 or dropped_message_count > 0
    return {
        "messages": next_messages,
        "summary": {
            **emptyReliefSummary(),
            "applied": applied,
            "selectedMode": "microcompact" if applied else "none",
            "appliedModes": ["microcompact"] if applied else [],
            "plannedModes": ["microcompact"] if applied else [],
            "pressureLevel": "elevated" if applied else "low",
            "steps": [
                {
                    "mode": "microcompact",
                    "applied": True,
                    "compactedMessageCount": len(compacted_prefix),
                    "droppedMessageCount": dropped_message_count,
                    "tokensSavedEstimate": tokens_saved_estimate,
                }
            ]
            if applied
            else [],
            "preservedRawStartIndex": preserved_raw_start_index,
            "compactedMessageCount": len(compacted_prefix),
            "droppedMessageCount": dropped_message_count,
            "tokensSavedEstimate": tokens_saved_estimate,
        },
    }


def applyContextReliefModes(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    messages = args["messages"]
    session = args["session"]
    retrieved_memory = args.get("retrievedMemory") or {"records": [], "tokenEstimate": 0}
    reactive_attempt = max(0, int(args.get("reactiveAttempt") if args.get("reactiveAttempt") is not None else (1 if args.get("aggressive") else 0)))
    pressure = classifyContextPressure({"session": session, "messages": messages})
    token_limit = resolveCompactionPolicy(
        (args.get("config") or {}).get("providerProfile") or {}
    ).get("compactionThresholdTokens")
    target_limit = token_limit
    pressure_level = classifyContextReliefPressure(pressure, reactive_attempt, tokenLimit=token_limit)
    modes = chooseContextReliefModes(
        {
            "session": session,
            "config": args.get("config") or {},
            "pressure": pressure,
            "pressureLevel": pressure_level,
            "tokenLimit": token_limit,
            "retrievedMemory": retrieved_memory,
            "aggressive": args.get("aggressive"),
            "reactiveAttempt": reactive_attempt,
        }
    )

    steps: list[dict[str, Any]] = []
    reinjection_lines: list[str] = []
    selected_mode = "none"
    total_tokens_saved_estimate = 0
    total_compacted = 0
    total_dropped = 0
    preserved_raw_start_index = 0

    for mode in modes:
        result = applyContextReliefMode(
            {
                "mode": mode,
                "session": session,
                "messages": messages,
                "retrievedMemory": retrieved_memory,
                "reactiveAttempt": reactive_attempt,
            }
        )
        if not result.get("applied"):
            continue
        selected_mode = mode
        messages = result["messages"]
        step = result["step"]
        steps.append(step)
        reinjection_lines.extend(step.get("reinjectionLines") or [])
        total_tokens_saved_estimate += int(step.get("tokensSavedEstimate") or 0)
        total_compacted += int(step.get("compactedMessageCount") or 0)
        total_dropped += int(step.get("droppedMessageCount") or 0)
        preserved_raw_start_index = max(preserved_raw_start_index, int(result.get("preservedRawStartIndex") or 0))
        pressure = classifyContextPressure({"session": session, "messages": messages})
        if isContextPressureRelieved(
            pressure,
            pressure_level,
            reactive_attempt,
            tokenLimit=target_limit,
        ):
            break

    normalized_reinjection_lines = normalizeReinjectionLines(reinjection_lines)
    reinjection_summary = " | ".join(normalized_reinjection_lines[:4]) if normalized_reinjection_lines else None
    summary = {
        "applied": bool(steps),
        "selectedMode": selected_mode,
        "appliedModes": [step["mode"] for step in steps],
        "plannedModes": modes,
        "pressureLevel": pressure_level,
        "reactiveAttempt": reactive_attempt,
        "steps": steps,
        "preservedRawStartIndex": preserved_raw_start_index,
        "compactedMessageCount": total_compacted,
        "droppedMessageCount": total_dropped,
        "tokensSavedEstimate": total_tokens_saved_estimate,
        "detail": " · ".join(step.get("detail", "") for step in steps if step.get("detail")) or None,
        "reinjectionLines": normalized_reinjection_lines,
        "reinjectionSummary": reinjection_summary,
    }
    return {"messages": messages, "summary": summary}


def chooseContextReliefModes(args: dict[str, Any]) -> list[str]:
    modes: list[str] = []
    session = args["session"]
    pressure = args["pressure"]
    pressure_level = args["pressureLevel"]
    token_limit = _token_limit_or_default(args.get("tokenLimit"))
    retrieved_memory = args.get("retrievedMemory") or {"records": [], "tokenEstimate": 0}
    repeated_file_tokens = pressure["categories"]["repeatedFileContentTokens"]
    strong_session_memory = len(retrieved_memory.get("records") or []) >= 4 and int(retrieved_memory.get("tokenEstimate") or 0) >= 500
    aggressive = bool(args.get("aggressive"))
    scheduled = pressure["totalTokenEstimate"] >= token_limit
    reactive = aggressive or int(args.get("reactiveAttempt") or 0) >= 1
    if not scheduled and not reactive:
        return []
    high_pressure = pressure_level in {"high", "critical"}
    critical_pressure = pressure_level == "critical"

    if (
        scheduled
        or repeated_file_tokens >= 8_000
        or aggressive
        or int(args.get("reactiveAttempt") or 0) >= 1
        or high_pressure
    ):
        modes.append("api_microcompact" if supportsApiMicrocompact(args.get("config") or {}) else "microcompact")
    elif scheduled or pressure["categories"]["lowValueTailTokens"] >= 2_000:
        modes.append("microcompact")

    if strong_session_memory and (
        scheduled
        or high_pressure
        or int(args.get("reactiveAttempt") or 0) >= 2
    ):
        modes.append("session_memory_compaction")

    if (
        scheduled
        or high_pressure
        or (aggressive and pressure["totalTokenEstimate"] >= 70_000)
        or int(args.get("reactiveAttempt") or 0) >= 1
    ):
        modes.append("partial_compaction")

    if critical_pressure and "partial_compaction" not in modes:
        modes.append("partial_compaction")
    return list(dict.fromkeys(modes))


def applyContextReliefMode(args: dict[str, Any]) -> dict[str, Any]:
    mode = args["mode"]
    reactive_attempt = int(args.get("reactiveAttempt") or 0)
    if mode == "microcompact":
        result = applyCompactPrefix(
            args["messages"],
            {
                "preserveMinMessages": RELIEF_PRESERVE_MIN_MESSAGES,
                "messageTextCharLimit": max(320, RELIEF_MESSAGE_TEXT_CHAR_LIMIT - (reactive_attempt * 220)),
                "toolInputCharLimit": max(120, RELIEF_TOOL_INPUT_CHAR_LIMIT - (reactive_attempt * 120)),
                "toolResultCharLimit": max(180, RELIEF_TOOL_RESULT_CHAR_LIMIT - (reactive_attempt * 180)),
                "lowValueToolNames": LOW_VALUE_TOOL_NAMES,
            },
        )
        return {
            "applied": result["summary"]["applied"],
            "messages": result["messages"],
            "step": {
                "mode": mode,
                "applied": result["summary"]["applied"],
                "compactedMessageCount": result["summary"]["compactedMessageCount"],
                "droppedMessageCount": result["summary"]["droppedMessageCount"],
                "tokensSavedEstimate": result["summary"]["tokensSavedEstimate"],
                "detail": "tool-heavy tail compacted with deterministic microcompact",
                "reinjectionLines": [],
            },
            "preservedRawStartIndex": result["summary"]["preservedRawStartIndex"],
        }
    if mode == "api_microcompact":
        result = applyCompactPrefix(
            args["messages"],
            {
                "preserveMinMessages": max(6, RELIEF_PRESERVE_MIN_MESSAGES - 2 - (reactive_attempt * 2)),
                "messageTextCharLimit": max(260, 900 - (reactive_attempt * 200)),
                "toolInputCharLimit": max(120, 320 - (reactive_attempt * 80)),
                "toolResultCharLimit": max(180, 420 - (reactive_attempt * 120)),
                "lowValueToolNames": LOW_VALUE_TOOL_NAMES,
            },
        )
        return {
            "applied": result["summary"]["applied"],
            "messages": result["messages"],
            "step": {
                "mode": "api_microcompact",
                "applied": result["summary"]["applied"],
                "compactedMessageCount": result["summary"]["compactedMessageCount"],
                "droppedMessageCount": result["summary"]["droppedMessageCount"],
                "tokensSavedEstimate": result["summary"]["tokensSavedEstimate"],
                "detail": "provider-aware aggressive microcompact trimmed tool-heavy context",
                "reinjectionLines": [],
            },
            "preservedRawStartIndex": result["summary"]["preservedRawStartIndex"],
        }
    if mode == "session_memory_compaction":
        result = applySessionMemoryCompaction(args["messages"], args.get("retrievedMemory") or {"records": [], "tokenEstimate": 0}, reactive_attempt)
        return {
            "applied": result["applied"],
            "messages": result["messages"],
            "step": {
                "mode": "session_memory_compaction",
                "applied": result["applied"],
                "compactedMessageCount": result["compactedMessageCount"],
                "droppedMessageCount": result["droppedMessageCount"],
                "tokensSavedEstimate": result["tokensSavedEstimate"],
                "detail": "older raw context collapsed into retrieved durable session memory",
                "requiresReinjection": True,
                "reinjectionLines": result["reinjectionLines"],
                "reinjectionSummary": result.get("reinjectionSummary"),
            },
            "preservedRawStartIndex": result["preservedRawStartIndex"],
        }
    if mode == "partial_compaction":
        result = applyPartialCompaction(args["messages"], reactive_attempt)
        return {
            "applied": result["applied"],
            "messages": result["messages"],
            "step": {
                "mode": "partial_compaction",
                "applied": result["applied"],
                "compactedMessageCount": result["compactedMessageCount"],
                "droppedMessageCount": result["droppedMessageCount"],
                "tokensSavedEstimate": result["tokensSavedEstimate"],
                "detail": "older continuation region collapsed into a bounded partial summary",
                "requiresReinjection": True,
                "reinjectionLines": result["reinjectionLines"],
                "reinjectionSummary": result.get("reinjectionSummary"),
            },
            "preservedRawStartIndex": result["preservedRawStartIndex"],
        }
    return {
        "applied": False,
        "messages": args["messages"],
        "step": {"mode": "none", "applied": False, "compactedMessageCount": 0, "droppedMessageCount": 0, "tokensSavedEstimate": 0},
        "preservedRawStartIndex": 0,
    }


def supportsApiMicrocompact(config: dict[str, Any]) -> bool:
    return config.get("provider") in {"openai", "anthropic"}


def classifyContextReliefPressure(pressure: dict[str, Any], reactiveAttempt: int, tokenLimit: int | None = None) -> str:
    if (
        reactiveAttempt >= 2
        or pressure["totalTokenEstimate"] >= _token_limit_or_default(tokenLimit)
    ):
        return "critical"
    if reactiveAttempt >= 1:
        return "high"
    return "low"


def isContextPressureRelieved(
    pressure: dict[str, Any],
    originalPressureLevel: str,
    reactiveAttempt: int,
    tokenLimit: int | None = None,
) -> bool:
    if reactiveAttempt > 0:
        return False
    if pressure.get("totalTokenEstimate", 0) <= _token_limit_or_default(tokenLimit):
        return True
    current = classifyContextReliefPressure(pressure, 0, tokenLimit=tokenLimit)
    if originalPressureLevel == "critical":
        return current in {"high", "low"}
    if originalPressureLevel == "high":
        return current == "low"
    return True


def _token_limit_or_default(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except Exception:
        parsed = 0
    return parsed if parsed > 0 else RELIEF_CONTEXT_TOKEN_LIMIT
