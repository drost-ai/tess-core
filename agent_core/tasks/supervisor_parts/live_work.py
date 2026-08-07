from __future__ import annotations

import datetime as _datetime
import json as _json
import os as _os
import time as _time
from typing import Any as _Any

from ..._utils import now_iso as _now_iso
from ...storage import (
    bridgePath as _bridge_path,
    childAgentPath as _child_agent_path,
    healthCheckMetaPath as _health_check_meta_path,
    listChildAgents as _list_child_agents,
    listHealthCheckTasks as _list_health_checks,
    listSessionMonitors as _list_session_monitors,
    loadBridge as _load_bridge,
    loadRemoteSession as _load_remote_session,
    monitorPath as _monitor_path,
    mutateChildAgent as _mutate_child_agent,
    readTaskMeta as _read_task_meta,
    remoteSessionPath as _remote_session_path,
    saveMonitor as _save_monitor,
    taskMetaPath as _task_meta_path,
    writeTaskMeta as _write_task_meta,
)
from .mappers import _is_active_child_agent_status, _is_related_child_agent
from .processes import _is_process_alive


_DEAD_WORK_GRACE_MS = 15 * 60 * 1000
_LIVE_WORK_CACHE_TTL_MS = 30_000
_MAX_VERDICT_DETAILS = 128
# One liveness definition across both reconciliation layers: anything that
# may still own a pid (backgrounded and degraded included) is live until the
# pid is verified dead. Subagents add pre-spawn "queued". Daemon-restart
# reconciliation uses the same flat set.
_ACTIVE_PROCESS_STATUSES = {
    "background_command": {"backgrounded", "running", "reconciling", "degraded"},
    "main_session": {"backgrounded", "running", "reconciling", "degraded"},
    "subagent": {"queued", "backgrounded", "running", "reconciling", "degraded"},
}
_TERMINAL_REMOTE_STATUSES = {"completed", "failed", "closed", "stopped", "terminated", "cancelled"}


def _session_has_long_lived_work(session: dict[str, _Any]) -> bool:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    restore = metadata.get("runtimeRestore") if isinstance(metadata.get("runtimeRestore"), dict) else {}
    return bool(
        metadata.get("taskIds")
        or metadata.get("monitorIds")
        or metadata.get("childAgentIds")
        or metadata.get("childSessionIds")
        or metadata.get("backgroundSessionIds")
        or metadata.get("remoteSessionId")
        or restore.get("activeTasks")
    )


def _is_active_health_check(record: dict[str, _Any]) -> bool:
    if record.get("status") not in {"active", "unhealthy"}:
        return False
    mode = record.get("mode") or ("command" if record.get("command") else "http" if record.get("url") else None)
    if mode == "command":
        return isinstance(record.get("command"), str) and bool(record["command"].strip())
    if mode == "http":
        return isinstance(record.get("url"), str) and bool(record["url"].strip())
    return False


def _is_active_monitor_status(status: _Any) -> bool:
    return status in {"active", "muted"}


