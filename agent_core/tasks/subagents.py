from __future__ import annotations

import os
import subprocess
import sys
import uuid
from typing import Any

from .._utils import maybe_await, now_iso, pretty_json
from ..config import buildSystemPromptForAgent, normalizeModelName, resolveModelForAgent, resolveProviderForModelSelection
from ..storage import createEmptySession, loadChildAgent, saveChildAgent, saveSession, taskOutputPath, writeTaskMeta
from ..workspace import createGitWorktree


PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}


async def runForegroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    prepared = await prepareSubagent(args)
    context = args["context"]
    child_record = prepared["childRecord"]
    child_session = prepared["childSession"]
    agent_id = prepared["agentId"]
    started_at = _monotonic_ms()
    _output(context, "subagent.started", _subagent_started_payload(prepared, args, False))
    await saveChildAgent({**child_record, "status": "running", "lastUpdatedAt": now_iso(), "lastSummary": "Started foreground subagent run."})

    try:
        result = await _execute_foreground_subagent(args, prepared)
        status = mapStopReasonToStatus(result.get("stopReason"))
        summary = (result.get("text") or "").strip()[:280] or None
        completed_at = now_iso()
        await saveChildAgent(
            {
                **child_record,
                "status": status,
                "completedAt": completed_at,
                "stopReason": result.get("stopReason"),
                "lastUpdatedAt": completed_at,
                "lastSummary": summary,
                "resultSummary": summary,
                "latestResultAt": completed_at,
                "completionReason": mapStopReasonToCompletionReason(result.get("stopReason")),
                "waitingReason": mapStopReasonToWaitingReason(result.get("stopReason")),
            }
        )
        _output(
            context,
            "subagent.completed" if status == "completed" else "subagent.failed",
            {
                "agent_id": agent_id,
                "session_id": child_session["id"],
                "parent_session_id": context["session"]["id"],
                "attachable_session_id": child_session["id"],
                "status": status,
                "stop_reason": result.get("stopReason"),
                "summary": summary,
                "duration_ms": _monotonic_ms() - started_at,
            },
        )
        return {
            "content": pretty_json(
                {
                    "agent_id": agent_id,
                    "session_id": child_session["id"],
                    "agent_type": child_record.get("agentType"),
                    "status": status,
                    "content": result.get("text") or "",
                    "usage": result.get("usage") or {},
                    "tool_count": result.get("toolCount") or 0,
                    "duration_ms": _monotonic_ms() - started_at,
                    "stop_reason": result.get("stopReason"),
                    "worktree_path": prepared.get("worktreePath"),
                }
            ),
            "isError": status != "completed",
        }
    except Exception as exc:
        message = str(exc)
        completed_at = now_iso()
        await saveChildAgent(
            {
                **child_record,
                "status": "failed",
                "completedAt": completed_at,
                "error": message,
                "lastUpdatedAt": completed_at,
                "lastSummary": message,
                "resultSummary": message,
                "latestResultAt": completed_at,
                "completionReason": "worker_failed",
            }
        )
        _output(
            context,
            "subagent.failed",
            {
                "agent_id": agent_id,
                "session_id": child_session["id"],
                "parent_session_id": context["session"]["id"],
                "attachable_session_id": child_session["id"],
                "status": "failed",
                "error": message,
            },
        )
        return {"content": pretty_json({"agent_id": agent_id, "session_id": child_session["id"], "status": "failed", "error": message, "worktree_path": prepared.get("worktreePath")}), "isError": True}


async def startBackgroundSubagent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    prepared = await prepareSubagent(args)
    context = args["context"]
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
            }
        )
    else:
        launched = await _start_local_background_subagent(prepared)
    task_id = launched.get("taskId") or launched.get("task_id")
    output_file = launched.get("outputFile") or launched.get("output_file") or (taskOutputPath(task_id) if task_id else None)
    started_at = now_iso()
    await saveChildAgent(
        {
            **child_record,
            "taskId": task_id,
            "pid": launched.get("pid"),
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
            "worktree_path": prepared.get("worktreePath"),
        },
    )
    return {"content": pretty_json({"agent_id": agent_id, "session_id": child_session["id"], "task_id": task_id, "status": "running", "run_in_background": True, "output_file": output_file, "worktree_path": prepared.get("worktreePath")})}


