from __future__ import annotations

from .shared import *
from ...sandbox import prepareSandboxedArgv


def _spawn_shell(
    command: str,
    cwd: str,
    output_file: str,
    *,
    sandbox_config: dict[str, Any] | None = None,
    runtime_home: str | None = None,
    sandbox_events: list[dict[str, Any]] | None = None,
) -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    argv = [_resolve_shell(), "-lc", command]
    if sandbox_config:
        def emit_sandbox_event(event: str, data: dict[str, Any]) -> None:
            if sandbox_events is not None:
                sandbox_events.append({"event": event, "data": data})

        launch = prepareSandboxedArgv(
            argv,
            config={"sandbox": sandbox_config, "runtimeHome": runtime_home},
            cwd=cwd,
            extraWritableRoots=[root for root in (os.path.dirname(output_file), runtime_home) if root],
            emit=emit_sandbox_event,
        )
        argv = launch["argv"]
    handle = open(output_file, "ab")
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    finally:
        handle.close()


def _spawn_main_session_worker(
    task_id: str,
    session_id: str,
    prompt: str,
    cwd: str,
    output_file: str,
    *,
    runtime_home: str | None = None,
) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    env = _worker_env({"AGENT_CORE_HOME": runtime_home} if runtime_home else None)
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_core.background_main_session_runner",
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--prompt",
                prompt,
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


def _spawn_subagent_worker(
    task_id: str,
    session_id: str,
    agent_id: str,
    cwd: str,
    output_file: str,
    *,
    runtime_home: str | None = None,
) -> subprocess.Popen[Any]:
    handle = open(output_file, "ab")
    kwargs: dict[str, Any] = {}
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid
    env = _worker_env(
        {
            "TESS_CHILD_AGENT_ID": agent_id,
            "TESS_CHILD_TASK_ID": task_id,
            "TESS_CHILD_SESSION_ID": session_id,
            **({"AGENT_CORE_HOME": runtime_home} if runtime_home else {}),
        }
    )
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


def _worker_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, **(extra or {})}
    package_root = str(Path(__file__).resolve().parents[3])
    env["PYTHONPATH"] = package_root if not env.get("PYTHONPATH") else f"{package_root}{os.pathsep}{env['PYTHONPATH']}"
    return env


def _watch_process_thread(
    task_id: str,
    command: str,
    cwd: str,
    output_file: str,
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> None:
    proc = _PROCESS_REGISTRY.get(task_id)
    if not proc:
        return
    code = proc.wait()
    _PROCESS_REGISTRY.pop(task_id, None)
    now = now_iso()
    previous_status_seen: list[Any] = [None]

    def _finalize_exit(previous_raw: dict[str, Any] | None) -> dict[str, Any]:
        previous = previous_raw or {}
        previous_status = previous.get("status")
        previous_status_seen[0] = previous_status
        if _is_terminal_worker_status(previous_status):
            status = str(previous_status)
        else:
            status = "completed" if code == 0 else "failed"
            if code is not None and code < 0:
                status = "terminated" if abs(code) in {signal.SIGINT, signal.SIGTERM} else "failed"
        exit_code = previous.get("exitCode") if _is_terminal_worker_status(previous_status) else None if status == "terminated" else code
        signal_name = previous.get("signal")
        if signal_name is None and code is not None and code < 0:
            signal_name = _signal_name(abs(code))
        return {
            **previous,
            "type": previous.get("type") or "background_command",
            "taskId": task_id,
            "id": task_id,
            "command": previous.get("command") or command,
            "cwd": previous.get("cwd") or cwd,
            "outputFile": previous.get("outputFile") or output_file,
            "status": status,
            "exitCode": exit_code,
            "signal": signal_name,
            "finishedAt": previous.get("finishedAt") or previous.get("completedAt") or now,
            "updatedAt": now,
            "detailStatus": previous.get("detailStatus") or previous.get("completionReason") or previous.get("waitingReason") or status,
        }

    meta = asyncio.run(mutateTaskMeta(task_id, _finalize_exit, fallback={}))
    previous_status = previous_status_seen[0]
    summary = None if _is_terminal_worker_status(previous_status) else _process_exit_summary(str(meta.get("status")), code)
    record = _build_task_record(meta, summary)
    asyncio.run(saveTaskRecord(record))
    event_session_id = meta.get("parent_session_id") if meta.get("type") == "subagent" else meta.get("sessionId")
    asyncio.run(maybe_await(emit(_event_name_for_task_status(record.get("status")), _task_record_event_payload(record), event_session_id)))
    _WATCHERS.discard(threading.current_thread())


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
