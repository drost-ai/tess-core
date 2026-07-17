from __future__ import annotations

import time as _time
import typing as _typing

if _typing.TYPE_CHECKING:
    from ..contracts.context import ToolContext

from .shared import *
from .plan_policy import bounded_mcp_identity as _bounded_mcp_identity
from .plan_policy import resolve_plan_mode_mcp_verdict as _resolve_plan_mode_mcp_verdict
from .providers import _emit_provider_response_completed, _record_provider_failure_if_needed
from .prompt_content import normalize_agent_prompt as _normalize_agent_prompt
from .tool_hooks import build_tool_hook_context as _build_tool_hook_context
from .tool_hooks import run_post_tool_hook as _run_post_tool_hook
from .tool_hooks import run_pre_tool_hook as _run_pre_tool_hook
from .tool_dispatch import create_serial_queue as _create_serial_queue
from .tool_dispatch import resolve_tool_concurrency as _resolve_tool_concurrency
from .tool_dispatch import run_tool_dispatch_batch as _run_tool_dispatch_batch
from ..agent_authority import enforceSessionAgentAuthority, filterToolsForAgentAuthority
from ..agent_runtime_bindings import assertAgentRuntimeBindings, materializeAgentExecutionRuntime
from ..agent_tool_results import createToolResultFinalizer as _create_tool_result_finalizer
from ..hooks import fireToolHooks
from ..image_budget import ImageBudgetError as _ImageBudgetError
from ..image_budget import createImageBudgetReservation as _create_image_budget_reservation
from ..permission_rules import findDispatchDenyRule
from ..provider_timeouts import createTurnDeadlineContext as _create_turn_deadline_context
from ..runtime_paths import getActiveRuntimePaths, resolveRuntimePaths, runWithRuntimePaths
from ..subagent_contract import filter_tools_for_subagent
from ..subagent_limits import build_subagent_tool_limit_result, check_subagent_runtime_limits, resolve_subagent_runtime_limits
from ..task_communication import deliver_queued_task_messages_to_session
from ..tool_attachments import (
    imageAttachmentReservationOrder as _image_attachment_reservation_order,
    resolveRuntimeProviderCapabilities as _resolve_runtime_provider_capabilities,
    stripInMemoryImageData as _strip_in_memory_image_data,
)

