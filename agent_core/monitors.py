from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from ._utils import now_iso
from .storage import listSessionMonitors, readTaskMeta, saveMonitor, saveSession, taskOutputPath, writeTaskMeta


MONITOR_TICK_MS = 1500
DEFAULT_CHECK_INTERVAL_MS = 5000
DEFAULT_HEARTBEAT_INTERVAL_MS = 60000
MIN_NARRATION_SPACING_MS = 20000
MIN_PROGRESS_BYTES = 120
MAX_OUTPUT_EXCERPT_CHARS = 6000


async def createMonitorRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_ref = args.get("session") or {}
    config_ref = args.get("config") or {}
    emit: Callable[[str, dict[str, Any]], Any] = args.get("emit") or (lambda *_: None)
    tick_interval = int(args.get("tickIntervalMs") or MONITOR_TICK_MS) / 1000
    closed = False
    foreground_busy = False
    tick_in_flight = False
    snapshot = _empty_snapshot()

    async def tick() -> None:
        nonlocal snapshot, tick_in_flight
        if closed or tick_in_flight or not session_ref.get("id"):
            return
        tick_in_flight = True
        try:
            monitors = await listSessionMonitors(session_ref["id"])
            now_ms = _now_ms()
            next_snapshot = _compute_snapshot(monitors, now_ms)
            if next_snapshot != snapshot:
                snapshot = next_snapshot
                emit("monitor.snapshot", _monitor_snapshot_payload(session_ref, snapshot))
            for monitor in monitors:
                if closed:
                    return
                if _should_check_monitor(monitor, now_ms):
                    await process_monitor(monitor, now_ms)
        finally:
            tick_in_flight = False

    async def process_monitor(monitor: dict[str, Any], now_ms: int) -> None:
        nonlocal snapshot
        task = await _load_task_record(monitor.get("taskId"))
        output_size = _get_output_size(monitor.get("taskId"))
        observed_task_status = (task or {}).get("status") or "unknown"
        final_status = _derive_monitor_status(task, monitor)
        task_transitioned = observed_task_status != monitor.get("lastObservedTaskStatus")
        output_changed = output_size != int(monitor.get("lastObservedOutputSize") or 0)
        reason = _monitor_update_reason(monitor, final_status, output_changed, output_size, now_ms)
        now = now_iso()
        next_monitor = {
            **monitor,
            "updatedAt": now,
            "status": final_status,
            "lastCheckAt": now,
            "lastObservedTaskStatus": observed_task_status,
            "lastObservedOutputSize": output_size,
            "muted": monitor.get("muted"),
        }
        if final_status in {"completed", "failed", "stopped"}:
            next_monitor["completedAt"] = monitor.get("completedAt") or now
        if final_status in {"failed", "stopped"}:
            next_monitor["failureReason"] = (task or {}).get("error") or monitor.get("failureReason")
        await saveMonitor(next_monitor)
        _sync_session_monitor_metadata(session_ref, await listSessionMonitors(session_ref["id"]))

        if task_transitioned or final_status != monitor.get("status"):
            emit(
                "monitor.status_changed",
                {
                    "session_id": session_ref.get("id"),
                    "monitor_id": monitor.get("id"),
                    "task_id": monitor.get("taskId"),
                    "status": final_status,
                    "task_status": observed_task_status,
                    "exit_code": (task or {}).get("exitCode"),
                },
            )

        if not reason or next_monitor.get("muted") or foreground_busy:
            return
        if reason in {"progress", "heartbeat"}:
            elapsed = now_ms - _parse_iso_ms(next_monitor.get("lastAssistantUpdateAt") or next_monitor.get("createdAt"))
            if elapsed < MIN_NARRATION_SPACING_MS:
                return
        excerpt = _read_output_excerpt(monitor.get("taskId"), int(next_monitor.get("lastNarratedOutputSize") or 0), output_size, MAX_OUTPUT_EXCERPT_CHARS)
        text = _build_deterministic_monitor_update(next_monitor, task, reason, excerpt["text"], excerpt["omittedBytes"])
        if not text:
            return
        _append_assistant_monitor_message(session_ref, text)
        update_time = now_iso()
        next_monitor.update(
            {
                "lastAssistantUpdateAt": update_time,
                "lastAssistantUpdateSummary": text,
                "lastNarratedOutputSize": output_size,
            }
        )
        if reason == "heartbeat":
            next_monitor["lastHeartbeatAt"] = update_time
        await saveMonitor(next_monitor)
        _sync_session_monitor_metadata(session_ref, await listSessionMonitors(session_ref["id"]))
        with _suppress():
            await saveSession(session_ref)
        emit(
            "monitor.assistant_update",
            {
                "session_id": session_ref.get("id"),
                "monitor_id": next_monitor.get("id"),
                "task_id": next_monitor.get("taskId"),
                "status": next_monitor.get("status"),
                "reason": reason,
                "text": text,
            },
        )
        snapshot = _compute_snapshot(await listSessionMonitors(session_ref["id"]), _now_ms())
        emit("monitor.snapshot", _monitor_snapshot_payload(session_ref, snapshot))

    async def run_loop() -> None:
        while not closed:
            await tick()
            await asyncio.sleep(tick_interval)

    loop_task = asyncio.create_task(run_loop())
    await tick()

    async def close() -> None:
        nonlocal closed
        closed = True
        with _suppress():
            loop_task.cancel()
        with _suppress():
            if not loop_task.get_loop().is_closed():
                await asyncio.gather(loop_task, return_exceptions=True)

    def set_foreground_busy(busy: bool) -> None:
        nonlocal foreground_busy
        foreground_busy = busy

    def set_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        nonlocal session_ref, config_ref
        session_ref = session
        config_ref = config or config_ref

    return {"close": close, "setForegroundBusy": set_foreground_busy, "getSnapshot": lambda: snapshot, "setSession": set_session}


