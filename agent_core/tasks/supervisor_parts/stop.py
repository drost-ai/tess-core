from __future__ import annotations

import contextlib
import signal
from ..._utils import maybe_await, now_iso
from ...process._termination import drain_owned_process
from ...process.identity import signal_owned_process
from ...storage import read_task_meta
from typing import Any, Callable
from .live_work import _invalidate_live_work_cache
from .mappers import _task_record_event_payload
from .reconciliation import _mark_subagent_cancelled_from_stop
from ..terminal_store import clear_prepared_task_stop, commit_prepared_task_stop, prepare_task_stop


def _create_task_stopper(
    coordinator: Any,
    live_work_cache: dict[str, Any],
    emit: Callable[[str, dict[str, Any], str | None], Any],
    abort_health: Callable[[str], Any] | None = None,
) -> Callable[..., Any]:
    async def stop_task(task_id: str, signal_name: str = "SIGTERM") -> dict[str, Any] | None:
        release_stop = await coordinator.acquireStop(task_id)
        try:
            prepared = await prepare_task_stop(task_id, signal_name)
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
            if not isinstance(transition_id, str):
                raise RuntimeError(
                    f"Task stop preparation did not persist a transition: {task_id}"
                )
            if not isinstance(identity, dict):
                # No owned process is durably attached (stop landed in the
                # launch window). The stop intent is already persisted, so
                # commit the terminal transition instead of wedging the task;
                # a still-"starting" reservation reconciles first-terminal-
                # wins at the next boot sweep, and an attach against the
                # terminal record is refused by the reservation ladder.
                committed = await commit_prepared_task_stop(task_id, transition_id)
            else:
                signum = getattr(signal, signal_name, None)
                if signum is None:
                    await clear_prepared_task_stop(task_id, transition_id)
                    raise ValueError(f"Unsupported task stop signal: {signal_name}")
                release_finalization = coordinator.holdFinalization(task_id)
                try:
                    try:
                        if signal_name == "SIGTERM":
                            # A deliberate stop must end in a dead worker: the
                            # identity-verified TERM->KILL drain replaces the
                            # old fire-TERM-and-declare-stopped behavior that
                            # left TERM-immune children running forever.
                            await drain_owned_process(identity)
                        else:
                            await signal_owned_process(identity, signum)
                    except Exception:
                        with contextlib.suppress(Exception):
                            await clear_prepared_task_stop(task_id, transition_id)
                        raise
                    await coordinator.afterSignal(task_id)
                    committed = await commit_prepared_task_stop(task_id, transition_id)
                finally:
                    release_finalization()

            result = committed["result"]
            record = committed.get("record")
            if result.get("changed"):
                if prepared.get("kind") == "subagent":
                    meta = await read_task_meta(task_id)
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
