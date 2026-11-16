from __future__ import annotations

import os
import uuid
from .._utils import maybe_await, now_iso, pretty_json
from ..config import (
    build_system_prompt_for_agent,
    normalize_model_name,
    resolve_model_for_agent,
    resolve_provider_for_model_selection,
)
from ..storage import (
    create_empty_session,
    save_child_agent,
    save_session,
    task_output_path,
)
from .contract import (
    runtime_contract_for_agent,
    summarize_subagent_runtime_contract,
)
from .depth_policy import require_subagent_depth_allowed
from .groups import (
    _upsert_subagent_launch_group,
    append_launch_group_metadata,
    resolve_subagent_launch_group_id,
)
from .permissions import inherit_parent_permission_mode, sanitize_permission_mode
from .spawn_utils import (
    _create_subagent_worktree,
    _dedupe,
    _output,
    _require_string,
    _throw_if_subagent_cancelled,
    _without_none_values,
)
from typing import Any
from ..storage.message_origin import message_with_origin as _message_with_origin
from ..authority.agent import (
    capture_agent_authority,
    derive_child_agent_authority,
    sandbox_config_for_agent_authority,
)
from ..contracts.records import launch_output_file, launch_task_id


async def start_background_subagent(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    context = args["context"]
    supervisor = context.get("daemon") or context.get("taskSupervisor")
    if not supervisor:
        # Checked before prepare_subagent so a refused launch persists nothing.
        raise RuntimeError(
            "Subagent launch requires the task supervisor daemon; unsupervised local spawning has been removed."
        )
    _throw_if_subagent_cancelled(context)
    prepared = await prepare_subagent(args)
    try:
        _throw_if_subagent_cancelled(context)
        child_session = prepared["childSession"]
        child_record = prepared["childRecord"]
        agent_id = prepared["agentId"]
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
    except BaseException:
        from ..runtime.shared_mcp import revoke_child_shared_mcp
        await revoke_child_shared_mcp(prepared["childConfig"])
        raise
    if isinstance(launched, dict) and launched.get("atCapacity"):
        from ..runtime.shared_mcp import revoke_child_shared_mcp
        await revoke_child_shared_mcp(prepared["childConfig"])
        # The supervisor refused the launch. Finalize the child record that
        # prepare_subagent already persisted - leaving it "queued" with no task
        # creates a phantom that no sweep, message, or stop can ever reach -
        # and surface the structured refusal so the model can decide.
        refused_at = now_iso()
        await save_child_agent(
            {
                **child_record,
                "status": "cancelled",
                "completionReason": "at_capacity",
                "completedAt": refused_at,
                "stoppable": False,
                "retryable": True,
                "waitingReason": None,
                "lastUpdatedAt": refused_at,
                "lastSummary": "Launch refused: subagent capacity is full.",
            }
        )
        await maybe_await(context.get("persist_session", lambda: None)())
        return {
            "content": pretty_json(
                _without_none_values(
                    {
                        "agent_id": agent_id,
                        "at_capacity": True,
                        "status": "refused",
                        "reason": launched.get("reason"),
                        "limit": launched.get("limit"),
                        "active": launched.get("active"),
                        "children": launched.get("children"),
                        "note": (
                            "Subagent capacity is full; nothing was launched. "
                            "SendMessage(to=<agent_id>) an existing child, or stop one and retry."
                        ),
                    }
                )
            )
        }
    task_id = launch_task_id(launched)
    output_file = launch_output_file(launched) or (task_output_path(task_id) if task_id else None)
    started_at = now_iso()
    append_launch_group_metadata(context["session"], child_record.get("launchGroupId") or prepared.get("launchGroupId"), agent_id, task_id)
    await _upsert_subagent_launch_group(
        context["session"]["id"],
        {
            "id": child_record.get("launchGroupId") or prepared.get("launchGroupId"),
            "label": child_record.get("teamName") or child_record.get("name") or child_record.get("agentType") or agent_id,
            "childAgentIds": [agent_id],
            "taskIds": [task_id] if task_id else [],
        },
    )
    await maybe_await(context.get("persist_session", lambda: None)())
    await save_child_agent(
        {
            **child_record,
            "taskId": task_id,
            "pid": launched.get("pid"),
            "processIdentity": launched.get("processIdentity"),
            "status": "backgrounded",
            "owner": "daemon",
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


async def prepare_subagent(args: dict[str, Any]) -> dict[str, Any]:
    context = args["context"]
    parent = context["session"]
    config = context["config"]
    require_subagent_depth_allowed(parent, config)
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
    requested_mode = sanitize_permission_mode(args.get("mode") or parent.get("permissionMode") or config.get("permissionMode"))
    child_mode = inherit_parent_permission_mode(requested_mode, parent.get("permissionMode") or config.get("permissionMode") or "default")
    parent_provider = parent.get("provider") if parent.get("provider") is not None else config.get("provider")
    parent_model = parent.get("model") if parent.get("model") is not None else config.get("model")
    child_model = normalize_model_name(model, parent_provider) if model else resolve_model_for_agent(config, agent_type, parent_model)
    child_provider = resolve_provider_for_model_selection(parent_provider, child_model) or parent_provider
    child_system = build_system_prompt_for_agent(config, agent_type) if agent_type else (parent.get("systemPrompt") if parent.get("systemPrompt") is not None else config.get("systemPrompt"))
    child_name = name or description.strip()
    runtime_contract = runtime_contract_for_agent(config, agent_type)
    runtime_contract_summary = summarize_subagent_runtime_contract(runtime_contract)
    get_tools = context.get("getTools")
    parent_tools = await maybe_await(get_tools()) if callable(get_tools) else context.get("tools")
    if parent_tools is None:
        parent_tools = [{"name": name} for name in config.get("tools") or []]
    parent_authority = await capture_agent_authority(
        config=config,
        session=parent,
        tools=parent_tools,
    )
    parent.setdefault("metadata", {})["effectiveAgentAuthority"] = parent_authority
    launch_group_id = resolve_subagent_launch_group_id(args, parent, context)
    worktree_path = await _create_subagent_worktree(context, agent_id, child_name) if isolation == "worktree" else None
    derived_authority = derive_child_agent_authority(
        parent=parent_authority,
        # Apply spawn inheritance before pinning; passing requested_mode here
        # silently discarded it (notably default under a bypass parent).
        # Derivation still intersects this mode with the parent's authority.
        requestedMode=child_mode,
        contract=runtime_contract,
        permissionRuleProjectRoot=os.path.realpath(parent["cwd"]),
        grantedWorkspaceRoots=[worktree_path] if worktree_path else None,
    )
    child_authority = derived_authority["authority"]
    child_mode = child_authority["mode"]
    child_cwd = worktree_path or parent["cwd"]
    parent_metadata = parent.setdefault("metadata", {})
    parent_has_original_cwd = parent_metadata.get("originalCwd") is not None
    child_session = create_empty_session(
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
    # The child is restored in another process: it needs the parent's registry.
    from ..session.persistence import stamp_provider_registry_metadata

    stamp_provider_registry_metadata(child_session, config)
    if runtime_contract:
        metadata["subagentRuntimeContract"] = runtime_contract
    metadata["childSessionIds"] = []
    metadata["childAgentIds"] = []
    child_session["messages"].append(
        _message_with_origin(
            {"role": "user", "content": prompt},
            actor="subagent",
            kind="subagent_task_prompt",
            intentAuthority="request",
            synthetic=True,
            lineage={
                "parentSessionId": parent["id"],
                "parentTurnId": metadata.get("parentTurnId"),
                "parentToolUseId": metadata.get("parentToolUseId"),
                "taskId": metadata.get("parentTaskId"),
            },
        )
    )

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
            "sandbox": sandbox_config_for_agent_authority(child_authority),
        }
    )
    # Broker admission is a new scoped connection, not inheritance of the
    # parent bearer. Pin its safe descriptor before either launch record saves.
    from ..runtime.shared_mcp import delegate_child_shared_mcp

    await delegate_child_shared_mcp(child_config, child_authority, child_session["id"])
    try:
        parent_metadata["childSessionIds"] = _dedupe([*(parent_metadata.get("childSessionIds") or []), child_session["id"]])
        parent_metadata["childAgentIds"] = _dedupe([*(parent_metadata.get("childAgentIds") or []), agent_id])
        parent_metadata["subagentLaunchGroups"] = _dedupe([*(parent_metadata.get("subagentLaunchGroups") or []), launch_group_id])
        append_launch_group_metadata(parent, launch_group_id, agent_id)
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
        await maybe_await(context.get("persist_session", lambda: None)())
        await save_session(child_session)
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
        await save_child_agent(child_record)
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
    except BaseException:
        from ..runtime.shared_mcp import revoke_child_shared_mcp
        await revoke_child_shared_mcp(child_config)
        raise