async def prepareSubagent(args: dict[str, Any]) -> dict[str, Any]:
    context = args["context"]
    parent = context["session"]
    config = context["config"]
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
    child_model = normalizeModelName(model, parent.get("provider")) if model else resolveModelForAgent(config, agent_type, parent.get("model") or config.get("model"))
    child_provider = resolveProviderForModelSelection(parent.get("provider") or config.get("provider"), child_model) or parent.get("provider") or config.get("provider")
    child_system = buildSystemPromptForAgent(config, agent_type) if agent_type else parent.get("systemPrompt") or config.get("systemPrompt")
    child_name = name or description.strip()
    worktree_path = await _create_subagent_worktree(context, agent_id, child_name) if isolation == "worktree" else None
    child_cwd = worktree_path or parent["cwd"]
    child_session = createEmptySession(
        cwd=child_cwd,
        provider=child_provider,
        providerProfileName=parent.get("providerProfileName") or config.get("providerProfileName"),
        model=child_model,
        effort=parent.get("effort") if parent.get("effort") is not None else config.get("effort"),
        systemPrompt=child_system,
        permissionMode=child_mode,
        name=child_name,
        assistantName=parent.get("metadata", {}).get("assistantName") or config.get("assistantName"),
        assistantDescription=parent.get("metadata", {}).get("assistantDescription") or config.get("assistantDescription"),
        originalCwd=parent.get("metadata", {}).get("originalCwd") or parent["cwd"],
    )
    metadata = child_session.setdefault("metadata", {})
    metadata["depth"] = int(parent.get("metadata", {}).get("depth") or 0) + 1
    metadata["parentSessionId"] = parent["id"]
    metadata["parentAgentId"] = parent.get("metadata", {}).get("agentId")
    metadata["agentId"] = agent_id
    metadata["agentName"] = child_name
    metadata["agentType"] = agent_type
    metadata["teamName"] = team_name
    metadata["worktreePath"] = worktree_path
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
        "appendSystemPrompt": None,
        "allowedRoots": _dedupe([*(config.get("allowedRoots") or []), child_cwd]),
    }
    parent_metadata = parent.setdefault("metadata", {})
    parent_metadata["childSessionIds"] = _dedupe([*(parent_metadata.get("childSessionIds") or []), child_session["id"]])
    parent_metadata["childAgentIds"] = _dedupe([*(parent_metadata.get("childAgentIds") or []), agent_id])
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
    return {"agentId": agent_id, "childSession": child_session, "childConfig": child_config, "childRecord": child_record, "worktreePath": worktree_path}


async def _start_local_background_subagent(prepared: dict[str, Any]) -> dict[str, Any]:
    agent_id = prepared["agentId"]
    child_session = prepared["childSession"]
    task_id = str(uuid.uuid4())
    output_file = taskOutputPath(task_id)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    proc = _spawn_local_subagent_worker(task_id, child_session["id"], agent_id, child_session["cwd"], output_file)
    started_at = now_iso()
    await writeTaskMeta(
        task_id,
        {
            "type": "subagent",
            "task_id": task_id,
            "agent_id": agent_id,
            "session_id": child_session["id"],
            "cwd": child_session["cwd"],
            "pid": proc.pid,
            "startedAt": started_at,
            "status": "running",
            "outputFile": output_file,
            "owner": "client",
        },
    )
    return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file, "pid": proc.pid}


def _spawn_local_subagent_worker(task_id: str, session_id: str, agent_id: str, cwd: str, output_file: str) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    env = {
        **os.environ,
        "TESS_CHILD_AGENT_ID": agent_id,
        "TESS_CHILD_TASK_ID": task_id,
        "TESS_CHILD_SESSION_ID": session_id,
    }
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_core.background_subagent_runner",
                "--session-id",
                session_id,
                "--agent-id",
                agent_id,
                "--task-id",
                task_id,
            ],
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    finally:
        handle.close()


