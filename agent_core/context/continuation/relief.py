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
    pressure_level = classifyContextReliefPressure(pressure, reactive_attempt)
    modes = chooseContextReliefModes(
        {
            "session": session,
            "config": args.get("config") or {},
            "pressure": pressure,
            "pressureLevel": pressure_level,
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
        if isContextPressureRelieved(pressure, pressure_level, reactive_attempt):
            break

    normalized_reinjection_lines = normalizeReinjectionLines(reinjection_lines)
    reinjection_summary = " | ".join(normalized_reinjection_lines[:4]) if normalized_reinjection_lines else None
    return {
        "messages": messages,
        "summary": {
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
        },
    }


def chooseContextReliefModes(args: dict[str, Any]) -> list[str]:
    modes: list[str] = []
    session = args["session"]
    pressure = args["pressure"]
    pressure_level = args["pressureLevel"]
    retrieved_memory = args.get("retrievedMemory") or {"records": [], "tokenEstimate": 0}
    idle_gap_ms = _idle_gap_ms(session.get("updatedAt"))
    tool_payload = pressure["categories"]["currentTurnToolPayloadTokens"]
    repeated_file_tokens = pressure["categories"]["repeatedFileContentTokens"]
    strong_session_memory = len(retrieved_memory.get("records") or []) >= 4 and int(retrieved_memory.get("tokenEstimate") or 0) >= 500
    aggressive = bool(args.get("aggressive"))
    high_pressure = pressure_level in {"high", "critical"}
    critical_pressure = pressure_level == "critical"

    if idle_gap_ms >= RELIEF_IDLE_GAP_MS and tool_payload >= 3_000:
        modes.append("time_based_microcompact")
    elif (
        tool_payload >= RELIEF_PRESSURE_API_MICROCOMPACT_TOKENS
        or repeated_file_tokens >= 8_000
        or aggressive
        or int(args.get("reactiveAttempt") or 0) >= 1
        or high_pressure
    ):
        modes.append("api_microcompact" if supportsApiMicrocompact(args.get("config") or {}) else "microcompact")
    elif tool_payload >= RELIEF_PRESSURE_MICROCOMPACT_TOKENS or pressure["categories"]["lowValueTailTokens"] >= 2_000 or pressure_level == "elevated":
        modes.append("microcompact")

    if strong_session_memory and (
        pressure["totalTokenEstimate"] >= RELIEF_PRESSURE_SESSION_MEMORY_TOKENS
        or high_pressure
        or int(args.get("reactiveAttempt") or 0) >= 2
    ):
        modes.append("session_memory_compaction")

    if (
        pressure["totalTokenEstimate"] >= RELIEF_PRESSURE_PARTIAL_TOKENS
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
    if mode in {"microcompact", "time_based_microcompact"}:
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
                "detail": "time gap and tool-heavy tail triggered cheap context relief"
                if mode == "time_based_microcompact"
                else "tool-heavy tail compacted with deterministic microcompact",
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


def classifyContextReliefPressure(pressure: dict[str, Any], reactiveAttempt: int) -> str:
    categories = pressure["categories"]
    if (
        reactiveAttempt >= 2
        or pressure["totalTokenEstimate"] >= RELIEF_PRESSURE_SESSION_MEMORY_TOKENS
        or categories["currentTurnToolPayloadTokens"] >= 24_000
        or categories["repeatedFileContentTokens"] >= 16_000
    ):
        return "critical"
    if (
        reactiveAttempt >= 1
        or pressure["totalTokenEstimate"] >= RELIEF_PRESSURE_PARTIAL_TOKENS
        or categories["currentTurnToolPayloadTokens"] >= 14_000
        or categories["repeatedFileContentTokens"] >= 10_000
    ):
        return "high"
    if (
        pressure["totalTokenEstimate"] >= 55_000
        or categories["currentTurnToolPayloadTokens"] >= RELIEF_PRESSURE_MICROCOMPACT_TOKENS
        or categories["lowValueTailTokens"] >= 2_000
    ):
        return "elevated"
    return "low"


def isContextPressureRelieved(pressure: dict[str, Any], originalPressureLevel: str, reactiveAttempt: int) -> bool:
    if reactiveAttempt > 0:
        return False
    current = classifyContextReliefPressure(pressure, 0)
    if originalPressureLevel == "critical":
        return current in {"high", "elevated", "low"}
    if originalPressureLevel == "high":
        return current in {"elevated", "low"}
    if originalPressureLevel == "elevated":
        return current == "low"
    return True