async def runAgentPrompt(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    session = values["session"]
    assertAgentRuntimeBindings(values.get("runtime"), session.get("id"))
    config = values["config"]
    paths = resolveRuntimePaths(candidate=config.get("runtimeHome")) if config.get("runtimeHome") else getActiveRuntimePaths()
    return await runWithRuntimePaths(paths, lambda: _runAgentPromptInScope(values))


async def _runAgentPromptInScope(args: dict[str, Any]) -> dict[str, Any]:
    session = args["session"]
    config = args["config"]
    prompt_content, prompt_text = _normalize_agent_prompt(args["prompt"])
    runtime_bindings = args.get("runtime")
    assertAgentRuntimeBindings(runtime_bindings, session.get("id"))
    runtime = materializeAgentExecutionRuntime(runtime_bindings)
    provider_override = args.get("providerOverride")
    authority = await enforceSessionAgentAuthority(session=session, config=config)
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
    apply_pending_mcp = runtime.get("applyPendingMcpCatalogChanges")
    if callable(apply_pending_mcp):
        await maybe_await(apply_pending_mcp())
    tool_names = set(filterToolsByPolicy(config))
    runtime_tools = filterToolsForAgentAuthority(
        filter_tools_for_subagent(runtime.get("tools", []), runtime_contract),
        authority,
    )
    tool_registry = [tool for tool in runtime_tools if tool.name in tool_names]
    base_system_prompt = args.get("systemPromptOverride") or session["systemPrompt"]
    text_parts: list[str] = []
    total_usage: dict[str, Any] = {}
    usage_totals = _ensure_usage_totals(session)
    active_model = args.get("modelOverride") or session["model"]
    cost = 0.0
    tool_count = 0
    provider_call_index = 0
    parent_turn_id = f"turn-{uuid.uuid4()}"
    turn_started_ms = int(_time.monotonic() * 1000)
    subagent_limits = resolve_subagent_runtime_limits(session, config)
    last_stable_snapshot = copy.deepcopy(session)
    last_stable_snapshot["messages"] = _strip_in_memory_image_data(last_stable_snapshot.get("messages", []))

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

    async def request_provider_response() -> tuple[dict[str, Any], dict[str, Any]]:
        deadlines = _create_turn_deadline_context(
            config=config,
            parentSignal=runtime.get("abortSignal"),
            perCallOptions=args.get("providerRequestOptions") or {},
            runtimeContract=runtime_contract,
        )
        deadline_runtime = {**runtime, "abortSignal": deadlines.signal}
        provider = provider_override or _create_provider(config, deadline_runtime, session)

        async def prepare(reactive_attempt: int) -> dict[str, Any]:
            return await deadlines.race(
                prepareProviderRequest(
                    {
                        "session": session,
                        "config": config,
                        "runtime": deadline_runtime,
                        "baseSystemPrompt": base_system_prompt,
                        "toolRegistry": tool_registry,
                        "activeModel": active_model,
                        "prompt": prompt_text,
                        "reactiveAttempt": reactive_attempt,
                        "providerRequestOptions": args.get("providerRequestOptions") or {},
                        "deadlines": deadlines,
                    }
                )
            )

        try:
            prepared_request = await prepare(0)
            while True:
                try:
                    _throw_if_turn_cancelled(runtime)
                    if requestHasImageInputs(prepared_request["request"]) and not modelSupportsImageInputs(config, active_model):
                        response = unsupportedImageInputResponse(active_model)
                    else:
                        response = await deadlines.race(maybe_await(provider["send"](prepared_request["request"])))
                    return prepared_request, response
                except Exception as error:
                    if _is_turn_abort_error(error, runtime.get("abortSignal")):
                        raise
                    if prepared_request.get("reactiveAttempt", 0) < 2 and isContextWindowFailure(error):
                        prepared_request = await prepare(
                            int(prepared_request.get("reactiveAttempt") or 0) + 1
                        )
                        continue
                    raise
        except Exception as error:
            await _record_provider_failure_if_needed(session, config, runtime, error)
            raise
        finally:
            deadlines.dispose()

    if _is_turn_cancelled(runtime):
        return await cancel_turn()

    appended_reconciliations = await appendPendingSubagentReconciliationsToSession(
        session,
        runtime=runtime,
        parentTurnId=parent_turn_id,
    )
    if appended_reconciliations.get("records"):
        await persistSession(session, config)
        await consumeAppendedSubagentReconciliations(session, appended_reconciliations, runtime=runtime)

    if prompt_content is not None:
        session.setdefault("messages", []).append({"role": "user", "content": prompt_content})
        await persistSession(session, config)
        _safe_emit(runtime, "message.user", {"text": prompt_text, "session_id": session["id"]})

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
            prepared_request, response = await request_provider_response()
        except _ImageBudgetError as error:
            _restore_session_state(session, last_stable_snapshot)
            await persistSession(session, config)
            _safe_emit(
                runtime,
                "provider.image_budget_rejected",
                {"session_id": session["id"], "code": error.code, "message": str(error), **error.measurement},
            )
            raise
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
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
        session["messages"] = _strip_in_memory_image_data(session["messages"])
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
        persist_queue = _create_serial_queue()
        tools_by_name = {tool.name: tool for tool in tool_registry}
        image_budget = _create_image_budget_reservation(
            _resolve_runtime_provider_capabilities(config), _image_attachment_reservation_order(tool_uses)
        )
        tool_priorities = {id(tool_use): index for index, tool_use in enumerate(tool_uses)}

        async def persist_tool_session() -> None:
            async def commit() -> None:
                await persistSession(session, config)
            await persist_queue.run(commit)

        def build_context(tool_use: dict[str, Any] | None = None) -> ToolContext:
            return {
                "cwd": session["cwd"],
                "allowedRoots": config.get("allowedRoots", []),
                "config": config,
                "session": session,
                "interactive": runtime.get("interactive", False),
                "outputJson": runtime.get("emitEvent", lambda *_: None),
                "askQuestion": runtime.get("askQuestion", lambda *_: ""),
                "persistSession": persist_tool_session,
                "updateCwd": lambda next_cwd: session.__setitem__("cwd", next_cwd),
                "getTools": lambda: runtime.get("tools", []),
                "daemon": runtime.get("daemon"),
                "abortSignal": runtime.get("abortSignal"),
                "reserveImageAttachment": lambda image: image_budget.reserve(
                    image, tool_priorities.get(id(tool_use))
                ),
                "currentToolUseId": tool_use.get("id") if isinstance(tool_use, dict) else None,
                "parentTurnId": parent_turn_id,
                "subagentLaunchGroupId": parent_turn_id,
            }

        finish_tool_result = _create_tool_result_finalizer(
            config=config, sessionId=session["id"],
            assistantMessageIndex=len(session.get("messages", [])) - 1,
            emit=lambda event, data: _safe_emit(runtime, event, data),
        )
        async def run_single(tool_use: dict[str, Any]) -> dict[str, Any]:
            nonlocal tool_count
            _throw_if_turn_cancelled(runtime)
            tool = tools_by_name.get(tool_use["name"])
            dispatch_deny = await findDispatchDenyRule(
                {
                    "cwd": session["cwd"],
                    "toolName": tool_use["name"],
                    "input": tool_use.get("input") or {},
                    "settingsSources": config.get("settingsSources"),
                    "inlineSettings": config.get("inlineSettings"),
                    "rules": (authority or {}).get("permissionRules") if authority else None,
                    "projectRoot": (authority or {}).get("permissionRuleProjectRoot") if authority else None,
                }
            )
            if dispatch_deny:
                message = f"Blocked by permission rule {dispatch_deny['raw']} ({dispatch_deny['scope']} settings)."
                return await finish_tool_result(
                    tool_use,
                    {"kind": "synthesized", "content": message, "isError": True},
                    rejected=True,
                )
            if _requires_plan_approval(session):
                is_mcp_candidate = bool(tool and getattr(tool, "mcp", None) is not None) or str(tool_use["name"]).startswith("mcp__")
                if is_mcp_candidate:
                    if tool is None:
                        verdict = {"decision": "block", "reason": "unresolved_target"}
                        server, mcp_tool_name = "unknown", "unknown"
                    else:
                        verdict = await _resolve_plan_mode_mcp_verdict(
                            tool=tool,
                            repository_identity=str(
                                runtime.get("repositoryIdentity")
                                or config.get("repositoryIdentity")
                                or session.get("cwd")
                                or ""
                            ),
                            resolve_operator_policy=runtime.get("resolveMcpOperatorPolicy")
                            or runtime.get("resolve_mcp_operator_policy"),
                        )
                        server, mcp_tool_name = _bounded_mcp_identity(tool)
                    event_name = "plan.mcp_tool_allowed" if verdict["decision"] == "allow" else "plan.mcp_tool_blocked"
                    _safe_emit(
                        runtime,
                        event_name,
                        {"server": server, "tool": mcp_tool_name, "reason": verdict["reason"]},
                    )
                    if verdict["decision"] != "allow":
                        message = (
                            f"MCP tool {server}/{mcp_tool_name} is blocked while plan approval is required "
                            f"({verdict['reason']})."
                        )
                        return await finish_tool_result(
                            tool_use,
                            {"kind": "synthesized", "content": message, "isError": True},
                            blocked_by_plan_contract=True,
                            rejected=True,
                        )
                elif tool_use["name"] not in PLAN_SAFE_TOOL_NAMES:
                    message = f"{tool_use['name']} is not available while plan approval is still required."
                    return await finish_tool_result(
                        tool_use,
                        {"kind": "synthesized", "content": message, "isError": True},
                        blocked_by_plan_contract=True,
                        rejected=True,
                    )
            invalid_tool_input = _invalid_tool_input_message(tool_use)
            if invalid_tool_input:
                tool_count += 1
                usage_totals["toolCalls"] += 1
                return await finish_tool_result(
                    tool_use,
                    {"kind": "synthesized", "content": invalid_tool_input, "isError": True},
                )
            if not tool:
                return await finish_tool_result(
                    tool_use,
                    {"kind": "synthesized", "content": f"Tool not available: {tool_use['name']}", "isError": True},
                )
            exceeded = check_subagent_runtime_limits(subagent_limits, subagent_limit_state())
            if exceeded and exceeded.get("reason") == "max_tool_calls":
                result = build_subagent_tool_limit_result(tool_use, exceeded)
                _safe_emit(runtime, "subagent.limit_exceeded", {"session_id": session["id"], "reason": exceeded.get("reason"), "limit": exceeded.get("limit")})
                return await finish_tool_result(
                    tool_use,
                    {"kind": "synthesized", "content": result["content"], "isError": True},
                    tool_name=tool.name,
                )
            tool_count += 1
            usage_totals["toolCalls"] += 1
            hook_fire = _build_tool_hook_context(session, config, authority)
            pre_hook = await _run_pre_tool_hook(tool, tool_use, hook_fire, runtime.get("emitEvent"))
            if pre_hook["decision"] == "deny":
                message = f"{tool.name} call blocked by PreToolUse hook"
                if pre_hook.get("reason"):
                    message = f"{message}: {pre_hook['reason']}"
                else:
                    message = f"{message}."
                return await finish_tool_result(
                    tool_use,
                    {"kind": "synthesized", "content": message, "isError": True},
                    tool_name=tool.name,
                    rejected=True,
                )

            _safe_emit(runtime, "tool.started", {"name": tool.name, "tool_use_id": tool_use["id"], "input": tool_use.get("input") or {}, "session_id": session["id"]})
            side_effects_may_have_occurred = _resolve_tool_concurrency(tool) == "exclusive"
            try:
                result = await tool.handler(tool_use.get("input") or {}, build_context(tool_use))
            except Exception as exc:
                if _is_turn_abort_error(exc, runtime.get("abortSignal")):
                    raise
                completed = await finish_tool_result(
                    tool_use,
                    {"kind": "thrown", "error": exc},
                    tool_name=tool.name,
                    side_effects_may_have_occurred=side_effects_may_have_occurred,
                )
                await _run_post_tool_hook(tool, tool_use, hook_fire, runtime.get("emitEvent"))
                return completed
            completed = await finish_tool_result(
                tool_use,
                {"kind": "returned", "result": result},
                tool_name=tool.name,
                side_effects_may_have_occurred=side_effects_may_have_occurred,
            )
            _throw_if_turn_cancelled(runtime)
            await _run_post_tool_hook(tool, tool_use, hook_fire, runtime.get("emitEvent"))
            return completed

        async def dispatch_single(tool_use: dict[str, Any]) -> dict[str, Any]:
            try:
                result = await run_single(tool_use)
            finally:
                await image_budget.complete(tool_priorities.get(id(tool_use), -1))
            pause = result.pop("_pauseMessage", None)
            blocked = bool(result.pop("_blockedByPlanContract", False))
            additional = result.pop("_additionalBlocks", [])
            return {
                "result": result,
                **({"additionalResults": additional} if additional else {}),
                **({"pauseMessage": pause} if pause else {}),
                **({"blockedByPlanContract": True} if blocked else {}),
            }

        try:
            batch = await _run_tool_dispatch_batch(
                tool_uses=tool_uses,
                sequential=_requires_plan_approval(session),
                concurrency_of=lambda tool_use: _resolve_tool_concurrency(tools_by_name.get(tool_use["name"])),
                run=dispatch_single,
            )
            tool_results = batch["blocks"]
            paused_for_approval = batch["pausedForApproval"]
            paused_message = batch["pausedMessage"]
            _throw_if_turn_cancelled(runtime)
        except Exception as error:
            if _is_turn_abort_error(error, runtime.get("abortSignal")):
                return await cancel_turn()
            raise

        session["messages"].append({"role": "user", "content": [result for result in tool_results if result is not None]})
        await persistSession(session, config)
        last_stable_snapshot = copy.deepcopy(session)
        last_stable_snapshot["messages"] = _strip_in_memory_image_data(last_stable_snapshot.get("messages", []))
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
