from __future__ import annotations

from ..._utils import is_process_alive as _is_process_alive

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
from ..._utils import maybe_await
from ...storage import getStateRoot, loadTaskRecord, readTaskMeta
from .mappers import (
    _event_name_for_task_status,
    _is_terminal_worker_status,
    _task_record_event_payload,
)
from .records import _build_task_record
from .shared import SupervisorRegistries
from pathlib import Path
from typing import Any, Callable
from ..terminal_store import commitTaskProcessCompletion, saveProjectedTaskRecord
from ...sandbox import prepareSandboxedArgv


def _prepare_shell_argv(
    command: str,
    cwd: str,
    *,
    sandbox_config: dict[str, Any] | None = None,
    runtime_home: str | None = None,
    output_file: str | None = None,
    sandbox_events: list[dict[str, Any]] | None = None,
) -> list[str]:
    argv = [_resolve_shell(), "-lc", command]
    if not sandbox_config:
        return argv

    def emit_sandbox_event(event: str, data: dict[str, Any]) -> None:
        if sandbox_events is not None:
            sandbox_events.append({"event": event, "data": data})

    launch = prepareSandboxedArgv(
        argv,
        config={"sandbox": sandbox_config, "runtimeHome": runtime_home},
        cwd=cwd,
        extraWritableRoots=[root for root in (os.path.dirname(output_file) if output_file else None, runtime_home) if root],
        emit=emit_sandbox_event,
    )
    return launch["argv"]


def _spawn_owned_argv(
    argv: list[str],
    cwd: str,
    output_file: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    handle = open(output_file, "ab")
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            env=env,
            **kwargs,
        )
    finally:
        handle.close()








def _worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # Workers resolve their runtime home from AGENT_CORE_HOME. Stamp the
    # active home so a client that configured it in-process (config option
    # or scoped home) still spawns workers against the same store; explicit
    # per-task overrides in ``extra`` win.
    env = {**os.environ, "AGENT_CORE_HOME": getStateRoot(), **(extra or {})}
    package_root = str(Path(__file__).resolve().parents[3])
    env["PYTHONPATH"] = package_root if not env.get("PYTHONPATH") else f"{package_root}{os.pathsep}{env['PYTHONPATH']}"
    return env


def _watch_process_thread(
    task_id: str,
    command: str,
    cwd: str,
    output_file: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
    wait_for_stop_commit: Callable[[str], None],
    registries: SupervisorRegistries,
) -> None:
    try:
        proc = registries.processes.get(task_id)
        if not proc:
            return
        code = proc.wait()
        wait_for_stop_commit(task_id)
        registries.processes.pop(task_id, None)
        signal_name = _signal_name(abs(code)) if code is not None and code < 0 else None

        async def finalize() -> None:
            committed = await commitTaskProcessCompletion(
                taskId=task_id,
                code=code,
                signalName=signal_name,
            )
            transition = committed["transition"]
            record = committed.get("record")
            if transition["changed"]:
                if record and transition.get("event"):
                    await maybe_await(
                        emit(
                            transition["event"],
                            _task_record_event_payload(record),
                            record.get("sessionId"),
                        )
                    )
                return

            # A worker may have durably written terminal task metadata before
            # the process watcher observes exit.  Project that worker-owned
            # state once, while preserving a supervisor/tool terminal record
            # as the first-terminal-wins authority.
            existing = await loadTaskRecord(task_id)
            if existing and existing.get("status") in {"completed", "failed", "stopped"}:
                return
            meta = await readTaskMeta(task_id)
            if not meta or not _is_terminal_worker_status(meta.get("status")):
                return
            projected = _build_task_record(meta, None)
            saved = await saveProjectedTaskRecord(projected)
            metadata = saved.get("metadata") if isinstance(saved.get("metadata"), dict) else {}
            owner = metadata.get("terminal_event_owner")
            if owner in {"supervisor", "tool"}:
                return
            event_session_id = (
                meta.get("parent_session_id")
                if meta.get("type") == "subagent"
                else meta.get("sessionId")
            )
            await maybe_await(
                emit(
                    _event_name_for_task_status(saved.get("status")),
                    _task_record_event_payload(saved),
                    event_session_id,
                )
            )

        asyncio.run(finalize())
    finally:
        registries.threadWatchers.discard(threading.current_thread())




def _terminate_process(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        with contextlib.suppress(Exception):
            os.kill(pid, signal.SIGTERM)
            return True
        return False


def _signal_name(signum: int) -> str | None:
    try:
        return signal.Signals(signum).name
    except Exception:
        return None


def _resolve_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    return "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"


def _process_exit_summary(status: str, code: int | None) -> str:
    if status == "completed":
        return "Task completed successfully."
    if status in {"stopped", "terminated"}:
        return "Task was terminated."
    return f"Task failed with exit code {code}." if isinstance(code, int) else "Task failed."
