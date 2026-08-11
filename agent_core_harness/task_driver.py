from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest import mock

import agent_core._facade as core
from agent_core.tasks.health_check_policy import (
    authorizeHealthCheckSpec,
    healthCheckSpecDigest,
    normalizeHealthCheckSpec,
)

from .model import ScenarioAssertionError
from .runtime_driver import ensure_session_config
from .sandbox import ScenarioSandbox

HARNESS_DAEMON_INSTANCE = "harness-daemon-instance"


async def ensure_daemon(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    await ensure_session_config(data, sandbox, state)
    client = await core.ensureTaskSupervisorDaemon({"instanceId": HARNESS_DAEMON_INSTANCE, "cwd": str(sandbox.workspace)})
    state["daemon_client"] = client
    state.setdefault("task_driver_results", []).append({"driver": "ensure_daemon", "status": "passed", "state": _json_safe(client.get("getState", lambda: {})())})
    await refresh_task_state(sandbox, state)
    return ["daemon_state", "task_driver_results"]


async def daemon_subscribe(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    client = await ensure_task_client(data, sandbox, state)
    events = state.setdefault("daemon_events", [])
    unsubscribe = client["subscribe"](lambda event: events.append(_json_safe(event)), {"sessionId": data.get("sessionId") or session["id"], "afterCursor": data.get("afterCursor", 0)})
    state.setdefault("task_unsubscribers", []).append(unsubscribe)
    state.setdefault("task_driver_results", []).append({"driver": "daemon_subscribe", "status": "passed", "sessionId": data.get("sessionId") or session["id"]})
    await refresh_task_state(sandbox, state)
    return ["daemon_events", "task_driver_results"]


async def start_background_task(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    client = await ensure_task_client(data, sandbox, state)
    command = _command_from_data(data, sandbox)
    with mock.patch.dict(os.environ, _isolated_task_environment(sandbox), clear=True):
        result = await client["startBackgroundCommand"]({"sessionId": session["id"], "command": command, "cwd": str(sandbox.workspace), "title": data.get("title")})
    _remember_task(state, data, result["taskId"])
    state.setdefault("task_driver_results", []).append({"driver": "start_background_task", "status": "passed", "taskId": result["taskId"], "command": command, "result": _json_safe(result)})
    await refresh_task_state(sandbox, state)
    return ["task_records", "task_metadata", "task_driver_results", "daemon_events"]


async def wait_task_status(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    task_id = _task_id(data, state)
    statuses = set(data.get("statusIn") or [data.get("status", "completed")])
    required_event = data.get("event")
    deadline = asyncio.get_running_loop().time() + float(data.get("timeoutSeconds") or 8)
    last_meta = None
    while asyncio.get_running_loop().time() < deadline:
        last_meta = await core.read_task_meta(task_id)
        if last_meta and last_meta.get("status") in statuses:
            await refresh_task_state(sandbox, state)
            if required_event and not any(
                event.get("event") == required_event
                and event.get("data", {}).get("task_id") == task_id
                for event in [
                    *(state.get("daemon_events") or []),
                    *(state.get("persisted_daemon_events") or []),
                ]
            ):
                await asyncio.sleep(float(data.get("pollSeconds") or 0.05))
                continue
            return ["task_records", "task_metadata", "daemon_events"]
        await asyncio.sleep(float(data.get("pollSeconds") or 0.05))
    raise ScenarioAssertionError(f"Task {task_id} did not reach {sorted(statuses)}; last={last_meta}", category="driver")


async def wait_health_check(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    task_id = _task_id(data, state)
    status = data.get("status")
    require_success = bool(data.get("requireSuccess"))
    required_event = data.get("event")
    deadline = asyncio.get_running_loop().time() + float(data.get("timeoutSeconds") or 10)
    last_health = None
    while asyncio.get_running_loop().time() < deadline:
        last_health = await core.load_health_check_task(task_id)
        if last_health and (not status or last_health.get("status") == status) and (not require_success or last_health.get("lastSuccessAt")):
            await refresh_task_state(sandbox, state)
            if required_event and not any(
                event.get("event") == required_event
                and event.get("data", {}).get("task_id") == task_id
                for event in state.get("persisted_daemon_events", [])
            ):
                await asyncio.sleep(float(data.get("pollSeconds") or 0.05))
                continue
            return ["health_checks", "task_records", "task_metadata", "daemon_events"]
        await asyncio.sleep(float(data.get("pollSeconds") or 0.05))
    raise ScenarioAssertionError(f"Health check {task_id} did not reach expected state; last={last_health}", category="driver")


async def read_task_output(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    task_id = _task_id(data, state)
    client = await ensure_task_client(data, sandbox, state)
    result = await client["readTaskOutput"](task_id, data.get("tailBytes"))
    record = {"driver": "read_task_output", "status": "passed", "taskId": task_id, "result": _json_safe(result)}
    state.setdefault("task_outputs", []).append(record)
    state.setdefault("task_driver_results", []).append(record)
    await refresh_task_state(sandbox, state)
    return ["task_outputs", "task_driver_results"]


async def stop_task(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    task_id = _task_id(data, state)
    client = await ensure_task_client(data, sandbox, state)
    stopped = await client["stopTask"](task_id)
    state.setdefault("task_driver_results", []).append({"driver": "stop_task", "status": "passed" if stopped else "failed", "taskId": task_id, "stopped": stopped})
    if not stopped and not data.get("expectFailure"):
        raise ScenarioAssertionError(f"Task not stopped: {task_id}", category="driver")
    await refresh_task_state(sandbox, state)
    return ["task_records", "task_metadata", "task_driver_results", "daemon_events"]


async def create_monitor(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": _task_id(data, state), "goal": data.get("goal", "watch task"), "checkIntervalMs": data.get("checkIntervalMs", 2000)})
    await core.save_monitor(monitor)
    metadata = session.setdefault("metadata", {})
    metadata["monitorIds"] = _dedupe([*(metadata.get("monitorIds") or []), monitor["id"]])
    await core.save_session(session)
    state.setdefault("monitor_aliases", {})[data.get("alias") or "last"] = monitor["id"]
    state.setdefault("task_driver_results", []).append({"driver": "create_monitor", "status": "passed", "monitorId": monitor["id"], "taskId": monitor["taskId"]})
    await refresh_task_state(sandbox, state)
    return ["monitor_records", "task_driver_results"]


async def get_daemon_bootstrap(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    client = await ensure_task_client(data, sandbox, state)
    bootstrap = await client["getBootstrap"](data.get("sessionId") or session["id"])
    state.setdefault("daemon_bootstrap", []).append(_json_safe(bootstrap))
    state.setdefault("task_driver_results", []).append({"driver": "get_daemon_bootstrap", "status": "passed", "taskTotal": bootstrap.get("taskSnapshot", {}).get("total", 0)})
    await refresh_task_state(sandbox, state)
    return ["daemon_bootstrap", "task_driver_results"]


async def seed_recovery_task(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    task_id = data.get("taskId") or data.get("alias") or "recovery-task"
    output_file = core.task_output_path(task_id)
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    Path(output_file).write_text(data.get("output", "still running\n"), encoding="utf-8")
    argv = [sys.executable, "-c", data.get("script", "import time; time.sleep(30)")]
    proc = subprocess.Popen(argv, cwd=str(sandbox.workspace), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    sandbox.process_registry.append({"process": proc, "kind": "recovery_task", "taskId": task_id})
    state.setdefault("owned_processes", []).append(proc)
    created = data.get("createdAt") or "2026-01-01T00:00:00Z"
    identity = await core.captureOwnedProcessIdentity({"pid": proc.pid, "expectedArgv": argv, "spawnedAt": created})
    meta = {"type": data.get("type", "background_command"), "taskId": task_id, "id": task_id, "sessionId": session["id"], "command": data.get("command", "sleep"), "cwd": str(sandbox.workspace), "startedAt": created, "updatedAt": created, "status": "running", "outputFile": output_file, "pid": proc.pid, "processIdentity": identity, "detailStatus": data.get("detailStatus", "running"), "owner": "daemon", "supervisorId": data.get("supervisorId", "previous-supervisor")}
    await core.write_task_meta(task_id, meta)
    await core.save_task_record({"id": task_id, "sessionId": session["id"], "sourceId": task_id, "kind": "background_command", "status": "running", "title": data.get("title", "recovery task"), "summary": "Background command is running.", "createdAt": created, "updatedAt": created, "startedAt": created, "cwd": str(sandbox.workspace), "command": meta["command"], "outputFile": output_file, "foregroundMode": "background", "taskFamily": "shell", "detailStatus": meta["detailStatus"], "retrieveOnly": True, "owner": "daemon", "supervisorId": meta["supervisorId"], "processIdentity": identity, "metadata": {"pid": proc.pid, "processIdentity": identity}})
    _remember_task(state, data, task_id)
    state.setdefault("task_driver_results", []).append({"driver": "seed_recovery_task", "status": "passed", "taskId": task_id, "pid": proc.pid})
    await refresh_task_state(sandbox, state)
    return ["task_records", "task_metadata", "task_driver_results"]


async def restart_daemon(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    await _unsubscribe_all(state)
    await core.stopTaskSupervisorDaemon({"instanceId": HARNESS_DAEMON_INSTANCE})
    state.pop("daemon_client", None)
    return await ensure_daemon(data, sandbox, state)


async def run_worker_entrypoint(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    task_id = data.get("taskId") or data.get("alias") or "worker-entrypoint"
    worker_dir = sandbox.home / "worker-entrypoints"
    worker_dir.mkdir(parents=True, exist_ok=True)
    meta_path = worker_dir / f"{task_id}.meta.json"
    output_path = worker_dir / f"{task_id}.output.log"
    command = data.get("command") or "printf worker-entrypoint-ok"
    env = _isolated_task_environment(sandbox)
    shell = data.get("shell") or env["SHELL"]
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo_root if not env.get("PYTHONPATH") else f"{repo_root}{os.pathsep}{env['PYTHONPATH']}"
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_core.tasks.runners.background", task_id, str(meta_path), str(output_path), str(sandbox.workspace), shell, command, cwd=str(sandbox.workspace), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=float(data.get("timeoutSeconds") or 10))
    meta = _read_json(meta_path)
    failed = proc.returncode != 0 or (isinstance(meta, dict) and (meta.get("status") == "failed" or meta.get("exitCode") not in (0, None)))
    child_exit = meta.get("exitCode") if isinstance(meta, dict) else None
    record = {"driver": "run_worker_entrypoint", "status": "failed" if failed else "passed", "taskId": task_id, "returnCode": child_exit if isinstance(child_exit, int) else proc.returncode, "runnerReturnCode": proc.returncode, "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace"), "meta": meta, "output": output_path.read_text(encoding="utf-8") if output_path.exists() else ""}
    state.setdefault("worker_results", []).append(record)
    state.setdefault("task_driver_results", []).append(record)
    if record["status"] == "failed" and not data.get("expectFailure"):
        raise ScenarioAssertionError(f"worker entrypoint failed: {record['stderr']}", category="driver")
    return ["worker_results", "task_driver_results", "workspace_tree"]


async def start_health_check(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    client = await ensure_task_client(data, sandbox, state)
    payload = {key: value for key, value in data.items() if key not in {"alias", "scriptTemplate"}}
    payload.setdefault("sessionId", session["id"])
    payload.setdefault("cwd", str(sandbox.workspace))
    if data.get("scriptTemplate") == "fail_twice_then_succeed":
        payload["mode"] = "command"
        payload["command"] = _health_recovery_command(sandbox, data.get("counterFile", "health-count.txt"))
    with mock.patch.dict(os.environ, _isolated_task_environment(sandbox), clear=True):
        spec = await normalizeHealthCheckSpec(payload, str(sandbox.workspace))
        execution_policy = await authorizeHealthCheckSpec(
            {
                "session": session,
                "config": config,
                "interactive": False,
                "outputJson": lambda *_args: None,
                "persistSession": lambda: None,
            },
            spec,
        )
        result = await client["startHealthCheck"](
            {
                "sessionId": session["id"],
                "spec": spec,
                "specDigest": healthCheckSpecDigest(spec),
                "executionPolicy": execution_policy,
            }
        )
    _remember_task(state, data, result["taskId"])
    state.setdefault("task_driver_results", []).append({"driver": "start_health_check", "status": "passed", "taskId": result["taskId"], "result": _json_safe(result)})
    await refresh_task_state(sandbox, state)
    return ["health_checks", "task_records", "task_metadata", "task_driver_results"]


def assert_task_record(state: dict[str, Any], data: dict[str, Any]) -> None:
    task_id = _task_id(data, state)
    record = _find_by_id(state.get("task_records") or [], task_id) or _find_by_task_id(state.get("task_metadata") or [], task_id)
    if not record:
        raise ScenarioAssertionError(f"Task record not found: {task_id}")
    _assert_fields(record, data, ("status", "kind", "taskFamily", "detailStatus", "owner"))
    if "metadataIncludes" in data:
        metadata = record.get("metadata") or record
        missing = [key for key in data["metadataIncludes"] if key not in metadata]
        if missing:
            raise ScenarioAssertionError(f"Task metadata missing keys: {missing}")


def assert_daemon_event(state: dict[str, Any], data: dict[str, Any]) -> None:
    events = [*(state.get("daemon_events") or []), *(state.get("persisted_daemon_events") or [])]
    names = [event.get("event") for event in events]
    missing = [name for name in data.get("eventsInclude", []) if name not in names]
    if missing:
        raise ScenarioAssertionError(f"Daemon events missing: {missing}")


def assert_task_output(state: dict[str, Any], data: dict[str, Any]) -> None:
    task_id = _task_id(data, state)
    outputs = [item for item in state.get("task_outputs", []) if item.get("taskId") == task_id]
    if not outputs:
        raise ScenarioAssertionError(f"No task output captured for {task_id}")
    output = str((outputs[-1].get("result") or {}).get("output") or "")
    if data.get("contains") and data["contains"] not in output:
        raise ScenarioAssertionError(f"Task output did not contain {data['contains']!r}")


def assert_bootstrap(state: dict[str, Any], data: dict[str, Any]) -> None:
    bootstraps = state.get("daemon_bootstrap") or []
    if not bootstraps:
        raise ScenarioAssertionError("No daemon bootstrap captured")
    bootstrap = bootstraps[int(data.get("index", len(bootstraps) - 1))]
    task_snapshot = bootstrap.get("taskSnapshot") or {}
    monitor_snapshot = bootstrap.get("monitorSnapshot") or {}
    if "minTasks" in data and int(task_snapshot.get("total") or 0) < int(data["minTasks"]):
        raise ScenarioAssertionError(f"Expected at least {data['minTasks']} bootstrap tasks")
    if "minMonitors" in data and int(monitor_snapshot.get("total") or 0) < int(data["minMonitors"]):
        raise ScenarioAssertionError(f"Expected at least {data['minMonitors']} bootstrap monitors")


async def refresh_task_state(sandbox: ScenarioSandbox, state: dict[str, Any]) -> None:
    state["daemon_state"] = _json_safe(await core.getTaskSupervisorDaemonState({"instanceId": HARNESS_DAEMON_INSTANCE}))
    state["persisted_daemon_events"] = _json_safe(await core.load_daemon_events(HARNESS_DAEMON_INSTANCE))
    state["task_records"] = _json_safe(await core.list_task_records())
    state["health_checks"] = _json_safe(await core.list_health_check_tasks())
    session = state.get("session")
    state["monitor_records"] = _json_safe(await core.list_session_monitors(session["id"])) if session and session.get("id") else []
    task_ids = set((state.get("task_aliases") or {}).values())
    task_ids.update(record.get("id") for record in state["task_records"] if record.get("id"))
    metadata = [meta for meta in [await core.read_task_meta(task_id) for task_id in sorted(task_ids)] if meta]
    state["task_metadata"] = _json_safe(sorted(metadata, key=_metadata_sort_key))


async def close_task_driver(state: dict[str, Any]) -> None:
    await _unsubscribe_all(state)
    await core.stopTaskSupervisorDaemon({"instanceId": HARNESS_DAEMON_INSTANCE})
    for proc in list(state.get("owned_processes") or []):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


async def ensure_task_client(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("daemon_client"):
        return state["daemon_client"]
    await ensure_daemon(data, sandbox, state)
    return state["daemon_client"]


async def _unsubscribe_all(state: dict[str, Any]) -> None:
    for unsubscribe in list(state.get("task_unsubscribers") or []):
        try:
            unsubscribe()
        except Exception:
            pass
    state["task_unsubscribers"] = []


def _command_from_data(data: dict[str, Any], sandbox: ScenarioSandbox) -> str:
    if data.get("command"):
        return str(data["command"])
    script = data.get("script", "print('background ok')")
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _isolated_task_environment(sandbox: ScenarioSandbox) -> dict[str, str]:
    captured = {
        "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL",
        "TEMP", "TERM", "TMP", "TMPDIR", "TZ", "USER",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in captured and not key.startswith("LC_")
    }
    task_tmp = sandbox.home / "tmp"
    task_tmp.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "HOME": str(sandbox.home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LOGNAME": "agent-core",
            "PATH": os.defpath,
            "SHELL": "/bin/sh",
            "TERM": "dumb",
            "TMPDIR": str(task_tmp),
            "TZ": "UTC",
            "USER": "agent-core",
        }
    )
    return environment


def _health_recovery_command(sandbox: ScenarioSandbox, counter_file: str) -> str:
    counter = Path(counter_file)
    if counter.is_absolute() or ".." in counter.parts:
        raise ScenarioAssertionError(
            "health recovery counter must stay within the scenario workspace",
            category="driver",
        )
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        p = Path({str(counter)!r})
        n = int(p.read_text()) if p.exists() else 0
        p.write_text(str(n + 1))
        print(f"health-attempt-{{n + 1}}")
        raise SystemExit(1 if n < 2 else 0)
        """
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _remember_task(state: dict[str, Any], data: dict[str, Any], task_id: str) -> None:
    aliases = state.setdefault("task_aliases", {})
    aliases[data.get("alias") or "last"] = task_id
    aliases[task_id] = task_id


def _task_id(data: dict[str, Any], state: dict[str, Any]) -> str:
    value = data.get("taskId") or data.get("task_id") or data.get("alias") or "last"
    aliases = state.get("task_aliases") or {}
    return aliases.get(value, value)


def _find_by_id(records: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    return next((record for record in records if record.get("id") == task_id), None)


def _find_by_task_id(records: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    return next((record for record in records if record.get("taskId") == task_id or record.get("task_id") == task_id), None)


def _metadata_sort_key(meta: dict[str, Any]) -> tuple[str, str, str]:
    return (str(meta.get("type") or ""), str(meta.get("title") or meta.get("command") or ""), str(meta.get("taskId") or meta.get("id") or ""))


def _assert_fields(record: dict[str, Any], data: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if key in data and record.get(key) != data[key]:
            raise ScenarioAssertionError(f"task {key} expected {data[key]!r}, got {record.get(key)!r}")


def _dedupe(values: list[Any]) -> list[Any]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        return repr(value)