async def _resolve_session_live_work(
    session: dict[str, _Any],
    context: dict[str, _Any] | None = None,
    *,
    reconcile: bool = True,
) -> dict[str, _Any]:
    """Verify durable marker families without reading a session archive.

    Marker presence is only a prefilter. Each referenced record must still be
    active, related to this session, and (where available) backed by a live
    process. Dead running-like records converge once after the grace window.
    """

    context = context or {}
    session_id = session.get("id") if isinstance(session.get("id"), str) else ""
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    families: list[str] = []
    reconciled: list[str] = []
    stale: list[str] = []

    def mark_family(name: str) -> None:
        if name not in families:
            families.append(name)

    def mark_reconciled(value: str) -> None:
        if len(reconciled) < _MAX_VERDICT_DETAILS and value not in reconciled:
            reconciled.append(value)

    def mark_stale(value: str) -> None:
        if len(stale) < _MAX_VERDICT_DETAILS and value not in stale:
            stale.append(value)

    task_ids = _normalized_ids(metadata.get("taskIds"))
    for task_id in task_ids:
        try:
            meta = await _read_task_meta(task_id)
        except Exception as exc:
            mark_stale(f"task:{task_id}:unavailable:{type(exc).__name__}")
            continue
        if not isinstance(meta, dict):
            mark_stale(f"task:{task_id}:missing")
            continue
        kind = meta.get("type") if meta.get("type") in _ACTIVE_PROCESS_STATUSES else "background_command"
        if not _task_belongs_to_session(meta, session_id, kind):
            mark_stale(f"task:{task_id}:foreign")
            continue
        next_meta, changed = await _reconcile_dead_task_meta(task_id, meta, kind, apply=reconcile)
        if changed:
            mark_reconciled(f"task:{task_id}")
        if next_meta.get("status") in _ACTIVE_PROCESS_STATUSES[kind]:
            mark_family("subagents" if kind == "subagent" else "tasks")
        else:
            mark_stale(f"task:{task_id}:{next_meta.get('status') or 'inactive'}")

    try:
        child_agents = context.get("childAgents")
        if child_agents is None:
            child_agents = await _list_child_agents()
    except Exception as exc:
        child_agents = []
        mark_stale(f"subagents:unavailable:{type(exc).__name__}")
    for child in child_agents if isinstance(child_agents, list) else []:
        if not isinstance(child, dict) or not _is_related_child_agent(session, child):
            continue
        child_id = child.get("agentId") if isinstance(child.get("agentId"), str) else "unknown"
        next_child, changed = await _reconcile_dead_child_agent(child, apply=reconcile)
        if changed:
            mark_reconciled(f"subagent:{child_id}")
        if _is_active_child_agent_status(next_child.get("status")):
            mark_family("subagents")
        else:
            mark_stale(f"subagent:{child_id}:{next_child.get('status') or 'inactive'}")

    monitor_ids = set(_normalized_ids(metadata.get("monitorIds")))
    if monitor_ids:
        try:
            monitors = context.get("monitors")
            if monitors is None:
                monitors = await _list_session_monitors(session_id)
        except Exception as exc:
            monitors = []
            mark_stale(f"monitors:unavailable:{type(exc).__name__}")
        seen_monitor_ids: set[str] = set()
        for monitor in monitors if isinstance(monitors, list) else []:
            if not isinstance(monitor, dict) or monitor.get("id") not in monitor_ids:
                continue
            monitor_id = str(monitor["id"])
            seen_monitor_ids.add(monitor_id)
            next_monitor, changed = await _reconcile_terminal_monitor(monitor, apply=reconcile)
            if changed:
                mark_reconciled(f"monitor:{monitor_id}")
            if _is_active_monitor_status(next_monitor.get("status")):
                mark_family("monitors")
            else:
                mark_stale(f"monitor:{monitor_id}:{next_monitor.get('status') or 'inactive'}")
        for monitor_id in sorted(monitor_ids - seen_monitor_ids):
            mark_stale(f"monitor:{monitor_id}:missing")

    try:
        health_checks = context.get("healthChecks")
        if health_checks is None:
            health_checks = await _list_health_checks()
    except Exception as exc:
        health_checks = []
        mark_stale(f"health_checks:unavailable:{type(exc).__name__}")
    for check in health_checks if isinstance(health_checks, list) else []:
        if not isinstance(check, dict) or check.get("sessionId") != session_id:
            continue
        check_id = check.get("taskId") if isinstance(check.get("taskId"), str) else "unknown"
        if _is_active_health_check(check):
            mark_family("health_checks")
        else:
            mark_stale(f"health_check:{check_id}:{check.get('status') or 'invalid'}")

    remote_id = metadata.get("remoteSessionId") if isinstance(metadata.get("remoteSessionId"), str) else None
    if remote_id:
        try:
            remote = context.get("remote") if "remote" in context else await _load_remote_session(remote_id)
            bridge_id = metadata.get("bridgeId")
            if isinstance(remote, dict) and isinstance(remote.get("bridgeId"), str):
                bridge_id = remote.get("bridgeId")
            bridge = context.get("bridge") if "bridge" in context else await _load_bridge(bridge_id) if isinstance(bridge_id, str) else None
            if remote is None:
                # Source-compatible opaque marker: superseded by the terminal store; this
                # with generation-aware authority.
                mark_family("remote")
            elif _remote_is_live(remote, bridge):
                mark_family("remote")
            else:
                mark_stale(f"remote:{remote_id}:{remote.get('status') or 'inactive'}")
        except Exception as exc:
            mark_stale(f"remote:{remote_id}:unavailable:{type(exc).__name__}")

    return {
        "live": bool(families),
        "families": families,
        "reconciled": reconciled,
        "stale": stale,
        "checkedAt": _now_iso(),
    }