def defaultMonitorCheckIntervalMs(value: Any) -> int:
    if not isinstance(value, (int, float)) or value != value:
        return DEFAULT_CHECK_INTERVAL_MS
    return max(2000, min(round(value), 60000))


def defaultMonitorHeartbeatIntervalMs(value: Any) -> int:
    if not isinstance(value, (int, float)) or value != value:
        return DEFAULT_HEARTBEAT_INTERVAL_MS
    return max(15000, min(round(value), 15 * 60_000))


def createMonitorRecord(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session = args.get("session") if isinstance(args.get("session"), dict) else {}
    now = now_iso()
    return {
        "id": args.get("id") or str(uuid.uuid4()),
        "sessionId": args.get("sessionId") or session.get("id"),
        "taskId": args["taskId"],
        "createdAt": now,
        "updatedAt": now,
        "status": "active",
        "goal": args.get("goal", ""),
        "summaryStyle": args.get("summaryStyle"),
        "checkIntervalMs": defaultMonitorCheckIntervalMs(args.get("checkIntervalMs")),
        "heartbeatIntervalMs": defaultMonitorHeartbeatIntervalMs(args.get("heartbeatIntervalMs")),
        "lastNarratedOutputSize": 0,
        "lastObservedOutputSize": 0,
    }


def formatMonitorSummary(monitors: list[dict[str, Any]]) -> str:
    if not monitors:
        return "No monitors."
    lines = []
    for monitor in monitors:
        parts = [f"{str(monitor.get('id') or '')[:8]}  {monitor.get('status')}", f"task {str(monitor.get('taskId') or '')[:8]}", str(monitor.get("goal") or "")]
        if monitor.get("lastAssistantUpdateAt"):
            parts.append(f"last update {monitor.get('lastAssistantUpdateAt')}")
        lines.append(" | ".join(part for part in parts if part))
    return "\n".join(lines)


def _empty_snapshot() -> dict[str, Any]:
    return {"total": 0, "active": 0, "failed": 0, "stopped": 0, "muted": 0, "labels": []}


def _monitor_snapshot_payload(session: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("id"),
        "total": snapshot.get("total"),
        "active": snapshot.get("active"),
        "failed": snapshot.get("failed"),
        "stopped": snapshot.get("stopped"),
        "muted": snapshot.get("muted"),
        "next_check_in_ms": snapshot.get("nextCheckInMs"),
        "labels": snapshot.get("labels") or [],
    }


def _should_check_monitor(monitor: dict[str, Any], now_ms: int) -> bool:
    if monitor.get("status") not in {"active", "muted"}:
        return False
    return now_ms - _parse_iso_ms(monitor.get("lastCheckAt")) >= int(monitor.get("checkIntervalMs") or DEFAULT_CHECK_INTERVAL_MS)


async def _load_task_record(task_id: Any) -> dict[str, Any] | None:
    if not isinstance(task_id, str) or not task_id:
        return None
    task = await readTaskMeta(task_id)
    if not task:
        return None
    if task.get("status") == "running" and isinstance(task.get("pid"), int) and not _is_process_alive(task["pid"]):
        now = now_iso()
        reconciled = {**task, "taskId": task_id, "updatedAt": now, "finishedAt": task.get("finishedAt") or now, "status": "completed" if task.get("exitCode") == 0 else "failed" if task.get("exitCode") is not None else "unknown"}
        await writeTaskMeta(task_id, reconciled)
        return reconciled
    return task


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _derive_monitor_status(task: dict[str, Any] | None, monitor: dict[str, Any]) -> str:
    if monitor.get("status") in {"stopped", "muted"}:
        return monitor["status"]
    task_status = (task or {}).get("status") or monitor.get("lastObservedTaskStatus") or "unknown"
    if task_status == "completed":
        return "completed"
    if task_status in {"failed", "unknown"}:
        return "failed"
    if task_status in {"terminated", "stopped"}:
        return "stopped"
    return "active"


def _get_output_size(task_id: Any) -> int:
    if not isinstance(task_id, str) or not task_id:
        return 0
    path = Path(taskOutputPath(task_id))
    return path.stat().st_size if path.exists() else 0


def _read_output_excerpt(task_id: Any, from_byte: int, to_byte: int, max_chars: int) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id:
        return {"text": "", "omittedBytes": 0}
    path = Path(taskOutputPath(task_id))
    if not path.exists():
        return {"text": "", "omittedBytes": 0}
    raw = path.read_text(encoding="utf-8", errors="replace")
    sliced = raw[from_byte:to_byte]
    if len(sliced) <= max_chars:
        return {"text": sliced.strip(), "omittedBytes": 0}
    trimmed = sliced[-max_chars:]
    return {"text": trimmed.strip(), "omittedBytes": len(sliced) - len(trimmed)}


def _monitor_update_reason(monitor: dict[str, Any], final_status: str, output_changed: bool, output_size: int, now_ms: int) -> str | None:
    if not monitor.get("lastAssistantUpdateAt"):
        return "start"
    if final_status not in {"active", monitor.get("status")}:
        return final_status if final_status in {"completed", "stopped", "failed"} else None
    progress_bytes = max(0, output_size - int(monitor.get("lastNarratedOutputSize") or 0))
    if output_changed and progress_bytes >= MIN_PROGRESS_BYTES:
        return "progress"
    if now_ms - _parse_iso_ms(monitor.get("lastAssistantUpdateAt") or monitor.get("createdAt")) >= int(monitor.get("heartbeatIntervalMs") or DEFAULT_HEARTBEAT_INTERVAL_MS):
        return "heartbeat"
    return None


def _build_deterministic_monitor_update(monitor: dict[str, Any], task: dict[str, Any] | None, reason: str, output_excerpt: str, omitted_bytes: int) -> str:
    label = monitor.get("goal") or f"task {str(monitor.get('taskId') or '')[:8]}"
    if reason == "start":
        return f"I started monitoring {label}. I will keep watching the background task and update you when something meaningful changes."
    if reason == "completed":
        suffix = f" with exit code {task.get('exitCode')}" if task and isinstance(task.get("exitCode"), int) else ""
        return f"The background task for {label} completed{suffix}."
    if reason == "failed":
        suffix = f" with exit code {task.get('exitCode')}" if task and isinstance(task.get("exitCode"), int) else ""
        error = f": {task.get('error')}" if task and task.get("error") else "."
        return f"The background task for {label} failed{suffix}{error}"
    if reason == "stopped":
        return f"The background task for {label} stopped before completion."
    if reason == "heartbeat":
        return f"I am still monitoring {label}. There has not been a major new change since my last update."
    excerpt = f" Latest output:\n{_render_excerpt(output_excerpt, omitted_bytes)}" if output_excerpt else ""
    return f"There is new background activity for {label}.{excerpt}"


def _render_excerpt(text: str, omitted_bytes: int) -> str:
    prefix = f"[omitted {omitted_bytes} earlier chars]\n" if omitted_bytes > 0 else ""
    return f"{prefix}{text}".strip()


def _append_assistant_monitor_message(session: dict[str, Any], text: str) -> None:
    trimmed = text.strip()
    if not trimmed:
        return
    messages = session.setdefault("messages", [])
    last = messages[-1] if messages else None
    if isinstance(last, dict) and last.get("role") == "assistant" and str(last.get("content") or "").strip() == trimmed:
        return
    messages.append({"role": "assistant", "content": trimmed})


def _sync_session_monitor_metadata(session: dict[str, Any], monitors: list[dict[str, Any]]) -> None:
    session.setdefault("metadata", {})["monitorIds"] = [monitor.get("id") for monitor in monitors if monitor.get("id")]


def _compute_snapshot(monitors: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    active = [monitor for monitor in monitors if monitor.get("status") == "active"]
    next_check = None
    if active:
        next_check = _bucket_ms(max(0, min(_parse_iso_ms(monitor.get("lastCheckAt")) + int(monitor.get("checkIntervalMs") or DEFAULT_CHECK_INTERVAL_MS) - now_ms for monitor in active)))
    return {
        "total": len(monitors),
        "active": len(active),
        "failed": sum(1 for monitor in monitors if monitor.get("status") == "failed"),
        "stopped": sum(1 for monitor in monitors if monitor.get("status") == "stopped"),
        "muted": sum(1 for monitor in monitors if monitor.get("status") == "muted"),
        "nextCheckInMs": next_check,
        "labels": [_summarize_goal(str(monitor.get("goal") or "")) for monitor in active[:3]],
    }


def _summarize_goal(goal: str) -> str:
    trimmed = goal.strip()
    return trimmed if len(trimmed) <= 48 else f"{trimmed[:45].rstrip()}..."


def _bucket_ms(value: float) -> int:
    import math

    if value <= 5000:
        return int(math.ceil(value / 1000) * 1000)
    return int(math.ceil(value / 5000) * 5000)


def _parse_iso_ms(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    import datetime

    try:
        return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


class _suppress:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return True
