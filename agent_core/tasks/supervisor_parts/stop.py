from __future__ import annotations

import contextlib
import signal
from ..._utils import maybe_await, now_iso
from ...process.identity import signalOwnedProcess
from ...storage import readTaskMeta
from typing import Any, Callable
from .live_work import _invalidate_live_work_cache
from .mappers import _task_record_event_payload
from .reconciliation import _mark_subagent_cancelled_from_stop
from ..terminal_store import clearPreparedTaskStop, commitPreparedTaskStop, prepareTaskStop


def _create_task_stopper(
    coordinator: Any,
    live_work_cache: dict[str, Any],
    emit: Callable[[str, dict[str, Any], str | None], Any],
    abort_health: Callable[[str], Any] | None = None,
) -> Callable[..., Any]:
    async def stop_task(task_id: str, signal_name: str = "SIGTERM") -> dict[str, Any] | None:
        release_stop = await coordinator.acquireStop(task_id)
        try:
            prepared = await prepareTaskStop(task_id, signal_name)
            if not prepared:
                return None
            result = prepared.get("result")
            affected_session_ids = [
                value
                for value in [prepared.get("sessionId")]
                if isinstance(value, str) and value
            ]
            if result:
                if prepared.get("healthCheck") and abort_health:
                    await maybe_await(abort_health(task_id))
                if result.get("changed"):
                    await emit(
                        "task.stopped",
                        _stop_payload(task_id, prepared, result),
                        prepared.get("sessionId"),
                    )
                _invalidate_live_work_cache(live_work_cache, *affected_session_ids)
                return result

            identity = prepared.get("identity")
            transition_id = prepared.get("transitionId")
            if not isinstance(identity, dict) or not isinstance(transition_id, str):
                raise RuntimeError(
                    f"Task stop preparation did not capture an owned process: {task_id}"
                )
            signum = getattr(signal, signal_name, None)
            if signum is None:
                await clearPreparedTaskStop(task_id, transition_id)
                raise ValueError(f"Unsupported task stop signal: {signal_name}")
            release_finalization = coordinator.holdFinalization(task_id)
            try:
                try:
                    await signalOwnedProcess(identity, signum)
                except Exception:
                    with contextlib.suppress(Exception):
                        await clearPreparedTaskStop(task_id, transition_id)
                    raise
                await coordinator.afterSignal(task_id)
                committed = await commitPreparedTaskStop(task_id, transition_id)
            finally:
                release_finalization()

            result = committed["result"]
            record = committed.get("record")
            if result.get("changed"):
                if prepared.get("kind") == "subagent":
                    meta = await readTaskMeta(task_id)
                    if meta:
                        await _mark_subagent_cancelled_from_stop(
                            meta,
                            task_id,
                            record.get("completedAt")
                            if isinstance(record, dict)
                            else now_iso(),
                        )
                await emit(
                    "task.stopped",
                    _stop_payload(task_id, {**prepared, "record": record}, result),
                    prepared.get("sessionId"),
                )
            _invalidate_live_work_cache(live_work_cache, *affected_session_ids)
            return result
        finally:
            release_stop()

    return stop_task


def _stop_payload(
    task_id: str,
    prepared: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    record = prepared.get("record")
    if isinstance(record, dict):
        return _task_record_event_payload(record)
    return {
        "session_id": prepared.get("sessionId"),
        "task_id": task_id,
        "kind": prepared.get("kind"),
        "status": result.get("status"),
        "title": prepared.get("title"),
        "summary": result.get("note"),
        "transition_id": result.get("transitionId"),
    }
