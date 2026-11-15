"""What a scheduled job is, and how it changes: built, edited, fired, advanced.

A job is either a ``turn`` (the runtime runs an agent turn in its session
with the job's instructions) or a host kind (a name the host registered a
handler for: its own periodic work, sharing the one timer). It carries its
rule and zone, its next fire as a UTC instant, at most one claim (the fire in
progress) and the outcome of its last fire. Pure functions over records; the
store writes them and the timer decides when.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import rules
from .rules import RuleError

TURN = "turn"
STATES = ("active", "paused", "done", "cancelled")
CREATORS = ("agent", "host", "owner")
UPCOMING = 3
_TEXT_LIMIT = 8000


def build_job(
    *,
    job_id: str,
    kind: str,
    rule: dict[str, Any],
    timezone: str,
    now: datetime,
    session_id: str | None = None,
    label: str = "",
    instructions: str = "",
    payload: dict[str, Any] | None = None,
    created_by: str = "host",
) -> dict[str, Any]:
    if created_by not in CREATORS:
        raise ValueError(f"created_by must be one of {', '.join(CREATORS)}")
    if not isinstance(kind, str) or not kind:
        raise ValueError("A job needs a kind.")
    if kind == TURN:
        if not session_id:
            raise RuleError("scheduler.session_required", "A turn job belongs to a session.")
        if not instructions.strip():
            raise RuleError("scheduler.instructions_required", "Say what should happen when the job fires.")
    rules.zone(timezone)
    normalized = rules.normalize(rule)
    first = rules.first_fire(normalized, timezone, now)
    if first is None:
        raise RuleError("scheduler.never_fires", "That rule has no time to fire at.")
    if normalized["kind"] == "once" and created_by != "host" and first <= now:
        raise RuleError(
            "scheduler.time_in_past",
            f"{rules.local_text(first, timezone)} is already past (now {rules.local_text(now, timezone)}).",
        )
    stamp = rules.iso(now)
    return {
        "id": job_id,
        "kind": kind,
        "sessionId": session_id,
        "label": _text(label, 200) or _default_label(instructions, kind),
        "instructions": _text(instructions, _TEXT_LIMIT),
        "payload": dict(payload or {}),
        "rule": normalized,
        "timezone": timezone.strip(),
        "state": "active",
        "createdBy": created_by,
        "createdAt": stamp,
        "updatedAt": stamp,
        "nextFireAt": rules.iso(first),
        "retryAt": None,
        "claim": None,
        "lastFire": None,
        "fireCount": 0,
        "revision": 0,
    }


def edit_job(job: dict[str, Any], changes: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Label, instructions, rule, zone, pause/resume or cancel; a new rule or
    zone recomputes the next fire. Finished and cancelled jobs are final."""
    if job["state"] in ("done", "cancelled"):
        raise RuleError("scheduler.job_finished", f"Job {job['id']} is {job['state']}; create a new one instead.")
    updated = dict(job)
    if changes.get("label") is not None:
        updated["label"] = _text(changes["label"], 200) or job["label"]
    if changes.get("instructions") is not None:
        if job["kind"] == TURN and not str(changes["instructions"]).strip():
            raise RuleError("scheduler.instructions_required", "Say what should happen when the job fires.")
        updated["instructions"] = _text(changes["instructions"], _TEXT_LIMIT)
    if changes.get("payload") is not None:
        updated["payload"] = dict(changes["payload"])
    if changes.get("rule") is not None or changes.get("timezone") is not None:
        timezone = (changes.get("timezone") or job["timezone"]).strip()
        rules.zone(timezone)
        rule = rules.normalize(changes["rule"]) if changes.get("rule") is not None else job["rule"]
        first = rules.first_fire(rule, timezone, now)
        if first is None or (rule["kind"] == "once" and job["createdBy"] != "host" and first <= now):
            raise RuleError("scheduler.time_in_past", "That time is already past.")
        updated.update(rule=rule, timezone=timezone, nextFireAt=rules.iso(first), retryAt=None)
    if changes.get("state") is not None:
        state = changes["state"]
        if state not in ("active", "paused", "cancelled"):
            raise ValueError("state must be active, paused or cancelled")
        if state == "active" and job["state"] == "paused" and job["nextFireAt"] and rules.parse(job["nextFireAt"]) <= now:
            # Resumed after its time passed while paused: the missed fire is
            # not made up; a recurring job continues from now.
            following = rules.next_fire(job["rule"], job["timezone"], now)
            updated["nextFireAt"] = rules.iso(following) if following else None
            if following is None:
                state = "done"
        updated["state"] = state
    updated["updatedAt"] = rules.iso(now)
    return updated


