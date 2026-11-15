"""Adding and changing scheduled jobs: one path for the agent's tools and the
host's API, so both see the same rules and both wake the timer.

Every change is written to the job's record first, then the runtime's
timer for this home (if one runs in this process) is woken and told, and the
host hears ``scheduler.job_changed``. A job written while no timer runs is
picked up when one starts, or at its next clock check.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..storage.paths import get_state_root
from . import jobs as job_rules
from .rules import RuleError
from .store import JobNotFound, check_job_id, list_jobs, load_job, mutate_job, new_job_id

# The running timer of each runtime home in this process (see timer.py).
_TIMERS: dict[str, Any] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def timer_for_home(home: str | None = None) -> Any:
    return _TIMERS.get(home or get_state_root())


async def create_job(*, job_id: str | None = None, **fields: Any) -> dict[str, Any]:
    """A new job; ``fields`` are :func:`jobs.build_job`'s. A given id that
    already names a job is an error (use :func:`ensure_job` for host jobs)."""
    target = check_job_id(job_id) if job_id else new_job_id()
    record = job_rules.build_job(job_id=target, now=now_utc(), **fields)

    def create(current: dict[str, Any] | None) -> dict[str, Any]:
        if current is not None:
            raise RuleError("scheduler.job_exists", f"Job {target} already exists.")
        return record

    saved, _ = await mutate_job(target, create)
    _changed(saved, "created")
    return saved


async def ensure_job(job_id: str, **fields: Any) -> dict[str, Any]:
    """A host's standing job: created when missing or finished, otherwise
    left exactly as it is (its schedule may have moved since)."""
    target = check_job_id(job_id)
    fresh = job_rules.build_job(job_id=target, now=now_utc(), **fields)
    created = False

    def ensure(current: dict[str, Any] | None) -> dict[str, Any] | None:
        nonlocal created
        if current is not None and current.get("state") in ("active", "paused"):
            return None
        created = True
        return {**fresh, "revision": int((current or {}).get("revision") or 0)}

    saved, _ = await mutate_job(target, ensure)
    if created:
        _changed(saved, "created")
    return saved


async def update_job(job_id: str, changes: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    """Edit, pause, resume or cancel. With ``session_id``, only that
    session's own turn jobs may be touched (the agent's scope)."""
    check_job_id(job_id)
    now = now_utc()

    def edit(current: dict[str, Any] | None) -> dict[str, Any]:
        if current is None or not _in_scope(current, session_id):
            raise JobNotFound(job_id)
        return job_rules.edit_job(current, changes, now)

    saved, _ = await mutate_job(job_id, edit)
    _changed(saved, "cancelled" if saved["state"] == "cancelled" else "updated")
    return saved


async def get_job(job_id: str, *, session_id: str | None = None) -> dict[str, Any]:
    record = await load_job(check_job_id(job_id))
    if record is None or not _in_scope(record, session_id):
        raise JobNotFound(job_id)
    return record


async def find_jobs(*, session_id: str | None = None, include_finished: bool = False, kind: str | None = None) -> list[dict[str, Any]]:
    """Jobs, soonest first; finished and cancelled ones only when asked."""
    found = [
        job
        for job in await list_jobs()
        if (session_id is None or job.get("sessionId") == session_id)
        and (kind is None or job.get("kind") == kind)
        and (include_finished or job.get("state") in ("active", "paused"))
    ]
    return sorted(found, key=lambda job: (job.get("nextFireAt") is None, job.get("nextFireAt") or "", job["id"]))


def _in_scope(job: dict[str, Any], session_id: str | None) -> bool:
    return session_id is None or (job.get("sessionId") == session_id and job.get("kind") == job_rules.TURN)


def _changed(job: dict[str, Any], reason: str) -> None:
    timer = timer_for_home()
    if timer is not None:
        timer.job_changed(job, reason)
