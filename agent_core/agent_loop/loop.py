from __future__ import annotations

from .shared import *

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
    last_stable_snapshot = copy.deepcopy(session)

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
            _throw_if_turn_cancelled(runtime)
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
