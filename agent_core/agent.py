from __future__ import annotations

import asyncio
import copy
from typing import Any

from ._utils import maybe_await, merge_usage
from .config import (
    buildPlanModeSystemPrompt,
    ensureProviderForModelSelection,
    filterToolsByPolicy,
    normalizeEffortForModel,
    resolveConfig,
    resolveFallbackProviderProfileName,
)
from .continuation import buildContinuationContext
from .provider_errors import fallbackEligibleProviderFailure, isProviderRequestError
from .providers.anthropic import sendAnthropicMessagesRequest
from .providers.openai import sendOpenAiRequest
from .session import persistProviderFailure, persistProviderRecovery, persistSession, repairSessionTranscript
from .token_accounting import measureRequestInputTokens, recordActualTokenUsage, recordPreflightTokenMeasurement


MODEL_PRICING = {
    "claude-opus-4-6": (5, 25, 6.25, 0.5),
    "claude-sonnet-4-6": (3, 15, 3.75, 0.3),
    "claude-haiku-4-5": (1, 5, 1.25, 0.1),
    "gpt-5.4": (2.5, 15, 0, 0),
    "gpt-5-mini-2025-08-07": (0.25, 2, 0, 0),
}

PLAN_SAFE_TOOL_NAMES = {
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "SetStatus",
    "Agent",
    "AskUserQuestion",
    "Config",
    "ExitPlanMode",
    "TaskMonitorStart",
    "TaskMonitorStop",
    "Mcp",
    "ListMcpResources",
    "ReadMcpResource",
}


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

    tool_names = set(filterToolsByPolicy(config))
    tool_registry = [tool for tool in runtime.get("tools", []) if tool.name in tool_names]
    provider = provider_override or _create_provider(config, runtime)
    base_system_prompt = args.get("systemPromptOverride") or session["systemPrompt"]
    text_parts: list[str] = []
    total_usage: dict[str, Any] = {}
    usage_totals = _ensure_usage_totals(session)
    active_model = args.get("modelOverride") or session["model"]
    cost = 0.0
    tool_count = 0

    if prompt.strip():
        session.setdefault("messages", []).append({"role": "user", "content": prompt})
        await persistSession(session, config)
        _safe_emit(runtime, "message.user", {"text": prompt, "session_id": session["id"]})

    usage_totals["turns"] += 1

    while True:
        request_context = await buildContinuationContext({"session": session, "config": config, "prompt": prompt})
        system = _apply_plan_mode_overlay(base_system_prompt, session)
        if request_context.get("systemContext"):
            system = f"{system}\n\n{request_context['systemContext']}"
        request = {
            "model": active_model,
            "effort": normalizeEffortForModel(config["provider"], active_model, session.get("effort") or config.get("effort")),
            "system": system,
            "messages": request_context["messages"],
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.inputSchema} for tool in tool_registry],
        }
        measurement = await measureRequestInputTokens({"config": config, "request": request, "session": session})
        recordPreflightTokenMeasurement(session, measurement)
        _safe_emit(runtime, "provider.input_tokens", {"session_id": session["id"], "model": request["model"], "provider": config["provider"], "input_tokens": measurement["inputTokens"], "source": measurement["source"]})

        try:
            response = await maybe_await(provider["send"](request))
        except Exception as error:
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

        active_model = response["model"]
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

        def build_context() -> dict[str, Any]:
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
            if _requires_plan_approval(session) and tool_use["name"] not in PLAN_SAFE_TOOL_NAMES:
                message = f"{tool_use['name']} is not available while plan approval is still required."
                _safe_emit(runtime, "tool.completed", {"name": tool_use["name"], "output": message, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": message}
            tool = next((entry for entry in tool_registry if entry.name == tool_use["name"]), None)
            if not tool:
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": f"Tool not available: {tool_use['name']}"}
            tool_count += 1
            usage_totals["toolCalls"] += 1
            _safe_emit(runtime, "tool.started", {"name": tool.name, "input": tool_use.get("input") or {}, "session_id": session["id"]})
            try:
                result = await tool.handler(tool_use.get("input") or {}, build_context())
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": result.get("content", ""), "error": result.get("isError", False), "session_id": session["id"]})
                block = {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": result.get("isError"), "content": result.get("content", "")}
                if result.get("pause") == "approval":
                    block["_pauseMessage"] = (result.get("content") or "").strip() or "Approval required before continuing."
                return block
            except Exception as exc:
                message = str(exc)
                _safe_emit(runtime, "tool.completed", {"name": tool.name, "output": message, "error": True, "session_id": session["id"]})
                return {"type": "tool_result", "tool_use_id": tool_use["id"], "is_error": True, "content": message}

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


def _create_provider(config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    if config.get("provider") == "openai":
        if not config.get("openaiToken"):
            return {"send": lambda request: {"content": [{"type": "text", "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'."}], "usage": {"input_tokens": 0, "output_tokens": 0}, "model": request["model"], "stopReason": "end_turn"}}
        return {
            "send": lambda request: sendOpenAiRequest(
                token=config["openaiToken"],
                baseUrl=config.get("openaiBaseUrl"),
                headers=config.get("customHeaders"),
                mode=config.get("openaiApiMode"),
                request=request,
                runtime=runtime,
            )
        }
    if not config.get("apiKey") and not config.get("authToken"):
        return {"send": lambda request: {"content": [{"type": "text", "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'."}], "usage": {"input_tokens": 0, "output_tokens": 0}, "model": request["model"], "stopReason": "end_turn"}}
    return {"send": lambda request: sendAnthropicMessagesRequest(config=config, request=request, runtime=runtime)}


def _apply_plan_mode_overlay(base: str, session: dict[str, Any]) -> str:
    if session.get("permissionMode") != "plan":
        return base
    state = session.get("metadata", {}).get("planMode", {})
    return buildPlanModeSystemPrompt(base, {"currentState": state.get("state"), "priorPlan": state.get("plan")})


def _requires_plan_approval(session: dict[str, Any]) -> bool:
    return session.get("permissionMode") == "plan" and session.get("metadata", {}).get("planMode", {}).get("state") != "approved"


def _ensure_usage_totals(session: dict[str, Any]) -> dict[str, int]:
    metadata = session.setdefault("metadata", {})
    metadata.setdefault("usage", {"turns": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0})
    return metadata["usage"]


def _merge_usage_into_session(session: dict[str, Any], usage: dict[str, Any]) -> None:
    totals = _ensure_usage_totals(session)
    totals["inputTokens"] += int(usage.get("input_tokens") or 0)
    totals["outputTokens"] += int(usage.get("output_tokens") or 0)
    totals["cacheCreationInputTokens"] += int(usage.get("cache_creation_input_tokens") or 0)
    totals["cacheReadInputTokens"] += int(usage.get("cache_read_input_tokens") or 0)


def _estimate_usage_cost(usage: dict[str, Any], model: str) -> float:
    pricing = _resolve_pricing(model)
    if not pricing:
        return 0.0
    return (
        (usage.get("input_tokens") or 0) * pricing[0]
        + (usage.get("output_tokens") or 0) * pricing[1]
        + (usage.get("cache_creation_input_tokens") or 0) * pricing[2]
        + (usage.get("cache_read_input_tokens") or 0) * pricing[3]
    ) / 1_000_000


def _resolve_pricing(model: str):
    key = model.strip().lower()
    if key in MODEL_PRICING:
        return MODEL_PRICING[key]
    if key.startswith("gpt-5.4"):
        return MODEL_PRICING["gpt-5.4"]
    if key.startswith("gpt-5-mini"):
        return MODEL_PRICING["gpt-5-mini-2025-08-07"]
    if "sonnet-4" in key:
        return MODEL_PRICING["claude-sonnet-4-6"]
    if "opus-4" in key:
        return MODEL_PRICING["claude-opus-4-6"]
    if "haiku-4-5" in key:
        return MODEL_PRICING["claude-haiku-4-5"]
    return None


def _round_usd(value: float) -> float:
    return round(value, 6)


def _safe_emit(runtime: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    try:
        runtime.get("emitEvent", lambda *_: None)(event, data)
    except Exception:
        pass


def _now() -> str:
    from ._utils import now_iso

    return now_iso()