async def finalizeBackgroundSubagentSuccessFromEnv(session: dict[str, Any], result: dict[str, Any]) -> None:
    agent_id = os.environ.get("TESS_CHILD_AGENT_ID")
    task_id = os.environ.get("TESS_CHILD_TASK_ID")
    if not agent_id or not task_id:
        return
    status = mapStopReasonToStatus(result.get("stopReason"))
    completed_at = now_iso()
    existing = await loadChildAgent(agent_id) or {
        "agentId": agent_id,
        "sessionId": session["id"],
        "parentSessionId": session.get("metadata", {}).get("parentSessionId") or "",
        "model": session.get("model"),
        "provider": session.get("provider"),
        "mode": session.get("permissionMode"),
        "runInBackground": True,
        "status": "running",
        "startedAt": session.get("createdAt"),
    }
    summary = (result.get("text") or "").strip()[:280] or None
    retryable = status in {"failed", "stopped", "truncated", "cancelled"}
    completed_like = status in {"completed", "failed", "truncated", "cancelled", "stopped"}
    await saveChildAgent(
        {
            **existing,
            "agentId": agent_id,
            "sessionId": session["id"],
            "name": session.get("metadata", {}).get("agentName"),
            "agentType": session.get("metadata", {}).get("agentType"),
            "teamName": session.get("metadata", {}).get("teamName"),
            "model": session.get("model"),
            "provider": session.get("provider"),
            "mode": session.get("permissionMode"),
            "isolation": "worktree" if session.get("metadata", {}).get("worktreePath") else None,
            "worktreePath": session.get("metadata", {}).get("worktreePath"),
            "runInBackground": True,
            "status": status,
            "completedAt": completed_at if completed_like else None,
            "taskId": task_id,
            "stopReason": result.get("stopReason"),
            "pid": None,
            "error": result.get("text") if retryable else None,
            "attachableSessionId": session["id"],
            "owner": existing.get("owner") or "daemon",
            "waitingReason": mapStopReasonToWaitingReason(result.get("stopReason")),
            "completionReason": mapStopReasonToCompletionReason(result.get("stopReason")),
            "resultSummary": summary,
            "latestResultAt": completed_at if completed_like else None,
            "retryable": retryable,
            "stoppable": False,
            "recoveryState": None,
            "lastUpdatedAt": completed_at,
            "lastSummary": summary,
        }
    )
    await writeTaskMeta(task_id, {"type": "subagent", "task_id": task_id, "agent_id": agent_id, "session_id": session["id"], "cwd": session.get("cwd"), "startedAt": session.get("createdAt"), "status": status, "completedAt": completed_at if completed_like else None, "outputFile": taskOutputPath(task_id), "stopReason": result.get("stopReason"), "summary": summary, "resultSummary": summary, "waitingReason": mapStopReasonToWaitingReason(result.get("stopReason")), "completionReason": mapStopReasonToCompletionReason(result.get("stopReason")), "attachableSessionId": session["id"], "retryable": retryable, "stoppable": False, "owner": "daemon"})


async def finalizeBackgroundSubagentFailureFromEnv(error: Any) -> None:
    agent_id = os.environ.get("TESS_CHILD_AGENT_ID")
    task_id = os.environ.get("TESS_CHILD_TASK_ID")
    session_id = os.environ.get("TESS_CHILD_SESSION_ID")
    if not agent_id or not task_id or not session_id:
        return
    message = str(error)
    completed_at = now_iso()
    existing = await loadChildAgent(agent_id) or {
        "agentId": agent_id,
        "sessionId": session_id,
        "parentSessionId": "",
        "provider": "anthropic",
        "model": "",
        "mode": "default",
        "runInBackground": True,
        "status": "running",
        "startedAt": completed_at,
    }
    await saveChildAgent({**existing, "agentId": agent_id, "sessionId": session_id, "runInBackground": True, "status": "failed", "completedAt": completed_at, "taskId": task_id, "error": message, "attachableSessionId": session_id, "owner": existing.get("owner") or "daemon", "completionReason": "worker_failed", "resultSummary": message, "latestResultAt": completed_at, "retryable": True, "stoppable": False, "recoveryState": None, "lastUpdatedAt": completed_at, "lastSummary": message})
    await writeTaskMeta(task_id, {"type": "subagent", "task_id": task_id, "agent_id": agent_id, "session_id": session_id, "status": "failed", "completedAt": completed_at, "outputFile": taskOutputPath(task_id), "error": message, "summary": message, "resultSummary": message, "completionReason": "worker_failed", "attachableSessionId": session_id, "retryable": True, "stoppable": False, "owner": "daemon"})


def sanitizePermissionMode(mode: Any) -> str:
    return mode if mode in PERMISSION_MODES else "default"


def inheritParentPermissionMode(childMode: str, parentMode: str) -> str:
    if parentMode == "bypassPermissions":
        return "bypassPermissions"
    if parentMode == "acceptEdits" and childMode in {"default", "plan"}:
        return "acceptEdits"
    if parentMode == "plan":
        return "plan"
    return childMode


