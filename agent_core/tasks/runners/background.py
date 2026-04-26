from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from ..._utils import now_iso
from ...storage import configureRuntimeHome, readTaskMeta, writeTaskMeta


async def run_background_command(args: dict[str, Any]) -> dict[str, Any]:
    runtime_home = os.environ.get("AGENT_CORE_HOME")
    if runtime_home:
        configureRuntimeHome(runtime_home)

    task_id = str(args["taskId"])
    command = str(args["command"])
    cwd = str(args["cwd"])
    output_path = str(args["outputPath"])
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    process: subprocess.Popen[Any] | None = None
    stop_requested = False

    def terminate_child(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        if process and process.poll() is None:
            _terminate_process(process.pid, signum)

    previous_term = signal.signal(signal.SIGTERM, terminate_child)
    previous_int = signal.signal(signal.SIGINT, terminate_child)
    try:
        with open(output_path, "ab") as output:
            process = subprocess.Popen(
                [_resolve_shell(), "-lc", command],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            await _merge_task_meta(
                task_id,
                {
                    "pid": process.pid,
                    "detailStatus": "running",
                    "updatedAt": now_iso(),
                },
            )
            exit_code = process.wait()

        current = await readTaskMeta(task_id) or {}
        now = now_iso()
        if current.get("status") == "terminated":
            update = {
                "updatedAt": now,
                "stoppedAt": current.get("stoppedAt") or now,
                "finishedAt": now,
                "detailStatus": "terminated",
                "exitCode": exit_code,
            }
        elif stop_requested or current.get("status") == "stopped" or _terminated_by_request(exit_code):
            update = {
                "status": "terminated",
                "updatedAt": now,
                "stoppedAt": current.get("stoppedAt") or now,
                "finishedAt": now,
                "detailStatus": "terminated",
                "exitCode": exit_code,
            }
        else:
            status = "completed" if exit_code == 0 else "failed"
            update = {
                "status": status,
                "updatedAt": now,
                "finishedAt": now,
                "detailStatus": status,
                "exitCode": exit_code,
            }
        await _merge_task_meta(task_id, update)
        return {"ok": True, "task_id": task_id, "exit_code": exit_code}
    except Exception as exc:
        now = now_iso()
        await _merge_task_meta(
            task_id,
            {
                "status": "failed",
                "updatedAt": now,
                "finishedAt": now,
                "detailStatus": "failed",
                "error": str(exc),
            },
        )
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-core Python background shell runner")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--output-path", required=True)
    parsed = parser.parse_args()
    asyncio.run(
        run_background_command(
            {
                "taskId": parsed.task_id,
                "command": parsed.command,
                "cwd": parsed.cwd,
                "outputPath": parsed.output_path,
            }
        )
    )


async def _merge_task_meta(task_id: str, update: dict[str, Any]) -> None:
    current = await readTaskMeta(task_id) or {"taskId": task_id, "id": task_id}
    await writeTaskMeta(task_id, {**current, **update})


def _terminate_process(pid: int | None, signum: int = signal.SIGTERM) -> bool:
    if not pid:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signum)
        return True
    with contextlib.suppress(Exception):
        os.kill(pid, signum)
        return True
    return False


def _terminated_by_request(exit_code: int | None) -> bool:
    return exit_code in (-signal.SIGTERM, -signal.SIGINT)


def _resolve_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
