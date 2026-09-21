"""Pure monitor outbox mutations, committed with observation in one JSON record."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..storage.paths import get_state_root
from .monitor_reconciliation import consolidate_held

TERMINAL = {"completed", "failed", "stopped"}
MAX_ATTEMPTS = 3


def initial_delivery_state(mode: str = "notification") -> dict[str, Any]:
    if mode not in {"notification", "continuation"}:
        raise ValueError("delivery_mode must be notification or continuation")
    return {"version": 1, "runtimeHome": get_state_root(), "mode": mode,
            "sequence": 0, "queue": [], "generation": 0,
            "processedBytes": 0, "processedGeneration": 0,
            "publishedBytes": 0, "publishedGeneration": 0}


def queue_observation(previous: dict[str, Any], record: dict[str, Any],
                      task: dict[str, Any] | None, reason: str | None,
                      now: int, output_identity: str | None = None) -> dict[str, Any]:
    if previous.get("deliveryStoppedAt"):
        return record
    # Adopt only records still being observed. Historical terminal records never
    # get a synthetic delivery on startup.
    state = deepcopy(previous.get("delivery") or initial_delivery_state())
    record = {**record, "delivery": state}
    if previous.get("status") == "muted":
        record["muted"] = True
    size = int(record.get("lastObservedOutputSize") or 0)
    rotated = (size < int(previous.get("lastObservedOutputSize") or 0)
               or (output_identity is not None and state.get("outputIdentity") is not None
                   and output_identity != state["outputIdentity"]))
    if rotated:
        state["generation"] += 1
        reason = reason or "progress"
    # While an unclaimed notice is held, its latest observation is authoritative,
    # even if a small progress delta does not independently cross the threshold.
    if not reason and state["queue"] and size != int(previous.get("lastObservedOutputSize") or 0):
        reason = state["queue"][-1]["reason"]
    if output_identity is not None:
        state["outputIdentity"] = output_identity
    if task and task.get("sessionId") != record["sessionId"]:
        state.update(queue=[], lastError="monitor.task_not_owned")
        record["deliveryStoppedAt"] = record["updatedAt"]
        return record
    if not reason:
        return record
    if reason == "heartbeat" and state["queue"]:
        return record
    generation = state["generation"]
    key = [generation, size, record.get("lastObservedTaskStatus"), reason]
    if reason == "heartbeat":
        key.append(now // max(1, int(record.get("heartbeatIntervalMs") or 60000)))
    if key == state.get("lastQueuedKey"):
        return record
    state["lastQueuedKey"] = key
    queue = state["queue"]
    # At most one frozen owned/checkpointed delivery and one latest observation.
    pending = next((item for item in queue if item["state"] in {"pending", "retryable", "failed"}
                    and not item.get("result") and not item.get("executionStarted")), None)
    if pending is None:
        state["sequence"] += 1
        pending = {"id": f"monitor:{record['id']}:delivery:{state['sequence']}", "attempts": 0}
        queue.append(pending)
    elif reason in TERMINAL and pending.get("reason") not in TERMINAL:
        # A completion has its own retry budget even if progress delivery failed.
        state["sequence"] += 1
        pending.update(id=f"monitor:{record['id']}:delivery:{state['sequence']}", attempts=0)
    pending.pop("envelope", None)
    pending.update(state="pending", reason=reason, generation=generation,
                   outputIdentity=state.get("outputIdentity"), toByte=size,
                   fromByte=state["processedBytes"] if state["processedGeneration"] == generation else 0,
                   taskStatus=record.get("lastObservedTaskStatus"), exitCode=(task or {}).get("exitCode"),
                   observedAt=record["lastCheckAt"], availableAt=now)
    # A new observation does not reset a failed delivery's retry budget.
    if pending["attempts"] >= MAX_ATTEMPTS:
        pending["state"] = "failed"
    if len(queue) > 2:
        state["queue"] = [queue[0], queue[-1]]
    if record.get("muted"):
        consolidate_held(record)
    return record


def mute_monitor_record(monitor: dict[str, Any], muted: bool, now: str) -> dict[str, Any]:
    record = deepcopy(monitor)
    if record.get("deliveryStoppedAt"):
        return {"record": monitor, "changed": False}
    changed = bool(record.get("muted") or record.get("status") == "muted") != muted
    record["muted"] = muted
    if record.get("status") == "muted":
        record["status"] = "active"
    if changed:
        record["updatedAt"] = now
        state = record.get("delivery")
        if state and state["queue"]:
            consolidate_held(record)
    return {"record": record, "changed": changed, "transition": changed}
