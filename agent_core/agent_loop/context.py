from __future__ import annotations

import hashlib
import json

from .shared import *
from .checkpoints import maybeAutoCheckpointRawSession, maybeAutoCompactSession
from .request_integrity import normalizeProviderRequestMessages as _normalize_provider_request_messages
from ..providers.prompt_cache import resolvePromptCachePolicy

async def prepareProviderRequest(args: dict[str, Any]) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    runtime = args["runtime"]
    base_system_prompt = args["baseSystemPrompt"]
    tool_registry = args["toolRegistry"]
    active_model = args["activeModel"]
    prompt = args["prompt"]
    reactive_attempt = int(args.get("reactiveAttempt") or 0)
    prepared_request: dict[str, Any] | None = None

    await maybeAutoCheckpointRawSession({"session": session, "config": config, "runtime": runtime})

    for _attempt in range(4):
        request_context = await buildContinuationContext(
            {
                "session": session,
                "config": config,
                "prompt": prompt,
                "aggressive": reactive_attempt > 0,
                "reactiveAttempt": reactive_attempt,
            }
        )
        emitContinuationRelief(runtime, session, request_context)
        recordContinuationRelief(session, request_context)
        system = _apply_plan_mode_overlay(base_system_prompt, session)
        system_context = request_context.get("systemContext")
        if system_context and not _dynamic_context_belongs_in_messages(config):
            system = f"{system}\n\n{request_context['systemContext']}"
        request_context = _normalize_request_context_messages(
            _inject_system_context_into_messages(request_context, system_context, config=config),
            config=config,
            runtime=runtime,
            session_id=session["id"],
        )
        request = {
            "model": active_model,
            "effort": normalizeEffortForModel(config["provider"], active_model, session.get("effort") or config.get("effort")),
            "sessionId": session["id"],
            "system": system,
            "messages": request_context["messages"],
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema} for tool in tool_registry],
        }
        prepared_request = {"request": request, "continuity": request_context, "reactiveAttempt": reactive_attempt}
        measurement = await measureRequestInputTokens({"config": config, "request": request, "session": session, "abortSignal": runtime.get("abortSignal")})
        compacted = await maybeAutoCompactSession(
            {
                "session": session,
                "config": config,
                "runtime": runtime,
                "currentInputTokens": measurement["inputTokens"],
                "currentInputTokensSource": measurement.get("source"),
            }
        )
        if compacted:
            continue
        recordPreflightTokenMeasurement(session, measurement)
        _safe_emit(
            runtime,
            "provider.input_tokens",
            {
                "session_id": session["id"],
                "model": request["model"],
                "provider": config["provider"],
                "input_tokens": measurement["inputTokens"],
                "source": measurement["source"],
                "base_input_tokens": measurement.get("baseInputTokens"),
                "safety_margin_tokens": measurement.get("safetyMarginTokens"),
                "continuity_pressure": request_context.get("pressure"),
                "continuity_relief": request_context.get("relief"),
                "retrieved_memory_count": len((request_context.get("retrievedMemory") or {}).get("records", [])),
                "retrieved_memory_tokens": (request_context.get("retrievedMemory") or {}).get("tokenEstimate"),
            },
        )
        await persistSession(session, config)
        return prepared_request

    if prepared_request:
        return prepared_request

    request_context = await buildContinuationContext(
        {
            "session": session,
            "config": config,
            "prompt": prompt,
            "aggressive": reactive_attempt > 0,
            "reactiveAttempt": reactive_attempt,
        }
    )
    emitContinuationRelief(runtime, session, request_context)
    recordContinuationRelief(session, request_context)
    system = _apply_plan_mode_overlay(base_system_prompt, session)
    system_context = request_context.get("systemContext")
    if system_context and not _dynamic_context_belongs_in_messages(config):
        system = f"{system}\n\n{request_context['systemContext']}"
    request_context = _normalize_request_context_messages(
        _inject_system_context_into_messages(request_context, system_context, config=config),
        config=config,
        runtime=runtime,
        session_id=session["id"],
    )
    return {
        "request": {
            "model": active_model,
            "effort": normalizeEffortForModel(config["provider"], active_model, session.get("effort") or config.get("effort")),
            "sessionId": session["id"],
            "system": system,
            "messages": request_context["messages"],
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema} for tool in tool_registry],
        },
        "continuity": request_context,
        "reactiveAttempt": reactive_attempt,
    }


