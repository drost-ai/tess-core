from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .compaction import buildCompactionSystemPromptContext, buildMessagesForWorkingContext
from .durable_memory import renderRetrievedMemoryPrompt, retrieveRelevantDurableMemory


RELIEF_PRESERVE_MIN_MESSAGES = 12
RELIEF_PREFERRED_RAW_TAIL_TOKENS = 24_000
RELIEF_MESSAGE_TEXT_CHAR_LIMIT = 1_200
RELIEF_TOOL_INPUT_CHAR_LIMIT = 500
RELIEF_TOOL_RESULT_CHAR_LIMIT = 750
LOW_VALUE_TOOL_NAMES = {"SetStatus", "TodoWrite", "Config"}
RELIEF_PRESSURE_MICROCOMPACT_TOKENS = 8_000
RELIEF_PRESSURE_API_MICROCOMPACT_TOKENS = 14_000
RELIEF_PRESSURE_PARTIAL_TOKENS = 95_000
RELIEF_PRESSURE_SESSION_MEMORY_TOKENS = 110_000
RELIEF_IDLE_GAP_MS = 15 * 60_000


async def buildContinuationContext(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    prompt = args.get("prompt", "")
    aggressive = bool(args.get("aggressive", False))
    reactive_attempt = max(0, int(args.get("reactiveAttempt") if args.get("reactiveAttempt") is not None else (1 if aggressive else 0)))

    compaction_context = await buildCompactionSystemPromptContext(session)
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
    return {
        "systemContext": system_context,
        "messages": relief["messages"],
        "pressure": classifyContextPressure({"session": session, "messages": relief["messages"], "systemContext": system_context}),
        "retrievedMemory": retrieved_memory,
        "relief": relief["summary"],
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
            if block.get("type") == "tool_use":
                tool_uses[str(block.get("id") or "")] = str(block.get("name") or "")
                continue
            if block.get("type") != "tool_result" or not isinstance(block.get("content"), str):
                continue
            tool_name = tool_uses.get(str(block.get("tool_use_id") or ""), "UnknownTool")
            token_estimate = estimateTextTokens(block["content"])
            categories["currentTurnToolPayloadTokens"] += token_estimate
            if tool_name in LOW_VALUE_TOOL_NAMES:
                categories["lowValueTailTokens"] += token_estimate
            if re.search(r'"content":|"filePath":|"startLine":|"totalLines":', block["content"]):
                categories["repeatedFileContentTokens"] += token_estimate

    return {
        "totalTokenEstimate": estimateMessagesTokens(messages) + estimateTextTokens(system_context),
        "categories": categories,
    }


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


def renderContinuationOverlay(session: dict[str, Any]) -> str | None:
    parts = [renderTodoOverlay(session), renderPlanOverlay(session)]
    return "\n\n".join(part for part in parts if part) or None


def renderTodoOverlay(session: dict[str, Any]) -> str | None:
    todos = session.get("todos") or []
    open_entries = []
    for todo in [todo for todo in todos if todo.get("status") != "completed"][:12]:
        status = "in progress" if todo.get("status") == "in_progress" else "pending"
        open_entries.append(f"{status}: {todo.get('activeForm') or todo.get('content')}")
    completed = [todo.get("content") for todo in todos if todo.get("status") == "completed"][-6:]
    if not open_entries and not completed:
        return None
    return "\n".join(
        [
            "<todo_state>",
            *[f"open={entry}" for entry in open_entries],
            *[f"completed={entry}" for entry in completed if entry],
            "</todo_state>",
        ]
    )


def renderPlanOverlay(session: dict[str, Any]) -> str | None:
    plan_state = (session.get("metadata") or {}).get("planMode")
    if not isinstance(plan_state, dict) or plan_state.get("state") == "inactive":
        return None
    lines = ["<plan_state>", f"state={plan_state.get('state')}"]
    if plan_state.get("requestId"):
        lines.append(f"request_id={plan_state['requestId']}")
    if plan_state.get("filePath"):
        lines.append(f"file={plan_state['filePath']}")
    if isinstance(plan_state.get("plan"), str) and plan_state["plan"].strip():
        lines.append(f"summary={truncate(plan_state['plan'].strip(), 800)}")
    lines.append("</plan_state>")
    return "\n".join(lines)


def choosePreservedRawStart(messages: list[dict[str, Any]]) -> int:
    fallback_start = max(0, len(messages) - RELIEF_PRESERVE_MIN_MESSAGES)
    last_plain_user_index = findLastPlainUserIndex(messages)
    if last_plain_user_index is None or last_plain_user_index <= 0:
        return fallback_start
    tail_tokens = estimateMessagesTokens(messages[last_plain_user_index:])
    return last_plain_user_index if tail_tokens <= RELIEF_PREFERRED_RAW_TAIL_TOKENS else fallback_start


def findLastPlainUserIndex(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            return index
        if not any(block.get("type") == "tool_result" for block in content):
            return index
    return None


def applyCompactPrefix(messages: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
    if len(messages) <= int(options["preserveMinMessages"]):
        return {"messages": messages, "summary": emptyReliefSummary()}
    preserved_raw_start_index = choosePreservedRawStart(messages)
    if preserved_raw_start_index <= 0:
        return {"messages": messages, "summary": emptyReliefSummary()}
    compacted_prefix = compactHistoricalTail(messages[:preserved_raw_start_index], options)
    next_messages = [*compacted_prefix, *messages[preserved_raw_start_index:]]
    tokens_saved_estimate = max(0, estimateMessagesTokens(messages) - estimateMessagesTokens(next_messages))
    dropped_message_count = preserved_raw_start_index - len(compacted_prefix)
    applied = tokens_saved_estimate > 0 or dropped_message_count > 0
    return {
        "messages": next_messages,
        "summary": {
            **emptyReliefSummary(),
            "applied": applied,
            "selectedMode": "microcompact",
            "appliedModes": ["microcompact"] if applied else [],
            "plannedModes": ["microcompact"] if applied else [],
            "pressureLevel": "elevated" if applied else "low",
            "preservedRawStartIndex": preserved_raw_start_index,
            "compactedMessageCount": len(compacted_prefix),
            "droppedMessageCount": dropped_message_count,
            "tokensSavedEstimate": tokens_saved_estimate,
        },
    }


def compactHistoricalTail(messages: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    tool_uses: dict[str, str] = {}
    compaction_state = {"seenFilePaths": set(), "seenCommands": set()}
    output = []
    for message in messages:
        compacted = compactMessageForContinuation(message, tool_uses, compaction_state, options)
        if compacted:
            output.append(compacted)
    return output


def compactMessageForContinuation(message: dict[str, Any], toolUses: dict[str, str], compactionState: dict[str, set[str]], options: dict[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": truncate(content, int(options["messageTextCharLimit"]))}
    if not isinstance(content, list):
        return message
    next_blocks: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type")
        if block_type == "text":
            next_blocks.append({**block, "text": truncate(str(block.get("text") or ""), int(options["messageTextCharLimit"]))})
            continue
        if block_type == "tool_use":
            tool_id = str(block.get("id") or "")
            tool_name = str(block.get("name") or "")
            toolUses[tool_id] = tool_name
            if tool_name in options["lowValueToolNames"]:
                continue
            input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
            next_blocks.append({**block, "input": compactToolInput(input_value, int(options["toolInputCharLimit"]))})
            continue
        if block_type == "tool_result":
            tool_name = toolUses.get(str(block.get("tool_use_id") or ""), "UnknownTool")
            if tool_name in options["lowValueToolNames"]:
                continue
            next_blocks.append(
                {
                    **block,
                    "content": summarizeToolResultForRelief(str(block.get("content") or ""), int(options["toolResultCharLimit"]), compactionState),
                }
            )
            continue
        next_blocks.append(block)
    return {**message, "content": next_blocks} if next_blocks else None


def applySessionMemoryCompaction(messages: list[dict[str, Any]], retrievedMemory: dict[str, Any], reactiveAttempt: int = 0) -> dict[str, Any]:
    if not retrievedMemory.get("records") or len(messages) <= RELIEF_PRESERVE_MIN_MESSAGES:
        return emptyCompactionResult(messages)
    keep_count = max(4, max(8, RELIEF_PRESERVE_MIN_MESSAGES // 2) - (reactiveAttempt * 2))
    preserved_raw_start_index = max(0, len(messages) - keep_count)
    if preserved_raw_start_index <= 0:
        return emptyCompactionResult(messages)
    removed_region = messages[:preserved_raw_start_index]
    next_messages = messages[preserved_raw_start_index:]
    region_summary = summarizeContinuationRegion(removed_region)
    reinjection_lines = normalizeReinjectionLines(
        [
            "Older raw context was reduced and replaced by retrieved durable memory plus the latest raw tail.",
            *renderRegionSummaryReinjectionLines(region_summary),
            *[
                f"durable memory: {truncate(collapseWhitespace(record.get('text') or ''), 180)}"
                for record in (retrievedMemory.get("records") or [])[:3]
            ],
        ]
    )
    return {
        "applied": True,
        "messages": next_messages,
        "preservedRawStartIndex": preserved_raw_start_index,
        "compactedMessageCount": 0,
        "droppedMessageCount": preserved_raw_start_index,
        "tokensSavedEstimate": max(0, estimateMessagesTokens(messages) - estimateMessagesTokens(next_messages)),
        "reinjectionLines": reinjection_lines,
        "reinjectionSummary": " | ".join(reinjection_lines[:4]),
    }


def applyPartialCompaction(messages: list[dict[str, Any]], reactiveAttempt: int = 0) -> dict[str, Any]:
    if len(messages) <= RELIEF_PRESERVE_MIN_MESSAGES + 4:
        return emptyCompactionResult(messages)
    keep_count = max(2, 4 - reactiveAttempt)
    preserved_raw_start_index = min(max(0, len(messages) - keep_count), choosePreservedRawStart(messages) + (reactiveAttempt * 4))
    if preserved_raw_start_index <= 2:
        return emptyCompactionResult(messages)
    region = messages[:preserved_raw_start_index]
    region_summary = summarizeContinuationRegion(region)
    reinjection_lines = normalizeReinjectionLines(renderRegionSummaryReinjectionLines(region_summary))
    summary_message = {"role": "user", "content": renderPartialCompactionSummary(region_summary, reactiveAttempt)}
    next_messages = [summary_message, *messages[preserved_raw_start_index:]]
    return {
        "applied": True,
        "messages": next_messages,
        "preservedRawStartIndex": preserved_raw_start_index,
        "compactedMessageCount": 1,
        "droppedMessageCount": max(0, preserved_raw_start_index - 1),
        "tokensSavedEstimate": max(0, estimateMessagesTokens(messages) - estimateMessagesTokens(next_messages)),
        "reinjectionLines": reinjection_lines,
        "reinjectionSummary": " | ".join(reinjection_lines[:4]),
    }


def renderPartialCompactionSummary(summary: dict[str, list[str]], reactiveAttempt: int = 0) -> str:
    lines = ["Partial continuation summary for older raw context in this session:"]
    entry_limit = 2 if reactiveAttempt >= 2 else 3 if reactiveAttempt >= 1 else 4
    appendSummarySection(lines, "Carry-forward requests", summary["requests"][:entry_limit])
    appendSummarySection(lines, "Relevant files", summary["files"][: entry_limit + 1])
    appendSummarySection(lines, "Commands and probes", summary["commands"][:entry_limit])
    appendSummarySection(lines, "Assistant findings", summary["findings"][:entry_limit])
    appendSummarySection(lines, "Observed failures", summary["failures"][:entry_limit])
    if len(lines) == 1:
        lines.append("- Older context was compacted, but no structured carry-forward items were extracted.")
    return "\n".join(lines)


def renderContextReliefPrompt(summary: dict[str, Any]) -> str | None:
    if not summary.get("applied") or not summary.get("appliedModes"):
        return None
    lines = [
        "<context_relief>",
        f"pressure_level={summary.get('pressureLevel')}",
        f"reactive_attempt={summary.get('reactiveAttempt')}",
        f"selected_mode={summary.get('selectedMode')}",
        *[f"planned_mode={mode}" for mode in summary.get("plannedModes", [])],
        *[f"applied_mode={mode}" for mode in summary.get("appliedModes", [])],
        f"tokens_saved_estimate={summary.get('tokensSavedEstimate')}",
    ]
    if summary.get("detail"):
        lines.append(f"detail={summary['detail']}")
    if summary.get("reinjectionSummary"):
        lines.append(f"reinjection_summary={summary['reinjectionSummary']}")
    lines.extend(f"reinjection={line}" for line in summary.get("reinjectionLines", []))
    lines.append("</context_relief>")
    return "\n".join(lines)


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


def compactToolInput(input: dict[str, Any], limit: int) -> dict[str, Any]:
    serialized = json.dumps(input, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= limit:
        return input
    return {"summary": truncate(serialized, limit)}


def summarizeToolResultForRelief(content: str, limit: int, compactionState: dict[str, set[str]] | None = None) -> str:
    try:
        parsed = json.loads(content)
    except Exception:
        return truncate(re.sub(r"\s+", " ", content), limit)
    if not isinstance(parsed, dict):
        return truncate(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), limit)

    file_record = asRecord(parsed.get("file"))
    if file_record and isinstance(file_record.get("filePath"), str) and isinstance(file_record.get("content"), str):
        seen = compactionState.setdefault("seenFilePaths", set()) if compactionState is not None else set()
        duplicate = file_record["filePath"] in seen
        seen.add(file_record["filePath"])
        preview = truncate(re.sub(r"\s+", " ", file_record["content"]), limit)
        return json.dumps(
            omitNone(
                {
                    "type": parsed.get("type") or "text",
                    "file": omitNone(
                        {
                            "filePath": file_record.get("filePath"),
                            "startLine": file_record.get("startLine"),
                            "totalLines": file_record.get("totalLines"),
                            "numLines": file_record.get("numLines"),
                            "preview": None if duplicate else preview,
                            "note": "Repeated file content omitted in microcompact history." if duplicate else None,
                        }
                    ),
                }
            ),
            indent=2,
            ensure_ascii=False,
        )

    if isinstance(parsed.get("command"), str) or "stdout" in parsed or "stderr" in parsed:
        command = parsed.get("command") if isinstance(parsed.get("command"), str) else None
        seen = compactionState.setdefault("seenCommands", set()) if compactionState is not None else set()
        duplicate = bool(command and command in seen)
        if command:
            seen.add(command)
        return json.dumps(
            omitNone(
                {
                    "command": truncate(command, 280) if command else None,
                    "cwd": parsed.get("cwd"),
                    "exitCode": parsed.get("exitCode"),
                    "timedOut": parsed.get("timedOut"),
                    "stdoutPreview": None
                    if duplicate
                    else truncate(re.sub(r"\s+", " ", parsed["stdout"]), 320)
                    if isinstance(parsed.get("stdout"), str)
                    else None,
                    "stderrPreview": truncate(re.sub(r"\s+", " ", parsed["stderr"]), 240) if isinstance(parsed.get("stderr"), str) else None,
                    "note": "Repeated command output omitted in microcompact history." if duplicate else None,
                }
            ),
            indent=2,
            ensure_ascii=False,
        )
    return truncate(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), limit)


def estimateMessagesTokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        value = f"{message.get('role')}:{content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, separators=(',', ':'))}"
        total += estimateTextTokens(value)
    return total


def estimateTodoAndPlanTokens(session: dict[str, Any]) -> int:
    todo_text = "\n".join(" ".join(str(part) for part in (todo.get("content"), todo.get("activeForm"), todo.get("status")) if part) for todo in session.get("todos", []))
    plan_state = (session.get("metadata") or {}).get("planMode")
    plan_text = plan_state.get("plan") if isinstance(plan_state, dict) and isinstance(plan_state.get("plan"), str) else ""
    return estimateTextTokens(f"{todo_text}\n{plan_text}")


def estimateTextTokens(value: str) -> int:
    return (len(value) + 3) // 4


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def collapseWhitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def asRecord(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def summarizeContinuationRegion(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    requests: list[str] = []
    files: list[str] = []
    commands: list[str] = []
    findings: list[str] = []
    failures: list[str] = []

    def push_line(target: list[str], value: str, limit: int = 220) -> None:
        normalized = truncate(re.sub(r"\s+", " ", value).strip(), limit)
        if normalized and normalized not in target:
            target.append(normalized)

    def push_unique(target: list[str], value: str) -> None:
        if value and value not in target:
            target.append(value)

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            push_line(requests if message.get("role") == "user" else findings, content, 220)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "text":
                push_line(requests if message.get("role") == "user" else findings, str(block.get("text") or ""), 220)
                continue
            if block.get("type") == "tool_use":
                input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
                file_path = extractFilePath(input_value)
                if file_path:
                    push_unique(files, file_path)
                command = extractCommand(input_value)
                if command:
                    push_unique(commands, truncate(command, 200))
                continue
            if block.get("type") == "tool_result":
                parsed = parseStructuredToolResult(str(block.get("content") or ""))
                if parsed.get("filePath"):
                    push_unique(files, parsed["filePath"])
                if parsed.get("command"):
                    push_unique(commands, truncate(parsed["command"], 200))
                if parsed.get("failure"):
                    push_unique(failures, parsed["failure"])
                elif block.get("is_error"):
                    push_unique(failures, truncate(re.sub(r"\s+", " ", str(block.get("content") or "")), 200))

    return {
        "requests": requests[-4:],
        "files": files[:6],
        "commands": commands[:5],
        "findings": findings[-4:],
        "failures": failures[-4:],
    }


def appendSummarySection(lines: list[str], heading: str, entries: list[str]) -> None:
    if not entries:
        return
    lines.append(f"{heading}:")
    lines.extend(f"- {entry}" for entry in entries)


def renderRegionSummaryReinjectionLines(summary: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    if summary["requests"]:
        lines.append(f"recent requests: {' | '.join(summary['requests'])}")
    if summary["files"]:
        lines.append(f"relevant files: {', '.join(summary['files'])}")
    if summary["commands"]:
        lines.append(f"commands and probes: {' | '.join(summary['commands'])}")
    if summary["findings"]:
        lines.append(f"assistant findings: {' | '.join(summary['findings'])}")
    if summary["failures"]:
        lines.append(f"observed failures: {' | '.join(summary['failures'])}")
    return lines


def normalizeReinjectionLines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", str(line)).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(truncate(normalized, 260))
        if len(output) >= 6:
            break
    return output


def extractFilePath(input: dict[str, Any]) -> str | None:
    for key in ("file_path", "path", "filePath", "target_file"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def extractCommand(input: dict[str, Any]) -> str | None:
    for key in ("command", "cmd", "shell_command"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def parseStructuredToolResult(content: str) -> dict[str, str]:
    try:
        parsed = json.loads(content)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    file_record = asRecord(parsed.get("file"))
    file_path = file_record.get("filePath") if file_record and isinstance(file_record.get("filePath"), str) else None
    command = parsed.get("command") if isinstance(parsed.get("command"), str) else None
    exit_code = parsed.get("exitCode") if isinstance(parsed.get("exitCode"), (int, float)) and not isinstance(parsed.get("exitCode"), bool) else None
    stderr = truncate(re.sub(r"\s+", " ", parsed["stderr"]), 180) if isinstance(parsed.get("stderr"), str) else None
    failure = f"command failed ({exit_code}){f': {stderr}' if stderr else ''}" if exit_code and exit_code != 0 else None
    return omitNone({"filePath": file_path, "command": command, "failure": failure})


def emptyReliefSummary(reactive_attempt: int = 0) -> dict[str, Any]:
    return {
        "applied": False,
        "selectedMode": "none",
        "appliedModes": [],
        "plannedModes": [],
        "pressureLevel": "low",
        "reactiveAttempt": reactive_attempt,
        "steps": [],
        "preservedRawStartIndex": 0,
        "compactedMessageCount": 0,
        "droppedMessageCount": 0,
        "tokensSavedEstimate": 0,
        "reinjectionLines": [],
        "reinjectionSummary": None,
        "detail": None,
    }


def emptyCompactionResult(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "applied": False,
        "messages": messages,
        "preservedRawStartIndex": 0,
        "compactedMessageCount": 0,
        "droppedMessageCount": 0,
        "tokensSavedEstimate": 0,
        "reinjectionLines": [],
    }


def omitNone(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _idle_gap_ms(updated_at: Any) -> int:
    if not isinstance(updated_at, str) or not updated_at:
        return 0
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() * 1000))


_estimate_text_tokens = estimateTextTokens
_estimate_messages_tokens = estimateMessagesTokens
_empty_relief = emptyReliefSummary
