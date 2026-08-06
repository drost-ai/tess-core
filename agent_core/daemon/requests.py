from __future__ import annotations

import asyncio
from typing import Any

from ..storage import loadDaemonState


async def _handle_daemon_request(
    supervisor: dict[str, Any],
    shutdown: asyncio.Event,
    request: dict[str, Any],
    signal: Any = None,
) -> dict[str, Any]:
    _throw_if_daemon_request_aborted(signal)
    method = _normalise_daemon_method(request.get("method"))
    params = request.get("params") or {}
    result: Any
    if method == "getState":
        result = supervisor["getState"]()
    elif method == "getStatus":
        result = await loadDaemonState()
    elif method == "getBootstrap":
        result = await supervisor["getBootstrap"](params.get("sessionId"))
    elif method == "listTasks":
        result = await supervisor["listTasks"](params.get("sessionId"))
    elif method == "inspectTask":
        result = await supervisor["inspectTask"](params.get("taskId"))
    elif method == "readTaskOutput":
        result = await supervisor["readTaskOutput"](
            params.get("taskId"),
            params.get("tailBytes"),
        )
    elif method == "stopTask":
        result = await supervisor["stopTask"](params.get("taskId"))
    elif method == "retryTask":
        result = await supervisor["retryTask"](params.get("taskId"))
    elif method == "startBackgroundCommand":
        result = await supervisor["startBackgroundCommand"](params)
    elif method == "startMainSessionTask":
        result = await supervisor["startMainSessionTask"](params)
    elif method == "startSubagentTask":
        result = await supervisor["startSubagentTask"](params)
    elif method == "startHealthCheck":
        result = await supervisor["startHealthCheck"](params)
    elif method == "shutdown":
        return {
            "result": {"status": "shutting_down"},
            "afterWrite": shutdown.set,
        }
    else:
        raise RuntimeError(f"Unknown daemon method: {method}")
    _throw_if_daemon_request_aborted(signal)
    return {"result": result}


def _throw_if_daemon_request_aborted(signal: Any) -> None:
    if not signal or not getattr(signal, "aborted", False):
        return
    reason = getattr(signal, "reason", None)
    raise reason if isinstance(reason, BaseException) else RuntimeError("Daemon request aborted.")


def _normalise_daemon_method(method: Any) -> str | None:
    aliases = {
        "daemon.status": "getState",
        "daemon.getStatus": "getStatus",
        "daemon.getState": "getState",
        "daemon.getBootstrap": "getBootstrap",
        "daemon.listTasks": "listTasks",
        "daemon.inspectTask": "inspectTask",
        "daemon.readTaskOutput": "readTaskOutput",
        "daemon.stopTask": "stopTask",
        "daemon.retryTask": "retryTask",
        "daemon.startBackgroundCommand": "startBackgroundCommand",
        "daemon.startMainSessionTask": "startMainSessionTask",
        "daemon.startSubagentTask": "startSubagentTask",
        "daemon.startHealthCheck": "startHealthCheck",
        "daemon.subscribe": "subscribe",
        "daemon.shutdown": "shutdown",
        "tasks.list": "listTasks",
        "tasks.inspect": "inspectTask",
        "tasks.read_output": "readTaskOutput",
        "tasks.stop": "stopTask",
        "tasks.retry": "retryTask",
        "tasks.start_background_command": "startBackgroundCommand",
        "tasks.start_main_session": "startMainSessionTask",
        "tasks.start_subagent": "startSubagentTask",
        "tasks.start_health_check": "startHealthCheck",
    }
    return aliases.get(method, method if isinstance(method, str) else None)
