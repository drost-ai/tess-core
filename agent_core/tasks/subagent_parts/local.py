from __future__ import annotations

from .shared import *


async def _start_local_background_subagent(prepared: dict[str, Any]) -> dict[str, Any]:
    agent_id = prepared["agentId"]
    child_session = prepared["childSession"]
    task_id = str(uuid.uuid4())
    output_file = taskOutputPath(task_id)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    metadata = child_session.setdefault("metadata", {})
    metadata["subagentTaskId"] = task_id
    if prepared.get("runtimeContract"):
        metadata["subagentRuntimeContract"] = prepared["runtimeContract"]
    await saveSession(child_session)
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
            "runtimeContract": prepared.get("runtimeContract"),
            "concurrencyGroup": (prepared.get("runtimeContract") or {}).get("concurrencyGroup"),
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
