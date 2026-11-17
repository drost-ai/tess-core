from __future__ import annotations

from .._utils import safe_int as _safe_int

import hashlib
import json

import re
from ..config import normalize_effort_for_model
from ..context.continuation import build_continuation_context
from ..context.token_accounting import (
    measure_request_input_tokens,
    record_preflight_token_measurement,
)
from ..providers.errors import is_provider_request_error
from ..providers.token_budget import computeProviderTokenBudget, resolveCompactionPolicy
from ..session.persistence import persist_session
from .._utils import now_iso as _now
from ..runtime.events.emit import safe_emit as _safe_emit
from .shared import model_supports_image_inputs, request_has_image_inputs
from typing import Any
from .checkpoints import maybe_auto_checkpoint_raw_session, maybe_auto_compact_session
from .context_budget import (
    apply_provider_token_budget as _apply_provider_token_budget,
    assess_context_headroom as _assess_context_headroom,
    enforce_context_headroom as _enforce_context_headroom,
    headroom_event_payload as _headroom_event_payload,
)
from ..context.request_preflight import (
    build_request_preflight as _build_request_preflight,
    image_measurement_event_fields as _image_measurement_event_fields,
    preflight_event_payload as _preflight_event_payload,
)
# Retained as a compatibility export from this module; dynamic_context owns use.

from .request_integrity import normalize_provider_request_messages as _normalize_provider_request_messages
from .dynamic_context import (
    dynamic_context_belongs_in_messages as _dynamic_context_belongs_in_messages,
    inject_system_context_into_messages as _inject_system_context_into_messages,
)
from ..payloads.image_budget import assert_image_context_capacity as _assert_image_context_capacity
from ..payloads.attachments import filter_images_for_provider_capabilities as _filter_images_for_provider_capabilities

