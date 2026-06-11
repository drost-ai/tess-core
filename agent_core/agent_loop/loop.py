from __future__ import annotations

import time as _time
import typing as _typing

if _typing.TYPE_CHECKING:
    from ..contracts.context import ToolContext

from .shared import *
from ..subagent_contract import filter_tools_for_subagent
from ..subagent_limits import build_subagent_tool_limit_result, check_subagent_runtime_limits, resolve_subagent_runtime_limits
from ..task_communication import deliver_queued_task_messages_to_session

async def runAgentPrompt(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args["session"]
    config = args["config"]
    prompt = args["prompt"]
    runtime = args["runtime"]
    provider_override = args.get("providerOverride")
    repair = repairSessionTranscript(session)
    if repair["repaired"]:
        await persistSession(session, config)
        _safe_emit(runtime, "transcript.repaired", {"session_id": session["id"], "truncated_from": repair.get("truncatedFrom"), "removed_count": repair["removedCount"], "missing_tool_use_ids": repair["missingToolUseIds"]})

    effective_provider = ensureProviderForModelSelection(session.get("provider") or config["provider"], args.get("modelOverride") or session["model"], config["provider"])
    session["provider"] = effective_provider
    provider_profile_name = resolveFallbackProviderProfileName(effective_provider, config.get("providerProfiles", {}), session.get("providerProfileName") or config.get("providerProfileName"))
    session["providerProfileName"] = provider_profile_name

    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    runtime_contract = metadata.get("subagentRuntimeContract") if isinstance(metadata, dict) else None
    tool_names = set(filterToolsByPolicy(config))
    runtime_tools = filter_tools_for_subagent(runtime.get("tools", []), runtime_contract)
    tool_registry = [tool for tool in runtime_tools if tool.name in tool_names]
    provider = provider_override or _create_provider(config, runtime)
    base_system_prompt = args.get("systemPromptOverride") or session["systemPrompt"]
    text_parts: list[str] = []
    total_usage: dict[str, Any] = {}
    usage_totals = _ensure_usage_totals(session)
    active_model = args.get("modelOverride") or session["model"]
    cost = 0.0
    tool_count = 0
    provider_call_index = 0
    turn_started_ms = int(_time.monotonic() * 1000)
    subagent_limits = resolve_subagent_runtime_limits(session, config)
    last_stable_snapshot = copy.deepcopy(session)

    def subagent_limit_state(tool_calls: int | None = None) -> dict[str, Any]:
        return {
            "providerCalls": provider_call_index,
            "toolCalls": tool_count if tool_calls is None else tool_calls,
            "costUsd": cost,
            "elapsedMs": int(_time.monotonic() * 1000) - turn_started_ms,
        }

    async def finish_subagent_limit(exceeded: dict[str, Any]) -> dict[str, Any]:
        text = "\n\n".join(text_parts).strip()
        _safe_emit(
            runtime,
            "subagent.limit_exceeded",
            {"session_id": session["id"], "reason": exceeded.get("reason"), "limit": exceeded.get("limit")},
        )
        await persistSession(session, config)
        return {
            "text": text,
            "toolCount": tool_count,
            "usage": total_usage,
            "model": active_model,
            "estimatedCostUsd": _round_usd(cost),
            "stopReason": exceeded.get("reason") or "subagent_limit",
        }

    async def cancel_turn() -> dict[str, Any]:
        _restore_session_state(session, last_stable_snapshot)
        await persistSession(session, config)
        _safe_emit(runtime, "turn.cancelled", {"session_id": session["id"], "model": active_model, "partial_text": "\n\n".join(text_parts).strip()})
        return {"text": "\n\n".join(text_parts).strip(), "toolCount": tool_count, "usage": total_usage, "model": active_model, "estimatedCostUsd": _round_usd(cost), "stopReason": "cancelled"}

    if _is_turn_cancelled(runtime):
        return await cancel_turn()

    if prompt.strip():
        session.setdefault("messages", []).append({"role": "user", "content": prompt})
        await persistSession(session, config)
        _safe_emit(runtime, "message.user", {"text": prompt, "session_id": session["id"]})

    usage_totals["turns"] += 1

    while True:
        if _is_turn_cancelled(runtime):
            return await cancel_turn()
        exceeded = check_subagent_runtime_limits(subagent_limits, subagent_limit_state())
        if exceeded and exceeded.get("reason") != "max_tool_calls":
            return await finish_subagent_limit(exceeded)
        delivered_messages = await deliver_queued_task_messages_to_session(session, config)
        if delivered_messages:
            _safe_emit(
                runtime,
                "subagent.mailbox.delivered",
                {
                    "session_id": session["id"],
                    "task_id": metadata.get("subagentTaskId") if isinstance(metadata, dict) else None,
                    "message_ids": [message.get("messageId") for message in delivered_messages],
                    "count": len(delivered_messages),
                },
            )
        try:
            prepared_request = await prepareProviderRequest(
                {
                    "session": session,
                    "config": config,
                    "runtime": runtime,
                    "baseSystemPrompt": base_system_prompt,
                    "toolRegistry": tool_registry,
                    "activeModel": active_model,
                    "prompt": prompt,
                    "reactiveAttempt": 0,
                }
            )
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise
        while True:
            try:
                _throw_if_turn_cancelled(runtime)
                if requestHasImageInputs(prepared_request["request"]) and not modelSupportsImageInputs(config, active_model):
                    response = unsupportedImageInputResponse(active_model)
                else:
                    response = await maybe_await(provider["send"](prepared_request["request"]))
                break
            except Exception as error:
                if _is_turn_abort_error(error, runtime.get("abortSignal")):
                    return await cancel_turn()
                if prepared_request.get("reactiveAttempt", 0) < 2 and isContextWindowFailure(error):
                    try:
                        prepared_request = await prepareProviderRequest(
                            {
                                "session": session,
                                "config": config,
                                "runtime": runtime,
                                "baseSystemPrompt": base_system_prompt,
                                "toolRegistry": tool_registry,
                                "activeModel": active_model,
                                "prompt": prompt,
                                "reactiveAttempt": int(prepared_request.get("reactiveAttempt") or 0) + 1,
                            }
                        )
                    except Exception as prepare_error:
                        if _is_turn_abort_error(prepare_error, runtime.get("abortSignal")):
                            return await cancel_turn()
                        raise
                    continue
                if isProviderRequestError(error):
                    session.setdefault("metadata", {})["lastProviderFailure"] = copy.deepcopy(error.record)
                    _safe_emit(runtime, "provider.failed", {"session_id": session["id"], **error.record})
                    await persistProviderFailure(session, config, error.record)
                raise

        if session.setdefault("metadata", {}).get("lastProviderFailure"):
            last = copy.deepcopy(session["metadata"]["lastProviderFailure"])
            recovered_at = _now()
            await persistProviderRecovery(session, config, {"provider": config["provider"], "recoveredAt": recovered_at, "attempts": last.get("attempt", 1), "lastFailure": last})
            session["metadata"].pop("lastProviderFailure", None)

        try:
            _throw_if_turn_cancelled(runtime)
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise
        active_model = response["model"]
        provider_call_index += 1
        _emit_provider_response_completed(
            runtime=runtime,
            session=session,
            config=config,
            response=response,
            prepared_request=prepared_request,
            call_index=provider_call_index,
        )
        session["messages"].append({"role": "assistant", "content": response["content"]})
        merge_usage(total_usage, response.get("usage") or {})
        _merge_usage_into_session(session, response.get("usage") or {})
        recordActualTokenUsage(session, response.get("usage") or {})
        cost += _estimate_usage_cost(response.get("usage") or {}, response["model"])

        text = "".join(block.get("text", "") for block in response["content"] if block.get("type") == "text")
        if text:
            text_parts.append(text)
            _safe_emit(runtime, "message.assistant", {"text": text, "model": response["model"], "session_id": session["id"]})

        tool_uses = [block for block in response["content"] if block.get("type") == "tool_use"]
        if not tool_uses:
            await persistSession(session, config)
            return {"text": "\n\n".join(text_parts).strip(), "toolCount": tool_count, "usage": total_usage, "model": active_model, "estimatedCostUsd": _round_usd(cost), "stopReason": response.get("stopReason")}

        tool_results: list[dict[str, Any] | None] = [None] * len(tool_uses)
        paused_for_approval = False
        paused_message = ""

        def build_context() -> ToolContext:
            return {
                "cwd": session["cwd"],
                "allowedRoots": config.get("allowedRoots", []),
                "config": config,
                "session": session,
                "interactive": runtime.get("interactive", False),
                "outputJson": runtime.get("emitEvent", lambda *_: None),
                "askQuestion": runtime.get("askQuestion", lambda *_: ""),
                "persistSession": lambda: persistSession(session, config),
                "updateCwd": lambda next_cwd: session.__setitem__("cwd", next_cwd),
                "getTools": lambda: runtime.get("tools", []),
                "daemon": runtime.get("daemon"),
                "abortSignal": runtime.get("abortSignal"),
            }

        async def run_single(tool_use: dict[str, Any]) -> dict[str, Any]:
            nonlocal tool_count
            _throw_if_turn_cancelled(runtime)
            if _requires_plan_approval(session) and tool_use["name"] not in PLAN_SAFE_TOOL_NAMES:
                message = f"{tool_use['name']} is not available while plan approval is still required."
                _safe_emit(runtime, "tool.completed", {"name": tool_use["name"], "output": message, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": message}
            invalid_tool_input = _invalid_tool_input_message(tool_use)
            if invalid_tool_input:
                tool_count += 1
                usage_totals["toolCalls"] += 1
                _safe_emit(runtime, "tool.completed", {"name": tool_use["name"], "output": invalid_tool_input, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": invalid_tool_input}
            tool = next((entry for entry in tool_registry if entry.name == tool_use["name"]), None)
            if not tool:
                _safe_emit(runtime, "tool.completed", {"name": tool_use["name"], "output": f"Tool not available: {tool_use['name']}", "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": f"Tool not available: {tool_use['name']}"}
            exceeded = check_subagent_runtime_limits(subagent_limits, subagent_limit_state())
            if exceeded and exceeded.get("reason") == "max_tool_calls":
                result = build_subagent_tool_limit_result(tool_use, exceeded)
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": result["content"], "error": True, "session_id": session["id"]})
                _safe_emit(runtime, "subagent.limit_exceeded", {"session_id": session["id"], "reason": exceeded.get("reason"), "limit": exceeded.get("limit")})
                return result
            tool_count += 1
            usage_totals["toolCalls"] += 1
            _safe_emit(runtime, "tool.started", {"name": tool.name, "input": tool_use.get("input") or {}, "session_id": session["id"]})
            try:
                result = await tool.handler(tool_use.get("input") or {}, build_context())
                _throw_if_turn_cancelled(runtime)
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": result.get("content", ""), "error": result.get("isError", False), "session_id": session["id"]})
                block = {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": result.get("isError"), "content": result.get("content", "")}
                if result.get("pause") == "approval":
                    block["_pauseMessage"] = (result.get("content") or "").strip() or "Approval required before continuing."
                return block
            except Exception as exc:
                if _is_turn_abort_error(exc, runtime.get("abortSignal")):
                    raise
                message = str(exc)
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": message, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": message}

        try:
            if _requires_plan_approval(session):
                for index, tool_use in enumerate(tool_uses):
                    result = await run_single(tool_use)
                    pause = result.pop("_pauseMessage", None)
                    tool_results[index] = result
                    if pause:
                        paused_for_approval = True
                        paused_message = pause
                        for skipped in range(index + 1, len(tool_uses)):
                            tool_results[skipped] = {"type": "tool_result", "tool_use_id": tool_uses[skipped]["id"], "is_error": True, "content": "Skipped: previous tool in the batch paused for approval."}
                        break
            else:
                outcomes = await asyncio.gather(*(run_single(tool_use) for tool_use in tool_uses))
                for index, result in enumerate(outcomes):
                    pause = result.pop("_pauseMessage", None)
                    if pause and not paused_for_approval:
                        paused_for_approval = True
                        paused_message = pause
                    tool_results[index] = result
            _throw_if_turn_cancelled(runtime)
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise

        session["messages"].append({"role": "user", "content": [result for result in tool_results if result is not None]})
        await persistSession(session, config)
        if paused_for_approval:
            if paused_message:
                text_parts.append(paused_message)
            return {"text": "\n\n".join(text_parts).strip(), "toolCount": tool_count, "usage": total_usage, "model": active_model, "estimatedCostUsd": _round_usd(cost), "stopReason": "approval_required"}

def extractAssistantText(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if block.get("type") == "text")


def _invalid_tool_input_message(tool_use: Any) -> str | None:
    if not isinstance(tool_use, dict):
        return None
    if tool_use.get("_agent_core_invalid_tool_input") is True:
        message = tool_use.get("_agent_core_invalid_tool_input_error")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "The provider streamed incomplete or invalid JSON for this tool call. Retry the tool call with complete valid JSON arguments."
    input_value = tool_use.get("input")
    if not isinstance(input_value, dict) or input_value.get("__agent_core_invalid_tool_input") is not True:
        return None
    message = input_value.get("error")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "The provider streamed incomplete or invalid JSON for this tool call. Retry the tool call with complete valid JSON arguments."


def _emit_provider_response_completed(
    *,
    runtime: dict[str, Any],
    session: dict[str, Any],
    config: dict[str, Any],
    response: dict[str, Any],
    prepared_request: dict[str, Any],
    call_index: int,
) -> None:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    token_tracking = metadata.get("tokenTracking") if isinstance(metadata.get("tokenTracking"), dict) else {}
    preflight = token_tracking.get("lastPreflight") if isinstance(token_tracking.get("lastPreflight"), dict) else {}
    runtime_restore = metadata.get("runtimeRestore") if isinstance(metadata.get("runtimeRestore"), dict) else {}
    continuity = prepared_request.get("continuity") if isinstance(prepared_request, dict) else {}
    relief = continuity.get("relief") if isinstance(continuity, dict) and isinstance(continuity.get("relief"), dict) else {}
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    _safe_emit(
        runtime,
        "provider.response_completed",
        {
            "session_id": session.get("id"),
            "provider": config.get("provider"),
            "provider_profile": session.get("providerProfileName") or config.get("providerProfileName"),
            "model": response.get("model"),
            "call_index": max(1, int(call_index or 1)),
            "usage": copy.deepcopy(usage),
            "stop_reason": response.get("stopReason"),
            "request_estimate_tokens": preflight.get("inputTokens"),
            "request_estimate_source": preflight.get("source"),
            "context_relief_mode": runtime_restore.get("lastContextReliefMode") or relief.get("selectedMode"),
            "context_relief_pressure": runtime_restore.get("lastContextReliefPressureLevel") or relief.get("pressureLevel"),
            "context_relief_kind": runtime_restore.get("lastContextReliefKind"),
            "context_relief_projection_hash": runtime_restore.get("lastContextReliefProjectionHash"),
            "context_relief_escalation_requested": bool(runtime_restore.get("contextReliefEscalationRequested")),
        },
    )
