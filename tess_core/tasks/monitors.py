from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


from .._utils import is_process_alive as _is_process_alive

from .._utils import parse_iso_ms

import asyncio
import math
import uuid
from pathlib import Path
from typing import Any

from .._utils import now_iso
from ..contracts.monitor_records import MonitorRecord
from .monitor_delivery_state import initial_delivery_state
from ..storage import list_session_monitors, mutate_task_meta, read_task_meta, task_output_path



async def create_monitor_runtime(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Local observer. Durable delivery is consumed by an explicit host.

    Daemon-on and daemon-off share the same transaction path. Never author into
    a mutable foreground session from an independent ticker.
    """
    from .supervisor_parts.monitor_watch import tick_daemon_monitors
    from ..runtime.paths import get_active_runtime_paths, RuntimePathsScope
    import logging

    args = dict(args or {})
    session_ref = args.get("session") or {}
    paths = args.get("runtimePaths") or get_active_runtime_paths()
    emit = args.get("emit") or (lambda *_: None)
    stop_event = asyncio.Event()
    snapshot = _empty_snapshot()
    interval = max(10, int(args.get("tickIntervalMs") or _knob("tasks.monitor_tick_ms"))) / 1000

    async def tick():
        nonlocal snapshot
        if not session_ref.get("id"):
            return
        with RuntimePathsScope(paths):
            await tick_daemon_monitors(lambda event, data, _session=None: emit(event, data), session_id=session_ref["id"])
            monitors = await list_session_monitors(session_ref["id"])
            if monitors or "monitorIds" in session_ref.get("metadata", {}):
                _sync_session_monitor_metadata(session_ref, monitors)
            snapshot = _compute_snapshot(monitors, _now_ms())
            emit("monitor.snapshot", _monitor_snapshot_payload(session_ref, snapshot))

    async def run_loop():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), interval)
            except TimeoutError:
                try:
                    await tick()
                except Exception:
                    logging.getLogger(__name__).exception("Monitor observation failed")

    await tick()
    loop_task = asyncio.create_task(run_loop())

    async def close():
        stop_event.set()
        if loop_task.get_loop() is not asyncio.get_running_loop():
            # Closed from another event loop than the one that started it (a
            # host that builds and closes in separate asyncio.run calls). The
            # task cannot be awaited here; Python 3.14 refuses outright.
            if not loop_task.get_loop().is_closed():
                loop_task.get_loop().call_soon_threadsafe(loop_task.cancel)
            return
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    def set_session(session, config=None):
        nonlocal session_ref
        session_ref = session

    return {"close": close, "setForegroundBusy": lambda _busy: None,
            "getSnapshot": lambda: snapshot, "setSession": set_session}


def default_monitor_check_interval_ms(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return int(_knob("tasks.monitor_check_interval_ms"))
    return max(2000, min(round(value), 60000))


def default_monitor_heartbeat_interval_ms(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return int(_knob("tasks.monitor_heartbeat_interval_ms"))
    return max(15000, min(round(value), 15 * 60_000))


def create_monitor_record(fields: dict[str, Any]) -> MonitorRecord:
    """Build one monitor record from record-shaped fields (wire-key input)."""
    args = fields
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
        "checkIntervalMs": default_monitor_check_interval_ms(args.get("checkIntervalMs")),
        "heartbeatIntervalMs": default_monitor_heartbeat_interval_ms(args.get("heartbeatIntervalMs")),
        "lastNarratedOutputSize": 0,
        "lastObservedOutputSize": 0,
        "delivery": initial_delivery_state(args.get("deliveryMode", "notification")),
    }


def format_monitor_summary(monitors: list[dict[str, Any]]) -> str:
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
