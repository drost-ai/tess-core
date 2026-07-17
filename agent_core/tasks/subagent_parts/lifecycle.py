from __future__ import annotations

from .shared import *
from ...agent_authority import (
    captureAgentAuthority,
    deriveChildAgentAuthority,
    sandboxConfigForAgentAuthority,
)
from ...contracts.records import launch_output_file, launch_task_id


async def startBackgroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    context = args["context"]
    _throw_if_subagent_cancelled(context)
    prepared = await prepareSubagent(args)
    _throw_if_subagent_cancelled(context)
    child_session = prepared["childSession"]
    child_record = prepared["childRecord"]
    agent_id = prepared["agentId"]
    supervisor = context.get("daemon") or context.get("taskSupervisor")
    if supervisor:
        launched = await supervisor["startSubagentTask"](
            {
                "parentSessionId": context["session"]["id"],
                "sessionId": child_session["id"],
                "agentId": agent_id,
                "cwd": child_session["cwd"],
                "label": child_record.get("name") or child_record.get("agentType") or agent_id,
                "parentTaskId": child_record.get("parentTaskId"),
                "parentToolUseId": child_record.get("parentToolUseId"),
                "parentTurnId": child_record.get("parentTurnId"),
                "launchGroupId": child_record.get("launchGroupId"),
                "runtimeContract": prepared.get("runtimeContract"),
            }
        )
    else:
        launched = await _start_local_background_subagent(prepared)
    task_id = launch_task_id(launched)
    output_file = launch_output_file(launched) or (taskOutputPath(task_id) if task_id else None)
    started_at = now_iso()
    appendLaunchGroupMetadata(context["session"], child_record.get("launchGroupId") or prepared.get("launchGroupId"), agent_id, task_id)
    await _upsert_subagent_launch_group(
        context["session"]["id"],
        {
            "id": child_record.get("launchGroupId") or prepared.get("launchGroupId"),
            "label": child_record.get("teamName") or child_record.get("name") or child_record.get("agentType") or agent_id,
            "childAgentIds": [agent_id],
            "taskIds": [task_id] if task_id else [],
        },
    )
    await maybe_await(context.get("persistSession", lambda: None)())
    await saveChildAgent(
        {
            **child_record,
            "taskId": task_id,
            "pid": launched.get("pid"),
            "processIdentity": launched.get("processIdentity"),
            "status": "backgrounded",
            "owner": "daemon" if context.get("daemon") else "client",
            "attachableSessionId": child_session["id"],
            "stoppable": True,
            "retryable": False,
            "waitingReason": None,
            "completionReason": None,
            "resultSummary": None,
            "latestResultAt": None,
            "recoveryState": None,
            "lastUpdatedAt": started_at,
            "lastSummary": "Running in the background.",
        }
    )
    _output(
        context,
        "subagent.backgrounded",
        {
            "agent_id": agent_id,
            "session_id": child_session["id"],
            "parent_session_id": context["session"]["id"],
            "attachable_session_id": child_session["id"],
            "task_id": task_id,
            "pid": launched.get("pid"),
            "output_file": output_file,
            "status": "backgrounded",
            "label": child_record.get("name") or child_record.get("agentType") or agent_id,
            "launch_group_id": child_record.get("launchGroupId"),
            "worktree_path": prepared.get("worktreePath"),
            "runtime_contract": prepared.get("runtimeContractSummary"),
        },
    )
    return {"content": pretty_json(_without_none_values({"agent_id": agent_id, "session_id": child_session["id"], "task_id": task_id, "status": "running", "run_in_background": True, "output_file": output_file, "runtime_contract": prepared.get("runtimeContractSummary"), "worktree_path": prepared.get("worktreePath")}))}