def mapStopReasonToStatus(stopReason: str | None) -> str:
    if stopReason == "plan_approval_required":
        return "awaiting_plan_approval"
    if stopReason == "plan_chat_requested":
        return "awaiting_input"
    if stopReason == "cancelled":
        return "cancelled"
    if stopReason == "max_tokens":
        return "truncated"
    if stopReason in {"max_budget_exceeded", "plan_rejected"}:
        return "failed"
    return "completed"


def mapStopReasonToWaitingReason(stopReason: str | None) -> str | None:
    if stopReason == "plan_approval_required":
        return "Awaiting plan approval before the worker can continue."
    if stopReason == "plan_chat_requested":
        return "Awaiting plan chat input before the worker can continue."
    return None


def mapStopReasonToCompletionReason(stopReason: str | None) -> str | None:
    return {
        "end_turn": "worker_completed",
        "cancelled": "cancelled",
        "max_tokens": "max_tokens",
        "max_budget_exceeded": "max_budget_exceeded",
        "plan_rejected": "plan_rejected",
    }.get(stopReason)


async def _execute_foreground_subagent(args: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    context = args["context"]
    callback = context.get("runSubagent")
    if callable(callback):
        result = await maybe_await(callback({**args, **prepared}, context))
        if isinstance(result, dict) and "content" in result and "text" not in result:
            return {"text": result.get("content") or "", "usage": result.get("usage") or {}, "toolCount": result.get("tool_count") or result.get("toolCount") or 0, "stopReason": result.get("stopReason") or result.get("stop_reason") or ("error" if result.get("isError") else "end_turn")}
        if isinstance(result, str):
            return {"text": result, "usage": {}, "toolCount": 0, "stopReason": "end_turn"}
        return result
    get_tools = context.get("getTools")
    tools = await maybe_await(get_tools()) if callable(get_tools) else context.get("tools")
    if tools is None:
        raise RuntimeError("Foreground subagent execution requires getTools, tools, or runSubagent in the runtime context.")
    from ..agent import runAgentPrompt

    runtime = {
        "interactive": context.get("interactive", False),
        "askQuestion": context.get("askQuestion", lambda *_: ""),
        "emitEvent": lambda event, data: _emit_child_event(context, event, data, prepared["agentId"], prepared["childSession"]["id"]),
        "tools": tools,
    }
    call_args: dict[str, Any] = {"session": prepared["childSession"], "config": prepared["childConfig"], "prompt": "", "runtime": runtime}
    if context.get("providerOverride"):
        call_args["providerOverride"] = context["providerOverride"]
    return await runAgentPrompt(call_args)


def _emit_child_event(context: dict[str, Any], event: str, data: dict[str, Any], agent_id: str, session_id: str) -> None:
    if event.startswith("permission."):
        _output(context, event, {**data, "agent_id": agent_id, "child_session_id": session_id})


def _subagent_started_payload(prepared: dict[str, Any], args: dict[str, Any], background: bool) -> dict[str, Any]:
    context = args["context"]
    child_record = prepared["childRecord"]
    child_session = prepared["childSession"]
    return {
        "agent_id": prepared["agentId"],
        "session_id": child_session["id"],
        "parent_session_id": context["session"]["id"],
        "attachable_session_id": child_session["id"],
        "agent_type": child_record.get("agentType"),
        "label": child_record.get("name") or child_record.get("agentType") or prepared["agentId"],
        "description": args.get("description"),
        "status": "running",
        "run_in_background": background,
        "worktree_path": prepared.get("worktreePath"),
    }


async def _create_subagent_worktree(context: dict[str, Any], agent_id: str, child_name: str) -> str:
    suffix = _slugify(child_name) or agent_id[-8:]
    controller = context.get("workspaceController")
    if controller and hasattr(controller, "createWorktree"):
        result = await maybe_await(controller.createWorktree(suffix))
        worktree = result.get("worktree") if isinstance(result, dict) else None
        if isinstance(worktree, dict) and worktree.get("targetPath"):
            return worktree["targetPath"]
    result = await createGitWorktree({"cwd": context["session"]["cwd"], "requestedName": suffix})
    return result["targetPath"]


def _output(context: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    context.get("outputJson", lambda *_: None)(event, data)


def _dedupe(values: list[Any]) -> list[str]:
    out = []
    for value in values:
        if isinstance(value, str) and value not in out:
            out.append(value)
    return out


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected {name} to be a non-empty string")
    return value


def _slugify(value: str) -> str:
    return __import__("re").sub(r"(^-+|-+$)", "", __import__("re").sub(r"[^a-z0-9]+", "-", value.lower()))[:24]


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)
