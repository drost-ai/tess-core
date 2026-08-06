from __future__ import annotations

from .shared import *
from .processes import _worker_env
from .records import _build_task_record
from .utils import _dedupe
from ...subagents.capacity import check_subagent_capacity, resolve_subagent_concurrency_policy
from ..communication import append_task_event
from ...process.task_launch import createTaskLaunchReservation


def _create_subagent_task_starter(
    cwd: str,
    supervisor_id: str,
    launch_process_task: Callable[..., Any],
) -> Callable[..., Any]:
    async def start_subagent_task(
        task_args: dict[str, Any] | None = None,
        **task_kwargs: Any,
    ) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        parent_session_id = _require_string(
            task_args.get("parentSessionId") or task_args.get("parent_session_id"),
            "parentSessionId",
        )
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        agent_id = _require_string(task_args.get("agentId") or task_args.get("agent_id"), "agentId")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("label") or task_args.get("title") or agent_id
        child_session = await loadSession(session_id)
        if not child_session:
            raise RuntimeError(f"Subagent session not found: {session_id}")
        child_metadata = child_session.setdefault("metadata", {})
        runtime_contract = (
            task_args.get("runtimeContract")
            or task_args.get("runtime_contract")
            or child_metadata.get("subagentRuntimeContract")
        )
        capacity = check_subagent_capacity(
            resolve_subagent_concurrency_policy(),
            await listTaskRecords(),
            runtime_contract if isinstance(runtime_contract, dict) else None,
        )
        if capacity:
            raise RuntimeError(
                f"Subagent capacity exceeded: {capacity.get('reason')} "
                f"({capacity.get('active')}/{capacity.get('limit')})"
            )
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        reservation = createTaskLaunchReservation(
            taskId=task_id,
            sessionId=parent_session_id,
            kind="subagent",
        )
        command = (
            f"{sys.executable} -m agent_core.process.background_subagent_runner "
            f"--session-id {session_id} --agent-id {agent_id} --task-id {task_id}"
        )
        child_argv = [
            sys.executable,
            "-m",
            "agent_core.process.background_subagent_runner",
            "--session-id",
            session_id,
            "--agent-id",
            agent_id,
            "--task-id",
            task_id,
        ]
        runtime_contract_value = runtime_contract if isinstance(runtime_contract, dict) else None
        parent_tool_use_id = task_args.get("parentToolUseId") or task_args.get("parent_tool_use_id")
        parent_turn_id = task_args.get("parentTurnId") or task_args.get("parent_turn_id")
        launch_group_id = task_args.get("launchGroupId") or task_args.get("launch_group_id")
        meta = {
            "type": "subagent",
            "taskId": task_id,
            "id": task_id,
            "agent_id": agent_id,
            "sessionId": session_id,
            "session_id": session_id,
            "parent_session_id": parent_session_id,
            "parent_task_id": task_args.get("parentTaskId") or task_args.get("parent_task_id"),
            "parent_tool_use_id": parent_tool_use_id,
            "parent_turn_id": parent_turn_id,
            "launch_group_id": launch_group_id,
            "command": command,
            "cwd": task_cwd,
            "startedAt": reservation["createdAt"],
            "updatedAt": reservation["createdAt"],
            "status": "starting",
            "outputFile": output_file,
            "title": title,
            "description": title,
            "attachableSessionId": session_id,
            "retryable": False,
            "stoppable": True,
            "detailStatus": "starting",
            "attachedDetailStatus": "worker_running",
            "attachedSummary": "Background subagent is running under daemon supervision.",
            "owner": "daemon",
            "supervisorId": supervisor_id,
            "runtimeContract": runtime_contract_value,
            "runtimeHome": task_args.get("runtimeHome"),
            "concurrencyGroup": runtime_contract.get("concurrencyGroup") if isinstance(runtime_contract, dict) else None,
            "timeoutMs": runtime_contract.get("timeoutMs") if isinstance(runtime_contract, dict) else None,
            "launchReservation": reservation,
            "metadata": {
                "runtime_contract": runtime_contract_value,
                "concurrency_group": runtime_contract.get("concurrencyGroup") if isinstance(runtime_contract, dict) else None,
                "launch_group_id": launch_group_id,
                "parent_tool_use_id": parent_tool_use_id,
                "parent_turn_id": parent_turn_id,
            },
        }
        record = {
            **_build_task_record(meta, "Background subagent is starting under daemon supervision."),
            "status": "queued",
            "detailStatus": "starting",
            "launchReservation": reservation,
        }

        def append_parent_task(record_session: dict[str, Any]) -> dict[str, Any]:
            metadata = record_session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            return record_session

        def bind_child_task(record_session: dict[str, Any]) -> dict[str, Any]:
            metadata = record_session.setdefault("metadata", {})
            metadata["subagentTaskId"] = task_id
            if isinstance(runtime_contract, dict) and runtime_contract:
                metadata["subagentRuntimeContract"] = runtime_contract
            return record_session

        async def attach_child(identity: dict[str, Any]) -> None:
            child = await loadChildAgent(agent_id)
            if child:
                await saveChildAgent({**child, "pid": identity["pid"], "processIdentity": identity})

        env = _worker_env(
            {
                "TESS_CHILD_AGENT_ID": agent_id,
                "TESS_CHILD_TASK_ID": task_id,
                "TESS_CHILD_SESSION_ID": session_id,
                **({"AGENT_CORE_HOME": task_args.get("runtimeHome")} if task_args.get("runtimeHome") else {}),
            }
        )
        result = await launch_process_task(
            task_id=task_id,
            session_id=parent_session_id,
            command=command,
            task_cwd=task_cwd,
            output_file=output_file,
            meta=meta,
            record=record,
            child_argv=child_argv,
            env=env,
            reserve_session_updates=[
                (parent_session_id, append_parent_task),
                (session_id, bind_child_task),
            ],
            attach_extra=attach_child,
        )
        current = await readTaskMeta(task_id) or {}
        await append_task_event(
            task_id,
            {
                "type": "task.subagent.started",
                "agentId": agent_id,
                "childSessionId": session_id,
                "parentSessionId": parent_session_id,
                "launchGroupId": launch_group_id,
                "pid": (current.get("processIdentity") or {}).get("pid"),
            },
        )
        return result

    return start_subagent_task
