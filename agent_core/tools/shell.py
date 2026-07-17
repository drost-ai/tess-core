from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.records import launch_output_file, launch_task_id
from ..contracts.tools import ToolResult

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from .._utils import maybe_await, now_iso, pretty_json
from ..execution_descriptors import createTaskExecutionDescriptor, summarizeOutputManifest
from ..owned_process_launch import activateOwnedProcessRunner, createOwnedProcessActivation
from ..storage import readTaskMeta, saveTaskRecord, taskOutputPath, writeTaskMeta
from ..task_process_launch import (
    attachTaskLaunchReservation,
    createTaskLaunchReservation,
    failTaskLaunchReservation,
    launchReservedProcess,
)
from ..tool_payloads import shapeTextToolResult
from .cancellation import _create_tool_abort_error, _signal_aborted, _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission
from .processes import _background_task_runner_argv, _communicate_shell_process, _prepare_sandboxed_argv, _resolve_shell, _spawn_background_task_runner, _track_background_runner
from .schemas import _tool_description, _tool_schema
from .session_metadata import _append_unique_metadata_list
from .validation import _require_string


def _bash_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        command = _require_string(input.get("command"), "command")
        timeout = _normalize_bash_timeout(command, input.get("timeout"))
        run_in_background = bool(input.get("run_in_background"))
        _throw_if_tool_cancelled(context)
        await _ensure_tool_permission(
            context,
            "Bash",
            "shell",
            f"{'Run shell command in background' if run_in_background else 'Run shell command'}\n{command}",
            always_ask=run_in_background,
            command=command,
        )
        if run_in_background:
            supervisor = context.get("daemon") or context.get("taskSupervisor")
            title = input.get("description") or input.get("title")
            session_registered = False
            if supervisor:
                started = await supervisor["startBackgroundCommand"](
                    {
                        "sessionId": context["session"]["id"],
                        "command": command,
                        "cwd": context["session"]["cwd"],
                        "title": title,
                        "sandbox": context.get("config", {}).get("sandbox"),
                        "runtimeHome": context.get("config", {}).get("runtimeHome"),
                    }
                )
                task_id = launch_task_id(started)
                output_path = launch_output_file(started) or taskOutputPath(task_id)
                task_record = await readTaskMeta(task_id) or {
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "startedAt": now_iso(),
                    "status": "running",
                    "outputFile": output_path,
                    "title": title,
                    "description": title,
                }
            else:
                task_id = str(uuid.uuid4())
                output_path = taskOutputPath(task_id)
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                reservation = createTaskLaunchReservation(
                    taskId=task_id,
                    sessionId=context["session"]["id"],
                    kind="background_command",
                )
                starting_meta = {
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "startedAt": reservation["createdAt"],
                    "updatedAt": reservation["createdAt"],
                    "status": "starting",
                    "outputFile": output_path,
                    "title": title,
                    "description": title,
                    "owner": "client",
                    "launchReservation": reservation,
                }
                record = {
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "sourceId": task_id,
                    "kind": "background_command",
                    "status": "queued",
                    "title": title or command[:80],
                    "summary": "Background command is starting.",
                    "createdAt": reservation["createdAt"],
                    "updatedAt": reservation["createdAt"],
                    "startedAt": reservation["createdAt"],
                    "completedAt": None,
                    "foregroundable": True,
                    "restorable": True,
                    "cwd": context["session"]["cwd"],
                    "command": command,
                    "outputFile": output_path,
                    "owner": "client",
                    "foregroundMode": "background",
                    "taskFamily": "shell",
                    "detailStatus": "starting",
                    "retrieveOnly": True,
                    "resultSummary": None,
                    "metadata": {},
                    "launchReservation": reservation,
                }
                runner_argv = _background_task_runner_argv(
                    task_id,
                    command,
                    context["session"]["cwd"],
                    output_path,
                    context,
                )
                activation = createOwnedProcessActivation(runner_argv)
                process_holder: dict[str, Any] = {}

                async def reserve() -> None:
                    nonlocal session_registered
                    await writeTaskMeta(task_id, starting_meta)
                    await saveTaskRecord(record)
                    _append_unique_metadata_list(context["session"], "taskIds", task_id)
                    await maybe_await(context.get("persistSession", lambda: None)())
                    session_registered = True

                def spawn():
                    runner = _spawn_background_task_runner(
                        task_id,
                        command,
                        context["session"]["cwd"],
                        output_path,
                        context,
                        argv=activation["argv"],
                        activation_stdin=True,
                    )
                    process_holder["runner"] = runner
                    return runner

                async def attach(identity: dict[str, Any]) -> None:
                    attached = attachTaskLaunchReservation(reservation)
                    await writeTaskMeta(
                        task_id,
                        {
                            **starting_meta,
                            "status": "running",
                            "runnerPid": identity["pid"],
                            "processIdentity": identity,
                            "launchReservation": attached,
                            "detailStatus": "launching",
                            "updatedAt": attached["attachedAt"],
                        },
                    )
                    await saveTaskRecord(
                        {
                            **record,
                            "status": "backgrounded",
                            "summary": "Background command launched by client runner.",
                            "detailStatus": "launching",
                            "processIdentity": identity,
                            "launchReservation": attached,
                            "updatedAt": attached["attachedAt"],
                        }
                    )
                    activateOwnedProcessRunner(process_holder["runner"], activation["activationToken"])

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
                    raise RuntimeError("Background runner exited before its process identity could be attached.")
                _track_background_runner(launched.child)
                task_record = await readTaskMeta(task_id) or starting_meta
            task_record = {
                **{
                    "taskId": task_id,
                    "id": task_id,
                    "sessionId": context["session"]["id"],
                    "command": command,
                    "cwd": context["session"]["cwd"],
                    "startedAt": now_iso(),
                    "status": "running",
                    "outputFile": output_path,
                    "title": title,
                    "description": title,
                },
                **task_record,
                "taskId": task_id,
                "outputFile": output_path,
            }
            if not session_registered:
                _append_unique_metadata_list(context["session"], "taskIds", task_id)
                await maybe_await(context.get("persistSession", lambda: None)())
            context.get("outputJson", lambda *_: None)(
                "task.registered",
                {
                    "session_id": context["session"]["id"],
                    "task_id": task_id,
                    "kind": "background_command",
                    "status": "backgrounded",
                    "title": task_record.get("title") or task_record.get("description") or command,
                    "owner": "daemon" if context.get("daemon") or context.get("taskSupervisor") else "client",
                },
            )
            execution = createTaskExecutionDescriptor(
                task_record,
                "",
                [
                    {
                        "kind": "task",
                        "status": "info",
                        "summary": "Background task launched. Call TaskMonitorStart if the assistant should keep watching it proactively.",
                        "subjectId": task_id,
                    }
                ],
            )
            return {
                "content": pretty_json(
                    {
                        "task_id": task_id,
                        "status": "running",
                        "output_file": output_path,
                        "execution": execution,
                        "next_step": "If you need to proactively keep watching this task and update the user without another prompt, call TaskMonitorStart with this task_id.",
                    }
                ),
                "meta": {"execution": execution},
            }
        argv = _prepare_sandboxed_argv([_resolve_shell(), "-lc", command], context, context["session"]["cwd"])
        proc = await asyncio.create_subprocess_exec(*argv, cwd=context["session"]["cwd"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr, timed_out = await _communicate_shell_process(proc, timeout, context)
        if _signal_aborted(context.get("abortSignal")):
            raise _create_tool_abort_error()
        exit_code = None if timed_out or (isinstance(proc.returncode, int) and proc.returncode < 0) else proc.returncode
        shaped_stdout = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="Bash", family="bash", text=stdout.decode(errors="replace"), strategy="head_tail")
        shaped_stderr = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="Bash", family="bash", text=stderr.decode(errors="replace"), strategy="head_tail")
        execution = {
            "id": f"{context['session']['id']}:bash",
            "kind": "tool",
            "title": "Shell command completed",
            "status": "failed" if timed_out or exit_code else "completed",
            "cwd": context["session"]["cwd"],
            "command": command,
            "output": summarizeOutputManifest(f"{stdout.decode(errors='replace')}{stderr.decode(errors='replace')}"),
            "metadata": {"exit_code": exit_code, "timed_out": timed_out},
        }
        return {
            "content": pretty_json({"command": command, "cwd": context["session"]["cwd"], "exitCode": exit_code, "timedOut": timed_out, "stdout": shaped_stdout["text"], "stderr": shaped_stderr["text"], "stdout_manifest": shaped_stdout["view"], "stderr_manifest": shaped_stderr["view"]}),
            "isError": bool(timed_out or exit_code),
            "meta": {"execution": execution},
        }

    return ToolDefinition("Bash", _tool_description("Bash"), _tool_schema("Bash"), handler)


def _normalize_bash_timeout(command: str, value: Any) -> int:
    raw = 120000 if value is None else float(value)
    timeout = raw * 1000 if 0 < raw <= 600 else raw
    if re.search(r"\b(?:npm|pnpm|yarn|bun)\s+install\b", command) and timeout < 300000:
        timeout = 300000
    return int(min(timeout, 600000))
