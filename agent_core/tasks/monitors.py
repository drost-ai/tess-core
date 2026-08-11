from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knobValue

    return knobValue(name)


from .._utils import is_process_alive as _is_process_alive

from .._utils import parse_iso_ms

import asyncio
import math
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from .._utils import now_iso
from .monitor_bootstrap import _MonitorBootstrapBaseline
from .monitor_narration import _author_monitor_update
from .monitor_observation import (
    buildMonitorNarrationMutation as _build_monitor_narration_mutation,
    buildMonitorObservationMutation as _build_monitor_observation_mutation,
)
from .monitor_transactions import (
    commitMonitorMutation as _commit_monitor_mutation,
    monitorTransitionEventFields as _monitor_transition_event_fields,
)
from ..session.persistence import persistSession
from ..storage import list_session_monitors, mutate_task_meta, read_task_meta, save_monitor, task_output_path, write_task_meta



async def createMonitorRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_ref = args.get("session") or {}
    config_ref = args.get("config") or {}
    emit: Callable[[str, dict[str, Any]], Any] = args.get("emit") or (lambda *_: None)
    tick_interval = int(args.get("tickIntervalMs") or int(_knob("tasks.monitor_tick_ms"))) / 1000
    closed = False
    foreground_busy = False
    tick_in_flight = False
    snapshot = _empty_snapshot()
    bootstrap_baseline = _MonitorBootstrapBaseline()
    stop_event = asyncio.Event()

    async def tick() -> None:
        nonlocal snapshot, tick_in_flight
        if closed or tick_in_flight or not session_ref.get("id"):
            return
        tick_in_flight = True
        try:
            monitors = await list_session_monitors(session_ref["id"])
            is_bootstrap_tick = bootstrap_baseline.begin(monitors)
            now_ms = _now_ms()
            next_snapshot = _compute_snapshot(monitors, now_ms)
            if next_snapshot != snapshot:
                snapshot = next_snapshot
                emit("monitor.snapshot", _monitor_snapshot_payload(session_ref, snapshot))
            for monitor in monitors:
                if closed:
                    return
                if _should_check_monitor(monitor, now_ms):
                    await process_monitor(monitor, now_ms, is_bootstrap_tick and bootstrap_baseline.includes(monitor.get("id")))
        finally:
            tick_in_flight = False

    async def process_monitor(monitor: dict[str, Any], now_ms: int, bootstrap_observation: bool = False) -> None:
        nonlocal snapshot
        task = await _load_task_record(monitor.get("taskId"))
        output_size = _get_output_size(monitor.get("taskId"))
        monitor_id = monitor.get("id") if isinstance(monitor.get("id"), str) else ""
        observed = await _commit_monitor_mutation(
            monitorId=monitor_id,
            expectedSessionId=session_ref["id"],
            mutate=lambda latest: _build_monitor_observation_mutation(
                latest, task, output_size, now_ms
            ),
        )
        next_monitor = observed["current"]
        reason = observed["value"].get("reason")
        observation = (
            next_monitor.get("status"),
            output_size,
            next_monitor.get("lastObservedTaskStatus"),
        )
        reason = bootstrap_baseline.resolve_reason(
            monitor, observation, reason, bootstrap_observation
        )
        _sync_session_monitor_metadata(session_ref, await list_session_monitors(session_ref["id"]))

        if observed.get("transitionId"):
            emit(
                "monitor.status_changed",
                {
                    "session_id": session_ref.get("id"),
                    "monitor_id": next_monitor.get("id"),
                    "task_id": next_monitor.get("taskId"),
                    "status": next_monitor.get("status"),
                    "previous_status": observed["previous"].get("status"),
                    "changed": observed.get("statusChanged", False),
                    "task_status": next_monitor.get("lastObservedTaskStatus"),
                    "exit_code": (task or {}).get("exitCode"),
                    **_monitor_transition_event_fields(observed),
                },
            )

        if not reason or next_monitor.get("muted") or foreground_busy:
            return
        if reason in {"progress", "heartbeat"}:
            elapsed = now_ms - int(parse_iso_ms(next_monitor.get("lastAssistantUpdateAt") or next_monitor.get("createdAt")) or 0)
            if elapsed < int(_knob("tasks.monitor_narration_spacing_ms")):
                return
        excerpt = _read_output_excerpt(monitor.get("taskId"), int(next_monitor.get("lastNarratedOutputSize") or 0), output_size, int(_knob("tasks.monitor_output_excerpt_chars")))
        text = await _author_monitor_update(config_ref, session_ref, next_monitor, task, reason, excerpt["text"], excerpt["omittedBytes"])
        if not text:
            return
        update_time = now_iso()
        narration = await _commit_monitor_mutation(
            monitorId=monitor_id,
            expectedSessionId=session_ref["id"],
            mutate=lambda latest: _build_monitor_narration_mutation(
                latest,
                observed["revision"],
                update_time,
                text,
                output_size,
                reason,
            ),
        )
        if not narration["value"].get("accepted"):
            return
        next_monitor = narration["current"]
        _append_assistant_monitor_message(session_ref, text)
        bootstrap_baseline.narrated(monitor_id)
        _sync_session_monitor_metadata(session_ref, await list_session_monitors(session_ref["id"]))
        with _suppress():
            await persistSession(session_ref, config_ref)
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
        snapshot = _compute_snapshot(await list_session_monitors(session_ref["id"]), _now_ms())
        emit("monitor.snapshot", _monitor_snapshot_payload(session_ref, snapshot))

    async def run_loop() -> None:
        while not closed:
            await tick()
            if not closed:
                with _suppress():
                    await asyncio.wait_for(stop_event.wait(), timeout=tick_interval)

    loop_task = asyncio.create_task(run_loop())
    await tick()

    async def close() -> None:
        nonlocal closed
        closed = True
        stop_event.set()
        with _suppress():
            if not loop_task.get_loop().is_closed():
                await asyncio.gather(loop_task, return_exceptions=True)

    def set_foreground_busy(busy: bool) -> None:
        nonlocal foreground_busy
        foreground_busy = busy
        if not busy:
            _schedule_tick()

    def set_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        nonlocal session_ref, config_ref, snapshot
        session_ref = session
        config_ref = config or config_ref
        snapshot = _empty_snapshot()
        bootstrap_baseline.reset()
        _schedule_tick()

    def _schedule_tick() -> None:
        with _suppress():
            loop = loop_task.get_loop()
            if loop.is_running() and not loop.is_closed():
                loop.call_soon_threadsafe(lambda: asyncio.create_task(tick()))

    return {"close": close, "setForegroundBusy": set_foreground_busy, "getSnapshot": lambda: snapshot, "setSession": set_session}


