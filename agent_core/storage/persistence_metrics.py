from __future__ import annotations

import contextvars
import itertools
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


PersistenceObserver = Callable[[dict[str, Any]], Any]

_OBSERVERS: list[PersistenceObserver] = []
_OBSERVERS_LOCK = threading.RLock()
_CALL_IDS = itertools.count(1)
_CURRENT_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_core_persistence_call_id",
    default=None,
)


@dataclass
class PersistenceCallMeasurement:
    call_id: str
    session_id: str
    started_ns: int
    token: contextvars.Token[str | None]
    finished: bool = False


def registerPersistenceObserver(observer: PersistenceObserver) -> Callable[[], None]:
    """Observe persistence calls and their physical writes.

    This is an internal measurement seam. Observers must never affect runtime
    behavior, so callback errors are isolated and the returned unsubscriber is
    idempotent.
    """

    with _OBSERVERS_LOCK:
        _OBSERVERS.append(observer)

    def unsubscribe() -> None:
        with _OBSERVERS_LOCK:
            if observer in _OBSERVERS:
                _OBSERVERS.remove(observer)

    return unsubscribe


def persistenceObserversActive() -> bool:
    with _OBSERVERS_LOCK:
        return bool(_OBSERVERS)


def beginPersistenceCall(session_id: str, message_count: int) -> PersistenceCallMeasurement | None:
    if not persistenceObserversActive():
        return None
    call_id = f"persist-{next(_CALL_IDS)}"
    token = _CURRENT_CALL_ID.set(call_id)
    measurement = PersistenceCallMeasurement(call_id, session_id, time.perf_counter_ns(), token)
    _notify(
        {
            "schemaVersion": 1,
            "event": "persist_call_started",
            "persistenceCallId": call_id,
            "sessionId": session_id,
            "messageCount": message_count,
        }
    )
    return measurement


def finishPersistenceCall(
    measurement: PersistenceCallMeasurement | None,
    outcome: str,
    *,
    revision: int | None = None,
) -> None:
    if measurement is None or measurement.finished:
        return
    measurement.finished = True
    elapsed_ns = time.perf_counter_ns() - measurement.started_ns
    event: dict[str, Any] = {
        "schemaVersion": 1,
        "event": "persist_call_finished",
        "persistenceCallId": measurement.call_id,
        "sessionId": measurement.session_id,
        "outcome": outcome,
        "elapsedNs": elapsed_ns,
    }
    if revision is not None:
        event["revision"] = revision
    _notify(event)
    _CURRENT_CALL_ID.reset(measurement.token)


def recordPersistenceIo(
    operation: str,
    file_path: str,
    byte_count: int,
    elapsed_ns: int,
) -> None:
    if not persistenceObserversActive():
        return
    _notify(
        {
            "schemaVersion": 1,
            "event": "storage_io",
            "persistenceCallId": _CURRENT_CALL_ID.get(),
            "operation": operation,
            "filePath": file_path,
            "byteCount": byte_count,
            "elapsedNs": elapsed_ns,
        }
    )


def _notify(event: dict[str, Any]) -> None:
    with _OBSERVERS_LOCK:
        observers = list(_OBSERVERS)
    for observer in observers:
        try:
            observer(dict(event))
        except Exception:
            pass
