from __future__ import annotations

from ..contracts.context import ToolContext

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..sandbox import prepareSandboxedArgv
from .cancellation import _create_tool_abort_error, _signal_aborted


_BACKGROUND_RUNNERS: list[subprocess.Popen[Any]] = []


def _background_task_runner_argv(
    task_id: str,
    command: str,
    cwd: str,
    output_path: str,
    context: ToolContext | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "agent_core.tasks.runners.background",
        "--task-id",
        task_id,
        "--command",
        command,
        "--cwd",
        cwd,
        "--output-path",
        output_path,
    ]
    if context:
        argv = _prepare_sandboxed_argv(
            argv,
            context,
            cwd,
            extra_writable_roots=[os.path.dirname(output_path), context.get("config", {}).get("runtimeHome")],
        )
    return argv


def _spawn_background_task_runner(
    task_id: str,
    command: str,
    cwd: str,
    output_path: str,
    context: ToolContext | None = None,
    *,
    argv: list[str] | None = None,
    activation_stdin: bool = False,
) -> subprocess.Popen[Any]:
    argv = argv or _background_task_runner_argv(task_id, command, cwd, output_path, context)
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=_background_runner_env(),
        stdin=subprocess.PIPE if activation_stdin else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _prepare_sandboxed_argv(
    argv: list[str],
    context: ToolContext,
    cwd: str,
    *,
    extra_writable_roots: list[str | None] | None = None,
) -> list[str]:
    launch = prepareSandboxedArgv(
        argv,
        config=context.get("config") or {},
        session=context.get("session"),
        cwd=cwd,
        extraWritableRoots=[root for root in (extra_writable_roots or []) if root],
        emit=context.get("outputJson"),
    )
    return launch["argv"]


def _background_runner_env() -> dict[str, str]:
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = package_root if not env.get("PYTHONPATH") else f"{package_root}{os.pathsep}{env['PYTHONPATH']}"
    return env


def _track_background_runner(runner: subprocess.Popen[Any]) -> None:
    _BACKGROUND_RUNNERS[:] = [process for process in _BACKGROUND_RUNNERS if process.poll() is None]
    _BACKGROUND_RUNNERS.append(runner)


async def _wait_for_tracked_background_runner(pid: int, timeout: float = 2) -> None:
    for process in list(_BACKGROUND_RUNNERS):
        if process.pid != pid:
            continue
        if process.poll() is None:
            try:
                await asyncio.to_thread(process.wait, timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        break
    _BACKGROUND_RUNNERS[:] = [process for process in _BACKGROUND_RUNNERS if process.poll() is None]


async def _communicate_shell_process(proc: asyncio.subprocess.Process, timeout_ms: int, context: ToolContext) -> tuple[bytes, bytes, bool]:
    communicate = asyncio.create_task(proc.communicate())
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    timed_out = False
    while not communicate.done():
        if _signal_aborted(context.get("abortSignal")):
            _terminate_asyncio_process(proc)
            await _finish_terminated_process(proc, communicate)
            raise _create_tool_abort_error()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            timed_out = True
            _terminate_asyncio_process(proc)
            break
        await asyncio.wait({communicate}, timeout=min(0.05, remaining))
    stdout, stderr = await _finish_terminated_process(proc, communicate)
    return stdout, stderr, timed_out


async def _finish_terminated_process(proc: asyncio.subprocess.Process, communicate: asyncio.Task[tuple[bytes, bytes]]) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(asyncio.shield(communicate), 2)
    except asyncio.TimeoutError:
        _kill_asyncio_process(proc)
        return await communicate


def _terminate_asyncio_process(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


def _kill_asyncio_process(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _resolve_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "cmd.exe" if os.name == "nt" else "/bin/sh"