def defaultMonitorCheckIntervalMs(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return int(_knob("tasks.monitor_check_interval_ms"))
    return max(2000, min(round(value), 60000))


def defaultMonitorHeartbeatIntervalMs(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return int(_knob("tasks.monitor_heartbeat_interval_ms"))
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
        lines.append(" · ".join(part for part in parts if part))
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
    return now_ms - int(parse_iso_ms(monitor.get("lastCheckAt")) or 0) >= int(monitor.get("checkIntervalMs") or int(_knob("tasks.monitor_check_interval_ms")))


async def _load_task_record(task_id: Any) -> dict[str, Any] | None:
    if not isinstance(task_id, str) or not task_id:
        return None
    task = await read_task_meta(task_id)
    if not task:
        return None
    if task.get("status") == "running" and isinstance(task.get("pid"), int) and not _is_process_alive(task["pid"]):
        now = now_iso()

        def _reconcile_dead_pid(current: dict[str, Any] | None) -> dict[str, Any]:
            base = current or task
            return {
                **base,
                "taskId": task_id,
                "updatedAt": now,
                "finishedAt": base.get("finishedAt") or now,
                "status": "completed" if base.get("exitCode") == 0 else "failed" if base.get("exitCode") is not None else "unknown",
            }

        return await mutate_task_meta(task_id, _reconcile_dead_pid, fallback=task)
    return task




def _get_output_size(task_id: Any) -> int:
    if not isinstance(task_id, str) or not task_id:
        return 0
    path = Path(task_output_path(task_id))
    return path.stat().st_size if path.exists() else 0


def _read_output_excerpt(task_id: Any, from_byte: int, to_byte: int, max_chars: int) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id:
        return {"text": "", "omittedBytes": 0}
    path = Path(task_output_path(task_id))
    if not path.exists():
        return {"text": "", "omittedBytes": 0}
    raw = path.read_text(encoding="utf-8", errors="replace")
    sliced = raw[from_byte:to_byte]
    if len(sliced) <= max_chars:
        return {"text": sliced.strip(), "omittedBytes": 0}
    trimmed = sliced[-max_chars:]
    return {"text": trimmed.strip(), "omittedBytes": len(sliced) - len(trimmed)}


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
        next_check = _bucket_ms(max(0, min(int(parse_iso_ms(monitor.get("lastCheckAt")) or 0) + int(monitor.get("checkIntervalMs") or int(_knob("tasks.monitor_check_interval_ms"))) - now_ms for monitor in active)))
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




def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


class _suppress:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, _exc, _traceback):
        # Operational failures are best-effort here, but cancellation is a
        # lifecycle signal and must reach the owning loop. Swallowing
        # CancelledError leaves run_loop alive while asyncio.run() drains.
        return bool(exc_type is not None and issubclass(exc_type, Exception))