def due(job: dict[str, Any], now: datetime) -> bool:
    if job.get("state") != "active" or not job.get("nextFireAt"):
        return False
    at = rules.parse(job["nextFireAt"])
    if job.get("retryAt"):
        at = max(at, rules.parse(job["retryAt"]))
    return at <= now


def wake_at(job: dict[str, Any]) -> datetime | None:
    """When the timer should look at this job next (None: never)."""
    if job.get("state") != "active" or not job.get("nextFireAt") or job.get("claim"):
        return None
    at = rules.parse(job["nextFireAt"])
    if job.get("retryAt"):
        at = max(at, rules.parse(job["retryAt"]))
    return at


def advance(job: dict[str, Any], *, fire: dict[str, Any], now: datetime, next_override: datetime | None = None, cancel: bool = False) -> dict[str, Any]:
    """The fire is over (whatever its outcome): record it and move on to the
    next occurrence after now, so missed occurrences collapse into this one."""
    updated = dict(job)
    updated["lastFire"] = dict(fire)
    updated["fireCount"] = int(job.get("fireCount") or 0) + 1
    updated["claim"] = None
    updated["retryAt"] = None
    updated["updatedAt"] = rules.iso(now)
    if cancel:
        updated["state"], updated["nextFireAt"] = "cancelled", None
        return updated
    if next_override is not None:
        following: datetime | None = next_override
    else:
        previous = rules.parse(fire["scheduledFor"]) if fire.get("scheduledFor") else None
        following = rules.next_fire(job["rule"], job["timezone"], now, previous=previous)
    if following is None:
        updated["state"], updated["nextFireAt"] = "done", None
    else:
        updated["nextFireAt"] = rules.iso(following)
    return updated


def view(job: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A job as a person (or the model) reads it: times in its own zone."""
    timezone = job["timezone"]
    next_at = rules.parse(job["nextFireAt"]) if job.get("nextFireAt") else None
    upcoming: list[str] = []
    if next_at is not None and rules.recurring(job["rule"]):
        cursor = next_at
        upcoming.append(rules.local_text(cursor, timezone))
        for _ in range(UPCOMING - 1):
            cursor = rules.next_fire(job["rule"], timezone, cursor, previous=cursor)
            if cursor is None:
                break
            upcoming.append(rules.local_text(cursor, timezone))
    return {
        "id": job["id"],
        "kind": job["kind"],
        "label": job["label"],
        "state": job["state"],
        "sessionId": job.get("sessionId"),
        "createdBy": job.get("createdBy"),
        "schedule": rules.describe(job["rule"], timezone),
        "timezone": timezone,
        "nextFireAt": job.get("nextFireAt"),
        "nextFire": rules.local_text(next_at, timezone) if next_at else None,
        "nextFireIn": rules.until_text(next_at, now) if next_at else None,
        "upcoming": upcoming,
        "instructions": job.get("instructions") if job["kind"] == TURN else None,
        "lastFire": job.get("lastFire"),
        "fireCount": job.get("fireCount", 0),
    }


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _default_label(instructions: str, kind: str) -> str:
    first = instructions.strip().splitlines()[0] if instructions.strip() else kind
    return first[:80]
