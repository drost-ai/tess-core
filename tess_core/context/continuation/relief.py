from __future__ import annotations

from ...providers.token_budget import resolveCompactionPolicy
from .compaction import (
    apply_compact_prefix,
    apply_partial_compaction,
    choose_preserved_raw_start,
    compact_historical_tail,
)
from .tokens import classify_context_pressure
from .parsing import empty_relief_summary
from .rendering import normalize_reinjection_lines
from .shared import (
    relief_knob,
    LOW_VALUE_TOOL_NAMES,
)
from .tokens import estimate_messages_tokens
from typing import Any


def apply_lightweight_context_relief(messages: list[dict[str, Any]], session: dict[str, Any]) -> dict[str, Any]:
    if len(messages) <= relief_knob("context.relief_preserve_min_messages"):
        return {"messages": messages, "summary": empty_relief_summary()}
    preserved_raw_start_index = choose_preserved_raw_start(messages)
    if preserved_raw_start_index <= 0:
        return {"messages": messages, "summary": empty_relief_summary()}
    compacted_prefix = compact_historical_tail(
        messages[:preserved_raw_start_index],
        {
            "messageTextCharLimit": relief_knob("context.relief_message_text_chars"),
            "toolInputCharLimit": relief_knob("context.relief_tool_input_chars"),
            "toolResultCharLimit": relief_knob("context.relief_tool_result_chars"),
            "lowValueToolNames": LOW_VALUE_TOOL_NAMES,
        },
    )
    next_messages = [*compacted_prefix, *messages[preserved_raw_start_index:]]
    tokens_saved_estimate = max(0, estimate_messages_tokens(messages) - estimate_messages_tokens(next_messages))
    dropped_message_count = preserved_raw_start_index - len(compacted_prefix)
    applied = tokens_saved_estimate > 0 or dropped_message_count > 0
    return {
        "messages": next_messages,
        "summary": {
            **empty_relief_summary(),
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


def apply_context_relief_modes(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    messages = args["messages"]
    session = args["session"]
    retrieved_memory = args.get("retrievedMemory") or {"records": [], "tokenEstimate": 0}
    reactive_attempt = max(0, int(args.get("reactiveAttempt") if args.get("reactiveAttempt") is not None else (1 if args.get("aggressive") else 0)))
    pressure = classify_context_pressure({"session": session, "messages": messages})
    token_limit = resolveCompactionPolicy(
        (args.get("config") or {}).get("providerProfile") or {}
    ).get("compactionThresholdTokens")
    target_limit = token_limit
    pressure_level = classify_context_relief_pressure(pressure, reactive_attempt, tokenLimit=token_limit)
    modes = choose_context_relief_modes(
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
        result = apply_context_relief_mode(
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
        pressure = classify_context_pressure({"session": session, "messages": messages})
        if is_context_pressure_relieved(
            pressure,
            pressure_level,
            reactive_attempt,
            tokenLimit=target_limit,
        ):
            break

    normalized_reinjection_lines = normalize_reinjection_lines(reinjection_lines)
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


def choose_context_relief_modes(args: dict[str, Any]) -> list[str]:
    modes: list[str] = []
    session = args["session"]
    pressure = args["pressure"]
    pressure_level = args["pressureLevel"]
    token_limit = _token_limit_or_default(args.get("tokenLimit"))
    repeated_file_tokens = pressure["categories"]["repeatedFileContentTokens"]
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
        modes.append("api_microcompact" if supports_api_microcompact(args.get("config") or {}) else "microcompact")

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


def apply_context_relief_mode(args: dict[str, Any]) -> dict[str, Any]:
    mode = args["mode"]
    reactive_attempt = int(args.get("reactiveAttempt") or 0)
    if mode == "microcompact":
        result = apply_compact_prefix(
            args["messages"],
            {
                "preserveMinMessages": relief_knob("context.relief_preserve_min_messages"),
                "messageTextCharLimit": max(320, relief_knob("context.relief_message_text_chars") - (reactive_attempt * 220)),
                "toolInputCharLimit": max(120, relief_knob("context.relief_tool_input_chars") - (reactive_attempt * 120)),
                "toolResultCharLimit": max(180, relief_knob("context.relief_tool_result_chars") - (reactive_attempt * 180)),
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
        result = apply_compact_prefix(
            args["messages"],
            {
                "preserveMinMessages": max(6, relief_knob("context.relief_preserve_min_messages") - 2 - (reactive_attempt * 2)),
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
    if mode == "partial_compaction":
        result = apply_partial_compaction(args["messages"], reactive_attempt)
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


def supports_api_microcompact(config: dict[str, Any]) -> bool:
    return config.get("provider") in {"openai", "anthropic", "google"}


def classify_context_relief_pressure(pressure: dict[str, Any], reactiveAttempt: int, tokenLimit: int | None = None) -> str:
    if (
        reactiveAttempt >= 2
        or pressure["totalTokenEstimate"] >= _token_limit_or_default(tokenLimit)
    ):
        return "critical"
    if reactiveAttempt >= 1:
        return "high"
    return "low"


def is_context_pressure_relieved(
    pressure: dict[str, Any],
    originalPressureLevel: str,
    reactiveAttempt: int,
    tokenLimit: int | None = None,
) -> bool:
    if reactiveAttempt > 0:
        return False
    if pressure.get("totalTokenEstimate", 0) <= _token_limit_or_default(tokenLimit):
        return True
    current = classify_context_relief_pressure(pressure, 0, tokenLimit=tokenLimit)
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
    return parsed if parsed > 0 else relief_knob("context.relief_context_token_limit")
