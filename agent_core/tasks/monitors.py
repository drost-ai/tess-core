from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from .._utils import now_iso
from ..config import ensureProviderForModelSelection, normalizeEffortForModel
from .monitor_bootstrap import _MonitorBootstrapBaseline
from .monitor_observation import (
    buildMonitorNarrationMutation as _build_monitor_narration_mutation,
    buildMonitorObservationMutation as _build_monitor_observation_mutation,
)
from .monitor_transactions import (
    commitMonitorMutation as _commit_monitor_mutation,
    monitorTransitionEventFields as _monitor_transition_event_fields,
)
from ..providers.anthropic import sendAnthropicMessagesRequest
from ..providers.openai import sendOpenAiRequest
from ..runtime.secret_provider import resolveProviderConfigSecrets
from ..session.persistence import persistSession
from ..storage import listSessionMonitors, mutateTaskMeta, readTaskMeta, saveMonitor, taskOutputPath, writeTaskMeta
from ..providers.usage_costs import mergeUsageIntoSession as _merge_usage_into_session

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
    bootstrap_baseline = _MonitorBootstrapBaseline()
    stop_event = asyncio.Event()

    async def tick() -> None:
        nonlocal snapshot, tick_in_flight
        if closed or tick_in_flight or not session_ref.get("id"):
            return
        tick_in_flight = True
        try:
            monitors = await listSessionMonitors(session_ref["id"])
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
        _sync_session_monitor_metadata(session_ref, await listSessionMonitors(session_ref["id"]))

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
            elapsed = now_ms - _parse_iso_ms(next_monitor.get("lastAssistantUpdateAt") or next_monitor.get("createdAt"))
            if elapsed < MIN_NARRATION_SPACING_MS:
                return
        excerpt = _read_output_excerpt(monitor.get("taskId"), int(next_monitor.get("lastNarratedOutputSize") or 0), output_size, MAX_OUTPUT_EXCERPT_CHARS)
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
        _sync_session_monitor_metadata(session_ref, await listSessionMonitors(session_ref["id"]))
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
        snapshot = _compute_snapshot(await listSessionMonitors(session_ref["id"]), _now_ms())
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
        return DEFAULT_CHECK_INTERVAL_MS
    return max(2000, min(round(value), 60000))


def defaultMonitorHeartbeatIntervalMs(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
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
    return now_ms - _parse_iso_ms(monitor.get("lastCheckAt")) >= int(monitor.get("checkIntervalMs") or DEFAULT_CHECK_INTERVAL_MS)


async def _load_task_record(task_id: Any) -> dict[str, Any] | None:
    if not isinstance(task_id, str) or not task_id:
        return None
    task = await readTaskMeta(task_id)
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

        return await mutateTaskMeta(task_id, _reconcile_dead_pid, fallback=task)
    return task


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


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


async def _author_monitor_update(config: dict[str, Any], session: dict[str, Any], monitor: dict[str, Any], task: dict[str, Any] | None, reason: str, output_excerpt: str, omitted_bytes: int) -> str:
    try:
        return await _author_monitor_update_with_provider(config, session, monitor, task, reason, output_excerpt, omitted_bytes)
    except Exception:
        return _build_deterministic_monitor_update(monitor, task, reason, output_excerpt, omitted_bytes)


async def _author_monitor_update_with_provider(config: dict[str, Any], session: dict[str, Any], monitor: dict[str, Any], task: dict[str, Any] | None, reason: str, output_excerpt: str, omitted_bytes: int) -> str:
    provider = ensureProviderForModelSelection(session.get("provider") or config.get("provider"), session.get("model") or config.get("model"), config.get("provider", "openai"))
    model = session.get("model") or config.get("model")
    request = {
        "model": model,
        "effort": normalizeEffortForModel(provider, model, session.get("effort") or config.get("effort")) if model else None,
        "system": "\n".join(
            [
                "You are continuing an existing agent session.",
                "Write a concise proactive update for the user about a monitored background task.",
                "Default to 1-3 short sentences.",
                "Be concrete, state what changed, and mention completion or failure clearly.",
                "Do not mention tokens, prompts, or that you are summarizing structured data.",
                "Do not ask the user to poll again. You are the one proactively updating them.",
            ]
        ),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "reason": reason,
                        "goal": monitor.get("goal"),
                        "summary_style": monitor.get("summaryStyle"),
                        "task": {
                            "status": (task or {}).get("status") or "unknown",
                            "exit_code": (task or {}).get("exitCode"),
                            "command": (task or {}).get("command"),
                            "cwd": (task or {}).get("cwd"),
                            "finished_at": (task or {}).get("finishedAt"),
                            "error": (task or {}).get("error"),
                        },
                        "monitor": {
                            "status": monitor.get("status"),
                            "created_at": monitor.get("createdAt"),
                            "last_assistant_update_at": monitor.get("lastAssistantUpdateAt"),
                            "last_assistant_update_summary": monitor.get("lastAssistantUpdateSummary"),
                            "omitted_output_bytes": omitted_bytes,
                        },
                        "new_output_excerpt": output_excerpt or "",
                    },
                    indent=2,
                ),
            }
        ],
        "tools": [],
    }
    resolved_config = await resolveProviderConfigSecrets(config)
    if provider == "openai":
        token = resolved_config.get("openaiToken")
        if not token:
            raise RuntimeError("No OpenAI token available for monitor updates.")
        response = await sendOpenAiRequest({"token": token, "baseUrl": resolved_config.get("openaiBaseUrl"), "headers": resolved_config.get("customHeaders"), "mode": resolved_config.get("openaiApiMode"), "request": request, "runtime": {}, "maxAttempts": 1})
    else:
        response = await sendAnthropicMessagesRequest({"request": request, "config": resolved_config, "runtime": {}, "maxAttempts": 1})
    text = "".join(str(block.get("text") or "") for block in response.get("content", []) if block.get("type") == "text").strip()
    if not text:
        raise RuntimeError("Provider returned an empty monitor update.")
    _merge_usage(session, response.get("usage") or {})
    return text


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


def _merge_usage(session: dict[str, Any], usage: dict[str, Any]) -> None:
    _merge_usage_into_session(session, usage)


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

    def __exit__(self, exc_type, _exc, _traceback):
        # Operational failures are best-effort here, but cancellation is a
        # lifecycle signal and must reach the owning loop. Swallowing
        # CancelledError leaves run_loop alive while asyncio.run() drains.
        return bool(exc_type is not None and issubclass(exc_type, Exception))