async def prepareSubagent(args: dict[str, Any]) -> dict[str, Any]:
    context = args["context"]
    parent = context["session"]
    config = context["config"]
    requireSubagentDepthAllowed(parent, config)
    description = _require_string(args.get("description"), "description")
    prompt = _require_string(args.get("prompt"), "prompt")
    agent_type = (args.get("agentType") or args.get("subagent_type") or "").strip() or None
    model = args.get("model") if isinstance(args.get("model"), str) and args.get("model").strip() else None
    name = args.get("name") if isinstance(args.get("name"), str) and args.get("name").strip() else None
    team_name = args.get("teamName") or args.get("team_name")
    team_name = team_name.strip() if isinstance(team_name, str) and team_name.strip() else parent.get("metadata", {}).get("teamName")
    run_in_background = bool(args.get("runInBackground") if "runInBackground" in args else args.get("run_in_background"))
    isolation = "worktree" if args.get("isolation") == "worktree" else None
    agent_id = f"agent-{uuid.uuid4()}"
    requested_mode = sanitizePermissionMode(args.get("mode") or parent.get("permissionMode") or config.get("permissionMode"))
    child_mode = inheritParentPermissionMode(requested_mode, parent.get("permissionMode") or config.get("permissionMode") or "default")
    parent_provider = parent.get("provider") if parent.get("provider") is not None else config.get("provider")
    parent_model = parent.get("model") if parent.get("model") is not None else config.get("model")
    child_model = normalizeModelName(model, parent_provider) if model else resolveModelForAgent(config, agent_type, parent_model)
    child_provider = resolveProviderForModelSelection(parent_provider, child_model) or parent_provider
    child_system = buildSystemPromptForAgent(config, agent_type) if agent_type else (parent.get("systemPrompt") if parent.get("systemPrompt") is not None else config.get("systemPrompt"))
    child_name = name or description.strip()
    runtime_contract = runtime_contract_for_agent(config, agent_type)
    runtime_contract_summary = summarize_subagent_runtime_contract(runtime_contract)
    get_tools = context.get("getTools")
    parent_tools = await maybe_await(get_tools()) if callable(get_tools) else context.get("tools")
    if parent_tools is None:
        parent_tools = [{"name": name} for name in config.get("tools") or []]
    parent_authority = await captureAgentAuthority(
        config=config,
        session=parent,
        tools=parent_tools,
    )
    parent.setdefault("metadata", {})["effectiveAgentAuthority"] = parent_authority
    derived_authority = deriveChildAgentAuthority(
        parent=parent_authority,
        requestedMode=requested_mode,
        contract=runtime_contract,
        permissionRuleProjectRoot=os.path.realpath(parent["cwd"]),
    )
    child_authority = derived_authority["authority"]
    child_mode = child_authority["mode"]
    launch_group_id = resolveSubagentLaunchGroupId(args, parent, context)
    worktree_path = await _create_subagent_worktree(context, agent_id, child_name) if isolation == "worktree" else None
    child_cwd = worktree_path or parent["cwd"]
    parent_metadata = parent.setdefault("metadata", {})
    parent_has_original_cwd = parent_metadata.get("originalCwd") is not None
    child_session = createEmptySession(
        cwd=child_cwd,
        provider=child_provider,
        providerProfileName=parent.get("providerProfileName") if parent.get("providerProfileName") is not None else config.get("providerProfileName"),
        model=child_model,
        effort=parent.get("effort") if parent.get("effort") is not None else config.get("effort"),
        systemPrompt=child_system,
        permissionMode=child_mode,
        name=child_name,
        assistantName=parent.get("metadata", {}).get("assistantName") or config.get("assistantName"),
        assistantDescription=parent.get("metadata", {}).get("assistantDescription") or config.get("assistantDescription"),
        originalCwd=parent_metadata.get("originalCwd"),
    )
    metadata = child_session.setdefault("metadata", {})
    if parent_has_original_cwd:
        metadata["originalCwd"] = parent_metadata.get("originalCwd")
    else:
        metadata.pop("originalCwd", None)
    metadata.pop("assistantName", None)
    metadata.pop("assistantDescription", None)
    metadata["depth"] = int(parent.get("metadata", {}).get("depth") or 0) + 1
    metadata["parentSessionId"] = parent["id"]
    metadata["parentAgentId"] = parent.get("metadata", {}).get("agentId")
    metadata["parentTaskId"] = args.get("parentTaskId")
    metadata["parentTurnId"] = args.get("parentTurnId") or context.get("parentTurnId")
    metadata["parentToolUseId"] = args.get("parentToolUseId") or context.get("currentToolUseId")
    metadata["agentId"] = agent_id
    metadata["agentName"] = child_name
    metadata["agentType"] = agent_type
    metadata["teamName"] = team_name
    metadata["launchGroupId"] = launch_group_id
    metadata["worktreePath"] = worktree_path
    metadata["effectiveAgentAuthority"] = child_authority
    if isinstance(config.get("parentContinuation"), dict):
        metadata["parentContinuation"] = dict(config["parentContinuation"])
    if runtime_contract:
        metadata["subagentRuntimeContract"] = runtime_contract
    metadata["childSessionIds"] = []
    metadata["childAgentIds"] = []
    child_session["messages"].append({"role": "user", "content": prompt})

    child_config = {
        **config,
        "agent": agent_type,
        "provider": child_provider,
        "model": child_model,
        "effort": child_session.get("effort"),
        "permissionMode": child_mode,
        "name": child_name,
        "systemPrompt": child_system,
        "subagentRuntimeContract": runtime_contract,
        "appendSystemPrompt": None,
        "allowedRoots": _dedupe([*(config.get("allowedRoots") or []), child_cwd]),
    }
    child_config.update(
        {
            "allowedRoots": list(child_authority["readableRoots"]),
            "tools": list(child_authority["enabledTools"]),
            "allowedTools": list(child_authority["enabledTools"]),
            "disallowedTools": [],
            "sandbox": sandboxConfigForAgentAuthority(child_authority),
        }
    )
    parent_metadata["childSessionIds"] = _dedupe([*(parent_metadata.get("childSessionIds") or []), child_session["id"]])
    parent_metadata["childAgentIds"] = _dedupe([*(parent_metadata.get("childAgentIds") or []), agent_id])
    parent_metadata["subagentLaunchGroups"] = _dedupe([*(parent_metadata.get("subagentLaunchGroups") or []), launch_group_id])
    appendLaunchGroupMetadata(parent, launch_group_id, agent_id)
    await _upsert_subagent_launch_group(
        parent["id"],
        {
            "id": launch_group_id,
            "label": team_name or child_name,
            "childAgentIds": [agent_id],
        },
    )
    if run_in_background:
        parent_metadata["backgroundSessionIds"] = _dedupe([*(parent_metadata.get("backgroundSessionIds") or []), child_session["id"]])
    await maybe_await(context.get("persistSession", lambda: None)())
    await saveSession(child_session)
    now = now_iso()
    child_record = {
        "agentId": agent_id,
        "sessionId": child_session["id"],
        "parentSessionId": parent["id"],
        "parentToolUseId": args.get("parentToolUseId"),
        "parentTurnId": args.get("parentTurnId") or context.get("parentTurnId"),
        "launchGroupId": launch_group_id,
        "parentTaskId": args.get("parentTaskId"),
        "name": child_name,
        "agentType": agent_type,
        "teamName": team_name,
        "provider": child_provider,
        "model": child_model,
        "mode": child_mode,
        "description": description,
        "isolation": isolation,
        "worktreePath": worktree_path,
        "runInBackground": run_in_background,
        "runtimeContract": runtime_contract,
        "effectiveAgentAuthority": child_authority,
        "parentContinuation": metadata.get("parentContinuation"),
        "status": "queued" if run_in_background else "running",
        "startedAt": now,
        "owner": "daemon" if run_in_background and context.get("daemon") else "client",
        "attachableSessionId": child_session["id"],
        "waitingReason": None,
        "completionReason": None,
        "resultSummary": None,
        "latestResultAt": None,
        "retryable": False,
        "stoppable": run_in_background,
        "recoveryState": None,
        "lastUpdatedAt": now,
        "lastSummary": "Queued for background execution." if run_in_background else "Starting foreground execution.",
    }
    await saveChildAgent(child_record)
    return {
        "agentId": agent_id,
        "childSession": child_session,
        "childConfig": child_config,
        "childRecord": child_record,
        "runtimeContract": runtime_contract,
        "runtimeContractSummary": runtime_contract_summary,
        "worktreePath": worktree_path,
        "launchGroupId": launch_group_id,
    }
