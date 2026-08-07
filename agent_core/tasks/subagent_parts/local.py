from __future__ import annotations

import os
import subprocess
import sys
import uuid
from ...storage import getStateRoot, mutateChildAgent, taskOutputPath, writeTaskMeta
from typing import Any
from ...process.owned_launch import activateOwnedProcessRunner, createOwnedProcessActivation
from ...storage import mutateSessionRecord, saveTaskRecord
from ...process.task_launch import (
    attachTaskLaunchReservation,
    createTaskLaunchReservation,
    failTaskLaunchReservation,
    launchReservedProcess,
)


async def _start_local_background_subagent(prepared: dict[str, Any]) -> dict[str, Any]:
    agent_id = prepared["agentId"]
    child_session = prepared["childSession"]
    task_id = str(uuid.uuid4())
    output_file = taskOutputPath(task_id)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    child_metadata = child_session.setdefault("metadata", {})
    parent_session_id = child_metadata.get("parentSessionId")
    if not isinstance(parent_session_id, str) or not parent_session_id:
        raise RuntimeError("Background subagent is missing its parent session identity.")
    reservation = createTaskLaunchReservation(
        taskId=task_id,
        sessionId=parent_session_id,
        kind="subagent",
    )
    starting_meta = {
        "type": "subagent",
        "taskId": task_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "session_id": child_session["id"],
        "parent_session_id": parent_session_id,
        "parent_task_id": child_metadata.get("parentTaskId"),
        "parent_tool_use_id": child_metadata.get("parentToolUseId"),
        "parent_turn_id": child_metadata.get("parentTurnId"),
        "launch_group_id": child_metadata.get("launchGroupId"),
        "cwd": child_session["cwd"],
        "startedAt": reservation["createdAt"],
        "updatedAt": reservation["createdAt"],
        "status": "starting",
        "detailStatus": "starting",
        "outputFile": output_file,
        "owner": "client",
        "runtimeContract": prepared.get("runtimeContract"),
        "concurrencyGroup": (prepared.get("runtimeContract") or {}).get("concurrencyGroup"),
        "launchReservation": reservation,
    }
    record = {
        "id": task_id,
        "sessionId": parent_session_id,
        "sourceId": agent_id,
        "kind": "subagent",
        "status": "queued",
        "title": prepared["childRecord"].get("name") or prepared["childRecord"].get("agentType") or agent_id,
        "summary": "Background subagent is starting.",
        "createdAt": reservation["createdAt"],
        "updatedAt": reservation["createdAt"],
        "startedAt": reservation["createdAt"],
        "completedAt": None,
        "foregroundable": True,
        "restorable": True,
        "cwd": child_session["cwd"],
        "outputFile": output_file,
        "owner": "client",
        "foregroundMode": "background",
        "taskFamily": "subagent",
        "detailStatus": "starting",
        "attachableSessionId": child_session["id"],
        "retrieveOnly": False,
        "relatedChildSessionIds": [child_session["id"]],
        "resultSummary": None,
        "metadata": {
            "agent_id": agent_id,
            "child_session_id": child_session["id"],
            "parent_session_id": parent_session_id,
            "runtime_contract": prepared.get("runtimeContract"),
            "launch_group_id": child_metadata.get("launchGroupId"),
        },
        "launchReservation": reservation,
    }
    child_argv = _local_subagent_worker_argv(task_id, child_session["id"], agent_id)
    activation = createOwnedProcessActivation(child_argv)
    process_holder: dict[str, subprocess.Popen[Any]] = {}

    async def reserve() -> None:
        await writeTaskMeta(task_id, starting_meta)
        await saveTaskRecord(record)

        def bind_child(session: dict[str, Any]) -> dict[str, Any]:
            metadata = session.setdefault("metadata", {})
            metadata["subagentTaskId"] = task_id
            if prepared.get("runtimeContract"):
                metadata["subagentRuntimeContract"] = prepared["runtimeContract"]
            return session

        def bind_parent(session: dict[str, Any]) -> dict[str, Any]:
            metadata = session.setdefault("metadata", {})
            metadata["taskIds"] = list(dict.fromkeys([*(metadata.get("taskIds") or []), task_id]))
            return session

        if await mutateSessionRecord(child_session["id"], bind_child) is None:
            raise RuntimeError(f"Subagent session not found during launch reservation: {child_session['id']}")
        if await mutateSessionRecord(parent_session_id, bind_parent) is None:
            raise RuntimeError(f"Parent session not found during launch reservation: {parent_session_id}")

    def spawn() -> subprocess.Popen[Any]:
        process = _spawn_local_subagent_worker(
            task_id,
            child_session["id"],
            agent_id,
            child_session["cwd"],
            output_file,
            argv=activation["argv"],
            activation_stdin=True,
        )
        process_holder["process"] = process
        return process

    async def attach(identity: dict[str, Any]) -> None:
        attached = attachTaskLaunchReservation(reservation)
        await writeTaskMeta(
            task_id,
            {
                **starting_meta,
                "pid": identity["pid"],
                "processIdentity": identity,
                "launchReservation": attached,
                "status": "running",
                "detailStatus": "worker_running",
                "updatedAt": attached["attachedAt"],
            },
        )
        await saveTaskRecord(
            {
                **record,
                "status": "backgrounded",
                "summary": "Background subagent is running.",
                "detailStatus": "worker_running",
                "processIdentity": identity,
                "launchReservation": attached,
                "updatedAt": attached["attachedAt"],
            }
        )
        await mutateChildAgent(
            agent_id,
            lambda current: {**(current or prepared["childRecord"]), "pid": identity["pid"], "processIdentity": identity},
        )
        activateOwnedProcessRunner(process_holder["process"], activation["activationToken"])

    async def fail(error: Any) -> None:
        failed = failTaskLaunchReservation(reservation, error)
        await writeTaskMeta(
            task_id,
            {
                **starting_meta,
                "status": "failed",
                "detailStatus": "launch_failed",
                "error": failed["failureReason"],
                "updatedAt": failed["failedAt"],
                "finishedAt": failed["failedAt"],
                "launchReservation": failed,
            },
        )
        await saveTaskRecord(
            {
                **record,
                "status": "failed",
                "summary": failed["failureReason"],
                "detailStatus": "launch_failed",
                "updatedAt": failed["failedAt"],
                "completedAt": failed["failedAt"],
                "launchReservation": failed,
            }
        )

    launched = await launchReservedProcess(
        reservation=reservation,
        expectedArgv=activation["argv"],
        reserve=reserve,
        spawn=spawn,
        attach=attach,
        fail=fail,
    )
    if launched.identity is None:
        raise RuntimeError("Background subagent exited before its process identity could be attached.")
    return {
        "taskId": task_id,
        "task_id": task_id,
        "outputFile": output_file,
        "output_file": output_file,
        "pid": launched.identity["pid"],
        "processIdentity": launched.identity,
    }


def _local_subagent_worker_argv(task_id: str, session_id: str, agent_id: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "agent_core.tasks.runners.subagent",
        "--session-id",
        session_id,
        "--agent-id",
        agent_id,
        "--task-id",
        task_id,
    ]


def _spawn_local_subagent_worker(
    task_id: str,
    session_id: str,
    agent_id: str,
    cwd: str,
    output_file: str,
    *,
    argv: list[str] | None = None,
    activation_stdin: bool = False,
) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    env = {
        **os.environ,
        # The worker (runners/subagent.py) resolves its runtime home from
        # AGENT_CORE_HOME; without the active home it would read the default
        # store and fail to find the child session.
        "AGENT_CORE_HOME": getStateRoot(),
        "TESS_CHILD_AGENT_ID": agent_id,
        "TESS_CHILD_TASK_ID": task_id,
        "TESS_CHILD_SESSION_ID": session_id,
    }
    try:
        return subprocess.Popen(
            argv or _local_subagent_worker_argv(task_id, session_id, agent_id),
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if activation_stdin else subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
    finally:
        handle.close()