def _create_live_work_verdict_cache(ttl_ms: int = _LIVE_WORK_CACHE_TTL_MS) -> dict[str, _Any]:
    ttl_ms = max(0, min(int(ttl_ms), _LIVE_WORK_CACHE_TTL_MS))
    entries: dict[str, dict[str, _Any]] = {}
    stats = {"hits": 0, "misses": 0, "invalidations": 0}

    async def resolve(session: dict[str, _Any], context: dict[str, _Any] | None = None) -> dict[str, _Any]:
        session_id = session.get("id") if isinstance(session.get("id"), str) else ""
        fingerprint = _live_work_fingerprint(session, context)
        now_ms = int(_time.time() * 1000)
        cached = entries.get(session_id)
        if cached and cached["fingerprint"] == fingerprint and now_ms - cached["verifiedAtMs"] < ttl_ms:
            stats["hits"] += 1
            return _copy_verdict(cached["verdict"])
        stats["misses"] += 1
        verdict = await _resolve_session_live_work(session, context)
        final_fingerprint = _live_work_fingerprint(session, context)
        if not any(":unavailable:" in value for value in verdict["stale"]):
            entries[session_id] = {
                "fingerprint": final_fingerprint,
                "verifiedAtMs": now_ms,
                "verdict": _copy_verdict(verdict),
            }
        else:
            entries.pop(session_id, None)
        return verdict

    def invalidate(session_id: str) -> None:
        if entries.pop(session_id, None) is not None:
            stats["invalidations"] += 1

    def get_stats() -> dict[str, int]:
        return {**stats, "entries": len(entries), "ttlMs": ttl_ms}

    return {"resolve": resolve, "invalidate": invalidate, "getStats": get_stats}


async def _reconcile_dead_task_meta(
    task_id: str,
    meta: dict[str, _Any],
    kind: str,
    *,
    apply: bool,
) -> tuple[dict[str, _Any], bool]:
    active = _ACTIVE_PROCESS_STATUSES.get(kind, set())
    if meta.get("status") not in active:
        return meta, False
    pid = meta.get("pid")
    if not isinstance(pid, int) or pid <= 0 or _is_process_alive(pid) or not _past_dead_work_grace(meta.get("startedAt")):
        return meta, False
    now = _now_iso()
    if kind == "subagent":
        summary = "Worker process is no longer running; marked failed during reconciliation."
        next_meta = {
            **meta,
            "status": "failed",
            "error": meta.get("error") or summary,
            "summary": meta.get("summary") or summary,
            "completionReason": meta.get("completionReason") or "worker_lost",
            "updatedAt": now,
            "finishedAt": meta.get("finishedAt") or now,
            "completedAt": meta.get("completedAt") or now,
        }
    else:
        next_meta = {
            **meta,
            "status": "terminated",
            "summary": meta.get("summary")
            or "Worker process is no longer running; reconciled during projection. Final completion details were unavailable.",
            "updatedAt": now,
            "finishedAt": meta.get("finishedAt") or now,
        }
    if apply:
        await _write_task_meta(task_id, next_meta)
        if kind == "subagent":
            agent_id = meta.get("agent_id") or (meta.get("metadata") or {}).get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                await _reconcile_child_by_id(agent_id, now, next_meta["summary"])
    return next_meta, True


async def _reconcile_dead_child_agent(
    child: dict[str, _Any],
    *,
    apply: bool,
) -> tuple[dict[str, _Any], bool]:
    if not _is_active_child_agent_status(child.get("status")):
        return child, False
    pid = child.get("pid")
    if not isinstance(pid, int) or pid <= 0 or _is_process_alive(pid) or not _past_dead_work_grace(child.get("startedAt")):
        return child, False
    now = _now_iso()
    summary = "Worker process is no longer running; marked failed during reconciliation."
    next_child = {
        **child,
        "status": "failed",
        "error": child.get("error") or summary,
        "completionReason": child.get("completionReason") or "worker_lost",
        "completedAt": child.get("completedAt") or now,
        "lastUpdatedAt": now,
    }
    if apply and isinstance(child.get("agentId"), str):
        agent_id = child["agentId"]
        await _mutate_child_agent(agent_id, lambda current: next_child if current else None)
    return next_child, True


async def _reconcile_child_by_id(agent_id: str, now: str, summary: str) -> None:
    def mutate(current: dict[str, _Any] | None) -> dict[str, _Any] | None:
        if not current or not _is_active_child_agent_status(current.get("status")):
            return current
        return {
            **current,
            "status": "failed",
            "error": current.get("error") or summary,
            "completionReason": current.get("completionReason") or "worker_lost",
            "completedAt": current.get("completedAt") or now,
            "lastUpdatedAt": now,
        }

    await _mutate_child_agent(agent_id, mutate)