def _dynamic_context_belongs_in_messages(config: dict[str, Any]) -> bool:
    return bool(resolvePromptCachePolicy(config=config).get("enabled"))


def _inject_system_context_into_messages(
    request_context: dict[str, Any],
    system_context: Any,
    *,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not system_context or not _dynamic_context_belongs_in_messages(config):
        return request_context
    messages = copy.deepcopy(request_context.get("messages") or [])
    if not messages:
        return {**request_context, "messages": [_dynamic_context_message(system_context)]}

    dynamic_block = _dynamic_context_text_block(system_context)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            messages[index] = {**message, "content": f"{dynamic_block['text']}\n\n{content}"}
        elif isinstance(content, list):
            messages[index] = {**message, "content": [dynamic_block, *copy.deepcopy(content)]}
        else:
            messages[index] = {**message, "content": dynamic_block["text"]}
        return {**request_context, "messages": messages}

    messages.append(_dynamic_context_message(system_context))
    return {**request_context, "messages": messages}


def _dynamic_context_message(system_context: Any) -> dict[str, Any]:
    return {"role": "user", "content": _dynamic_context_text_block(system_context)["text"]}


def _dynamic_context_text_block(system_context: Any) -> dict[str, str]:
    text = str(system_context or "").strip()
    return {
        "type": "text",
        "text": "\n".join(
            [
                "[Dynamic continuation context]",
                text,
                "[/Dynamic continuation context]",
            ]
        ),
    }

def _normalize_request_context_messages(
    request_context: dict[str, Any],
    *,
    config: dict[str, Any],
    runtime: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    protocol_normalized = _normalize_provider_request_messages(
        config["provider"],
        request_context["messages"],
    )
    repair = protocol_normalized["repair"]
    if repair.get("changed"):
        _safe_emit(
            runtime,
            "provider.request_repaired",
            {
                "session_id": session_id,
                "provider": config["provider"],
                **repair,
            },
        )
    return {
        **request_context,
        "messages": protocol_normalized["messages"],
        "protocolRepair": repair,
    }

def emitContinuationRelief(runtime: dict[str, Any], session: dict[str, Any] | str, continuity: dict[str, Any]) -> None:
    session_id = session.get("id") if isinstance(session, dict) else session
    metadata = session.get("metadata") if isinstance(session, dict) else {}
    durable_checkpoint_available = bool((metadata or {}).get("compactionState"))
    compactions = (metadata or {}).get("compactions")
    relief = continuity.get("relief") or {}
    pressure = continuity.get("pressure") or {}
    relief_without_checkpoint = bool(
        not durable_checkpoint_available and relief.get("pressureLevel") in {"high", "critical"}
    )
    if relief.get("selectedMode") != "none":
        _safe_emit(
            runtime,
            "context_relief.selected",
            {
                "session_id": session_id,
                "mode": relief.get("selectedMode"),
                "reason": relief.get("detail"),
                "token_estimate": pressure.get("totalTokenEstimate"),
                "pressure_level": relief.get("pressureLevel"),
                "reactive_attempt": relief.get("reactiveAttempt"),
                "planned_modes": relief.get("plannedModes"),
                "durable_checkpoint_available": durable_checkpoint_available,
                "compactions": compactions,
                "relief_without_checkpoint": relief_without_checkpoint,
            },
        )
    if relief.get("appliedModes"):
        _safe_emit(
            runtime,
            "context_relief.applied",
            {
                "session_id": session_id,
                "mode": relief.get("selectedMode"),
                "modes": relief.get("appliedModes"),
                "tokens_saved_estimate": relief.get("tokensSavedEstimate"),
                "dropped_message_count": relief.get("droppedMessageCount"),
                "compacted_message_count": relief.get("compactedMessageCount"),
                "detail": relief.get("detail"),
                "pressure_level": relief.get("pressureLevel"),
                "reactive_attempt": relief.get("reactiveAttempt"),
                "reinjection_summary": relief.get("reinjectionSummary"),
                "reinjection_section_count": len(relief.get("reinjectionLines") or []),
                "durable_checkpoint_available": durable_checkpoint_available,
                "compactions": compactions,
                "relief_without_checkpoint": relief_without_checkpoint,
            },
        )

def recordContinuationRelief(session: dict[str, Any], continuity: dict[str, Any]) -> None:
    relief = continuity.get("relief") or {}
    metadata = session.setdefault("metadata", {})
    previous_restore = metadata.get("runtimeRestore") if isinstance(metadata.get("runtimeRestore"), dict) else {}
    selected_mode = str(relief.get("selectedMode") or "none")
    applied = bool(relief.get("applied") and selected_mode != "none")
    previous_mode = str(previous_restore.get("lastContextReliefMode") or "none")
    previous_count = _safe_int(previous_restore.get("recentContextReliefCount"))
    recent_count = previous_count + 1 if applied and previous_mode not in {"", "none", "full_checkpoint_compaction"} else (1 if applied else 0)
    relief_kind = "circuit_breaker" if applied else "none"
    if selected_mode == "full_checkpoint_compaction":
        relief_kind = "full_checkpoint_compaction"
    projection_hash = _context_relief_projection_hash(relief)
    escalation_requested = False
    metadata["runtimeRestore"] = {
        **(
            previous_restore
            or {
                "savedAt": _now(),
                "activeTasks": [],
                "restoredTaskCount": len(metadata.get("taskIds") or []),
            }
        ),
        "savedAt": _now(),
        "lastContextReliefMode": selected_mode,
        "lastContextReliefPressureLevel": relief.get("pressureLevel"),
        "lastContextReliefReactiveAttempt": relief.get("reactiveAttempt"),
        "lastContextReliefDetail": relief.get("detail"),
        "lastContextReliefReinjectionSummary": relief.get("reinjectionSummary"),
        "lastContextReliefKind": relief_kind,
        "lastContextReliefProjectionHash": projection_hash,
        "recentContextReliefCount": recent_count,
        "contextReliefEscalationRequested": escalation_requested,
        "contextReliefEscalationReason": "repeated_context_relief" if escalation_requested else "",
    }
    if metadata.get("compactionState"):
        metadata["compactionState"]["lastContextReliefMode"] = relief.get("selectedMode")
        metadata["compactionState"]["lastReliefModes"] = relief.get("appliedModes")
        metadata["compactionState"]["lastContextReliefPressureLevel"] = relief.get("pressureLevel")
        metadata["compactionState"]["lastReliefReactiveAttempt"] = relief.get("reactiveAttempt")
        metadata["compactionState"]["lastReliefDetail"] = relief.get("detail")
        metadata["compactionState"]["lastReinjectionSummary"] = relief.get("reinjectionSummary")

def isContextWindowFailure(error: BaseException) -> bool:
    if not isProviderRequestError(error):
        return False
    record = getattr(error, "record", {})
    detail = f"{record.get('rawMessage') or ''} {record.get('rawBody') or ''} {error}".lower()
    return bool(re.search(r"context|input token|prompt token|max token|maximum context length|too large|too long|context_length|request exceeds|input is too long|input too long", detail))


def _context_relief_projection_hash(relief: dict[str, Any]) -> str:
    payload = {
        "selectedMode": relief.get("selectedMode"),
        "appliedModes": relief.get("appliedModes"),
        "pressureLevel": relief.get("pressureLevel"),
        "reactiveAttempt": relief.get("reactiveAttempt"),
        "tokensSavedEstimate": relief.get("tokensSavedEstimate"),
        "droppedMessageCount": relief.get("droppedMessageCount"),
        "compactedMessageCount": relief.get("compactedMessageCount"),
        "reinjectionSummary": relief.get("reinjectionSummary"),
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0
