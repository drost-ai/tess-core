from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..._utils import now_iso
from ...storage import configure_runtime_home, read_task_meta, write_task_meta
from ..terminal_transitions import (
    apply_task_process_completion,
    normalize_task_process_outcome,
    resolve_task_semantic_status,
)


async def run_background_command(args: dict[str, Any]) -> dict[str, Any]:
    runtime_home = os.environ.get("TESS_CORE_HOME")
    if runtime_home:
        configure_runtime_home(runtime_home)

    task_id = str(args["taskId"])
    command = str(args["command"])
    cwd = str(args["cwd"])
    output_path = str(args["outputPath"])
    meta_path = str(args["metaPath"]) if args.get("metaPath") else None
    shell = str(args["shell"]) if args.get("shell") else _resolve_shell()
    if meta_path:
        Path(meta_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    process: subprocess.Popen[Any] | None = None
    def terminate_child(signum: int, _frame: Any) -> None:
        if process and process.poll() is None:
            _terminate_process(process.pid, signum)

    previous_term = signal.signal(signal.SIGTERM, terminate_child)
    previous_int = signal.signal(signal.SIGINT, terminate_child)
    try:
        with open(output_path, "ab") as output:
            process = subprocess.Popen(
                [shell, "-lc", command],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            await _merge_task_meta(
                task_id,
                {
                    "taskId": task_id,
                    "command": command,
                    "cwd": cwd,
                    "outputFile": output_path,
                    "status": "running",
                    "pid": process.pid,
                    "runnerPid": os.getpid(),
                    "detailStatus": "running",
                    "updatedAt": now_iso(),
                },
                meta_path,
            )
            raw_exit_code = process.wait()

        current = await _read_runner_meta(task_id, meta_path)
        now = now_iso()
        exit_code, signal_name = normalize_task_process_outcome(
            raw_exit_code,
            _exit_signal_name(raw_exit_code),
        )
        transition = apply_task_process_completion(
            current=resolve_task_semantic_status(current.get("status")),
            code=exit_code,
            signalName=signal_name,
            terminationIntent=(
                current.get("terminationIntent")
                if isinstance(current.get("terminationIntent"), dict)
                else None
            ),
        )
        if not transition["changed"]:
            update = {
                "exitCode": exit_code,
                "signal": signal_name or current.get("signal"),
            }
        else:
            intent = (
                current.get("terminationIntent")
                if isinstance(current.get("terminationIntent"), dict)
                else {}
            )
            terminal_status = (
                "terminated" if transition["status"] == "stopped" else transition["status"]
            )
            update = {
                "taskId": task_id,
                "command": command,
                "cwd": cwd,
                "outputFile": output_path,
                "pid": process.pid if process else current.get("pid"),
                "runnerPid": os.getpid(),
                "status": terminal_status,
                "updatedAt": now,
                "finishedAt": now,
                "detailStatus": transition["status"],
                "exitCode": exit_code,
                "signal": signal_name,
                "summary": transition["summary"],
                "terminalTransitionId": intent.get("transitionId") or str(uuid.uuid4()),
                "terminalEventOwner": intent.get("eventOwner") or "task_runtime",
            }
            if transition["status"] == "stopped":
                update["stoppedAt"] = current.get("stoppedAt") or now
                update["_clearError"] = True
            elif transition["status"] == "failed":
                update["error"] = transition["summary"]
        await _merge_task_meta(task_id, update, meta_path)
        return {"ok": True, "task_id": task_id, "exit_code": exit_code}
    except Exception as exc:
        now = now_iso()
        current = await _read_runner_meta(task_id, meta_path)
        if resolve_task_semantic_status(current.get("status")) == "running":
            await _merge_task_meta(
                task_id,
                {
                    "status": "failed",
                    "updatedAt": now,
                    "finishedAt": now,
                    "detailStatus": "failed",
                    "error": str(exc),
                    "runnerPid": os.getpid(),
                    "terminalTransitionId": str(uuid.uuid4()),
                    "terminalEventOwner": "task_runtime",
                },
                meta_path,
            )
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main() -> None:
    positional = _parse_positional_args()
    if positional:
        asyncio.run(run_background_command(positional))
        return
    parser = argparse.ArgumentParser(description="tess-core Python background shell runner")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--meta-path")
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--shell")
    parsed = parser.parse_args()
    asyncio.run(
        run_background_command(
            {
                "taskId": parsed.task_id,
                "metaPath": parsed.meta_path,
                "command": parsed.command,
                "cwd": parsed.cwd,
                "outputPath": parsed.output_path,
                "shell": parsed.shell,
            }
        )
    )


def _parse_positional_args() -> dict[str, str] | None:
    import sys

    if len(sys.argv) != 7 or sys.argv[1].startswith("-"):
        return None
    task_id, meta_path, output_path, cwd, shell, command = sys.argv[1:]
    return {"taskId": task_id, "metaPath": meta_path, "outputPath": output_path, "cwd": cwd, "shell": shell, "command": command}


async def _merge_task_meta(task_id: str, update: dict[str, Any], meta_path: str | None = None) -> None:
    current = await _read_runner_meta(task_id, meta_path)
    clean_update = dict(update)
    clear_error = bool(clean_update.pop("_clearError", False))
    next_meta = {**current, **clean_update}
    if clear_error:
        next_meta.pop("error", None)
    if meta_path:
        Path(meta_path).write_text(f"{json.dumps(next_meta, indent=2)}\n", encoding="utf-8")
    else:
        await write_task_meta(task_id, next_meta)


async def _read_runner_meta(task_id: str, meta_path: str | None = None) -> dict[str, Any]:
    if not meta_path:
        return await read_task_meta(task_id) or {"taskId": task_id, "id": task_id}
    try:
        return json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except Exception:
        return {"taskId": task_id, "id": task_id}


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


def _exit_signal_name(exit_code: int | None) -> str | None:
    if not isinstance(exit_code, int) or exit_code >= 0:
        return None
    with contextlib.suppress(Exception):
        return signal.Signals(-exit_code).name
    return f"SIG{-exit_code}"


def _resolve_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"


if __name__ == "__main__":
    main()