async def _reconcile_terminal_monitor(
    monitor: dict[str, _Any],
    *,
    apply: bool,
) -> tuple[dict[str, _Any], bool]:
    if not _is_active_monitor_status(monitor.get("status")):
        return monitor, False
    task_id = monitor.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        return monitor, False
    try:
        task = await _read_task_meta(task_id)
    except Exception:
        return monitor, False
    if not isinstance(task, dict):
        return monitor, False
    status = task.get("status")
    if status == "completed":
        next_status = "completed"
    elif status in {"failed", "truncated"}:
        next_status = "failed"
    elif status in {"terminated", "stopped", "cancelled"}:
        next_status = "stopped"
    else:
        return monitor, False
    now = _now_iso()
    next_monitor = {
        **monitor,
        "status": next_status,
        "updatedAt": now,
        "completedAt": monitor.get("completedAt") or now,
        "lastObservedTaskStatus": status,
    }
    if next_status in {"failed", "stopped"}:
        next_monitor["failureReason"] = task.get("error") or monitor.get("failureReason")
    if apply:
        await _save_monitor(next_monitor)
    return next_monitor, True


def _task_belongs_to_session(meta: dict[str, _Any], session_id: str, kind: str) -> bool:
    if not session_id:
        return False
    if meta.get("sessionId") == session_id or meta.get("session_id") == session_id:
        return True
    return kind == "subagent" and meta.get("parent_session_id") == session_id


def _remote_is_live(remote: dict[str, _Any], bridge: dict[str, _Any] | None) -> bool:
    if remote.get("status") in _TERMINAL_REMOTE_STATUSES:
        return False
    if isinstance(bridge, dict) and bridge.get("status") in _TERMINAL_REMOTE_STATUSES:
        return False
    return True


def _past_dead_work_grace(value: _Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        started = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=_datetime.timezone.utc)
        age_ms = (_datetime.datetime.now(_datetime.timezone.utc) - started).total_seconds() * 1000
        return age_ms >= _DEAD_WORK_GRACE_MS
    except (TypeError, ValueError, OverflowError):
        return False


def _normalized_ids(value: _Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in result:
            result.append(item)
    return result


def _live_work_fingerprint(session: dict[str, _Any], context: dict[str, _Any] | None) -> str:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    paths: list[str] = []
    paths.extend(_task_meta_path(task_id) for task_id in _normalized_ids(metadata.get("taskIds")))
    paths.extend(_monitor_path(monitor_id) for monitor_id in _normalized_ids(metadata.get("monitorIds")))
    paths.extend(_child_agent_path(agent_id) for agent_id in _normalized_ids(metadata.get("childAgentIds")))
    remote_id = metadata.get("remoteSessionId")
    if isinstance(remote_id, str) and remote_id:
        paths.append(_remote_session_path(remote_id))
    bridge_id = metadata.get("bridgeId")
    if isinstance(bridge_id, str) and bridge_id:
        paths.append(_bridge_path(bridge_id))
    context = context or {}
    for check in context.get("healthChecks") or []:
        if isinstance(check, dict) and isinstance(check.get("taskId"), str):
            paths.append(_health_check_meta_path(check["taskId"]))
    fingerprints = [_path_fingerprint(path) for path in sorted(set(paths))]
    context_shape = {
        key: [
            {
                field: record.get(field)
                for field in ("agentId", "taskId", "id", "sessionId", "parentSessionId", "status", "pid", "updatedAt", "lastUpdatedAt")
            }
            for record in value
            if isinstance(record, dict)
        ]
        for key, value in context.items()
        if key in {"childAgents", "healthChecks", "monitors"} and isinstance(value, list)
    }
    return _json.dumps(
        {
            "sessionId": session.get("id"),
            "markers": {
                key: metadata.get(key)
                for key in (
                    "taskIds",
                    "monitorIds",
                    "childAgentIds",
                    "childSessionIds",
                    "backgroundSessionIds",
                    "remoteSessionId",
                    "bridgeId",
                    "runtimeRestore",
                )
            },
            "paths": fingerprints,
            "context": context_shape,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _path_fingerprint(path: str) -> tuple[_Any, ...]:
    try:
        stat = _os.stat(path, follow_symlinks=False)
        return (path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_mode)
    except OSError as exc:
        return (path, type(exc).__name__, getattr(exc, "errno", None))


def _copy_verdict(verdict: dict[str, _Any]) -> dict[str, _Any]:
    return {
        "live": bool(verdict.get("live")),
        "families": list(verdict.get("families") or []),
        "reconciled": list(verdict.get("reconciled") or []),
        "stale": list(verdict.get("stale") or []),
        "checkedAt": verdict.get("checkedAt"),
    }


def _task_session_ids(meta: dict[str, _Any] | None) -> list[str]:
    if not isinstance(meta, dict):
        return []
    return _normalized_ids(
        [meta.get("sessionId"), meta.get("session_id"), meta.get("parent_session_id")]
    )


def _invalidate_live_work_cache(cache: dict[str, _Any], *session_ids: str) -> None:
    for session_id in _normalized_ids(list(session_ids)):
        cache["invalidate"](session_id)