async def prepare_provider_request(args: dict[str, Any]) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    runtime = args["runtime"]
    base_system_prompt = args["baseSystemPrompt"]
    tool_registry = args["toolRegistry"]
    active_model = args["activeModel"]
    prompt = args["prompt"]
    reactive_attempt = int(args.get("reactiveAttempt") or 0)
    last_compaction_outcome: dict[str, Any] | None = None

    raw_checkpoint_outcome = await maybe_auto_checkpoint_raw_session({"session": session, "config": config, "runtime": runtime})
    durable_compaction_applied = bool(raw_checkpoint_outcome and raw_checkpoint_outcome.get("status") == "completed")

    while True:
        request_context = await build_continuation_context(
            {
                "session": session,
                "config": config,
                "prompt": prompt,
                "aggressive": reactive_attempt > 0,
                "reactiveAttempt": reactive_attempt,
            }
        )
        # Relief is emitted and recorded per built attempt — including passes
        # a compaction continue discards — because the TS peer does exactly
        # this ahead of its measurement/compaction decision. Moving it after
        # the decision reads cleaner but changes the event stream (verified
        # against agent-provider.ts and the context-management golden).
        emit_continuation_relief(runtime, session, request_context)
        record_continuation_relief(session, request_context)
        system = base_system_prompt
        system_context = request_context.get("systemContext")
        if system_context and not _dynamic_context_belongs_in_messages(config):
            system = f"{system}\n\n{request_context['systemContext']}"
        # A model that cannot see answers an image the user has just sent with a refusal.
        # Images further back (sent before a switch to this model, say) are not a reason to
        # refuse: admission below replaces them with a note and the request goes ahead.
        newest_user = next(
            (m for m in reversed(request_context.get("messages") or []) if isinstance(m, dict) and m.get("role") == "user"),
            None,
        )
        unsupported_image_inputs = (
            newest_user is not None
            and request_has_image_inputs({"messages": [newest_user]})
            and not model_supports_image_inputs(config, active_model)
        )
        request_context = _normalize_request_context_messages(
            _admit_request_images(
                _inject_system_context_into_messages(request_context, system_context, config=config),
                config=config,
                runtime=runtime,
                session_id=session["id"],
            ),
            config=config,
            runtime=runtime,
            session_id=session["id"],
        )
        request = {
            "model": active_model,
            "effort": normalize_effort_for_model(config["provider"], active_model, session.get("effort") or config.get("effort")),
            "sessionId": session["id"],
            "system": system,
            "messages": request_context["messages"],
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema} for tool in tool_registry],
        }
        measurement = await measure_request_input_tokens(
            {
                "config": config,
                "request": request,
                "session": session,
                "abortSignal": runtime.get("abortSignal"),
                "deadlines": args.get("deadlines"),
                "emitEvent": lambda event, data: _safe_emit(runtime, event, data),
            }
        )
        compacted = None
        if not durable_compaction_applied:
            compacted = await maybe_auto_compact_session(
                {
                    "session": session,
                    "config": config,
                    "runtime": runtime,
                    "currentInputTokens": measurement["inputTokens"],
                    "currentInputTokensSource": measurement.get("source"),
                }
            )
            last_compaction_outcome = compacted
        if compacted and compacted.get("status") == "completed":
            durable_compaction_applied = True
            continue
        budget = computeProviderTokenBudget({"profile": config.get("providerProfile") or {}, "estimatedInputTokens": measurement["inputTokens"]})
        _assert_image_context_capacity(measurement, budget)
        compaction_policy = resolveCompactionPolicy(config.get("providerProfile") or {})
        headroom = _assess_context_headroom(
            measurement=measurement,
            policy=compaction_policy,
            outputBudget=budget,
            compactionOutcome=last_compaction_outcome,
        )
        _safe_emit(
            runtime,
            "compaction.headroom",
            _headroom_event_payload(session["id"], headroom),
        )
        preflight = _build_request_preflight(
            request=request,
            measurement=measurement,
            outputBudget=budget,
            contextBudget=headroom,
            continuation=request_context,
            session=session,
            baseSystemPrompt=base_system_prompt,
            preflightHints=args.get("requestPreflightHints"),
            dynamicContextInMessages=_dynamic_context_belongs_in_messages(config),
            compactionOutcome=last_compaction_outcome,
            providerProfileName=config.get("providerProfileName"),
        )
        measurement["requestPreflight"] = preflight
        record_preflight_token_measurement(session, measurement)
        _safe_emit(
            runtime,
            "provider.request_preflight",
            _preflight_event_payload(session["id"], preflight),
        )
        _enforce_context_headroom(config=config, assessment=headroom)
        request = _apply_provider_token_budget(request, budget, config)
        prepared_request = {
            "request": request,
            "continuity": request_context,
            "reactiveAttempt": reactive_attempt,
            "contextBudget": headroom,
            "unsupportedImageInputs": unsupported_image_inputs,
        }
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
                **_image_measurement_event_fields(measurement),
                "output_budget_safety_margin_tokens": budget.get("safetyMarginTokens"),
                "max_context_length": budget.get("maxContextLength"),
                "compaction_threshold_tokens": compaction_policy.get("compactionThresholdTokens"),
                "compaction_target_tokens": compaction_policy.get("compactionTargetTokens"),
                "compaction_output_tokens": compaction_policy.get("compactionOutputTokens"),
                "remaining_headroom_tokens": headroom.get("remainingHeadroomTokens"),
                "headroom_status": headroom.get("status"),
                "impossible_headroom_reason": headroom.get("reason"),
                "configured_max_output_tokens": budget.get("configuredMaxOutputTokens"),
                "effective_max_output_tokens": budget.get("effectiveMaxOutputTokens"),
                "remaining_output_tokens": budget.get("remainingOutputTokens"),
                "output_budget_clamped": budget.get("clamped"),
                "min_useful_output_tokens": budget.get("minUsefulOutputTokens"),
                "continuity_pressure": request_context.get("pressure"),
                "continuity_relief": request_context.get("relief"),
                "retrieved_memory_count": len((request_context.get("retrievedMemory") or {}).get("records", [])),
                "retrieved_memory_tokens": (request_context.get("retrievedMemory") or {}).get("tokenEstimate"),
            },
        )
        await persist_session(session, config)
        return prepared_request


def _admit_request_images(
    request_context: dict[str, Any], *, config: dict[str, Any], runtime: dict[str, Any], session_id: str
) -> dict[str, Any]:
    profile = config.get("providerProfile") if isinstance(config.get("providerProfile"), dict) else {}
    legacy_vision_contract = (
        isinstance(profile.get("supportsVision"), bool)
        and not isinstance(config.get("providerCapabilities"), dict)
        and not isinstance(profile.get("capabilities"), dict)
    )
    if legacy_vision_contract:
        return request_context
    return {
        **request_context,
        "messages": _filter_images_for_provider_capabilities(
            messages=request_context.get("messages") or [],
            config=config,
            emitEvent=lambda event, data: _safe_emit(runtime, event, data),
            sessionId=session_id,
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

def emit_continuation_relief(runtime: dict[str, Any], session: dict[str, Any] | str, continuity: dict[str, Any]) -> None:
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

def record_continuation_relief(session: dict[str, Any], continuity: dict[str, Any]) -> None:
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

def is_context_window_failure(error: BaseException) -> bool:
    if not is_provider_request_error(error):
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


