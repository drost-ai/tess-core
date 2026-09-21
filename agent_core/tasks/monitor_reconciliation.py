"""Latest observation and retained execution history are separate durable facts."""
from __future__ import annotations

from copy import deepcopy


def preserve_interruption(record, items, reason):
    state = record["delivery"]
    saved = state.get("interruption")
    for item in items:
        if item.get("recoveryResult") or not (item.get("executionStarted") or item.get("result")):
            continue
        if saved is None:
            saved = state["interruption"] = {"reason": reason, "at": record["updatedAt"],
                                             "uncertain": False, "deliveries": []}
        if not any(previous["id"] == item["id"] for previous in saved["deliveries"]):
            # Retain the exact checkpoint and provenance, never claim authority.
            keys = ("id", "reason", "taskStatus", "executionStarted", "result", "authoredAt",
                    "error", "generation", "fromByte", "toByte", "observedAt", "attempts")
            snapshot = {key: deepcopy(item[key]) for key in keys if key in item}
            receipt = state.get("lastReceipt")
            snapshot["receipt"] = deepcopy(receipt) if receipt and receipt.get("deliveryId") == item["id"] else None
            # No receipt means unacknowledged, not proof of no durable publication.
            saved["deliveries"].append(snapshot)
        saved["uncertain"] |= bool(item.get("executionStarted") and not item.get("result")
                                   or item.get("error") == "monitor.execution_uncertain")


def consolidate_held(record):
    state = record["delivery"]
    queue = state["queue"]
    if not queue:
        return
    latest = queue[-1]
    uncertain_latest = latest.get("executionStarted") and (
        not latest.get("result") or latest.get("error") == "monitor.execution_uncertain")
    preserve_interruption(record, queue[:-1] + ([latest] if uncertain_latest else []), "mute")
    if state.get("interruption") and (uncertain_latest or latest.get("result")):
        preserve_interruption(record, [latest], "mute")
        # A changed response must not reuse an identity that might already have
        # reached the durable journal before an acknowledgment failure.
        state["sequence"] += 1
        latest = {key: deepcopy(latest[key]) for key in (
            "reason", "generation", "outputIdentity", "toByte", "fromByte", "taskStatus",
            "exitCode", "observedAt", "availableAt") if key in latest}
        latest.update(id=f"monitor:{record['id']}:delivery:{state['sequence']}", attempts=0)
    for key in ("token", "owner", "leaseUntil"):
        latest.pop(key, None)
    latest["state"] = "authored" if latest.get("result") else "pending"
    state["queue"] = [latest]


def interruption_result(record, item):
    saved = record["delivery"]["interruption"]
    text = f"The task for {str(record.get('goal') or record['taskId'])[:4096]} is {item['taskStatus']}."
    if saved["uncertain"]:
        text += " Earlier follow-up was interrupted; tools may already have run and verification may be incomplete."
    if record["delivery"]["mode"] == "continuation":
        text += " Automatic follow-up is paused to avoid repeating earlier actions; request any further inspection explicitly."
    for previous in saved["deliveries"]:
        checkpoint = (previous.get("result") or {}).get("text")
        if checkpoint:
            text += "\nEarlier saved response (before this observation): " + checkpoint[:8192]
    return {"text": text, "rationale": "Latest task state with preserved interruption history"}
