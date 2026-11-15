"""Scheduled jobs on disk: one JSON record per job under the runtime home.

``<home>/scheduler/jobs/<id>.json``, written inside the ``schedule-job``
record transaction so the tools, the host and the timer never lose one
another's changes, across processes too. A job is never deleted: cancelled
and finished jobs stay as records of what was scheduled and what happened.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Callable

from ..storage.io_executor import run_storage_io
from ..storage.json_io import mutate_json_record, read_json_file
from ..storage.paths import get_state_root

NAMESPACE = "schedule-job"
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")


class JobNotFound(LookupError):
    code = "scheduler.job_not_found"

    def __init__(self, job_id: str) -> None:
        super().__init__(f"No scheduled job {job_id}.")
        self.job_id = job_id


def jobs_dir() -> str:
    return os.path.join(get_state_root(), "scheduler", "jobs")


def new_job_id() -> str:
    return f"sch_{uuid.uuid4().hex[:20]}"


def check_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise ValueError(f"Invalid scheduled job id: {job_id!r} (lowercase letters, digits, '.', '_' and '-', at most 80).")
    return job_id


def job_path(job_id: str) -> str:
    return os.path.join(jobs_dir(), f"{check_job_id(job_id)}.json")


async def load_job(job_id: str) -> dict[str, Any] | None:
    record = await read_json_file(job_path(job_id), None)
    return record if isinstance(record, dict) else None


def _list_ids_sync() -> list[str]:
    try:
        names = os.listdir(jobs_dir())
    except FileNotFoundError:
        return []
    return sorted(name[:-5] for name in names if name.endswith(".json") and _ID.fullmatch(name[:-5]))


async def list_jobs() -> list[dict[str, Any]]:
    jobs = []
    for job_id in await run_storage_io(_list_ids_sync):
        try:
            record = await load_job(job_id)
        except (OSError, ValueError):
            continue  # a torn record is skipped, never fatal to the others
        if record is not None:
            jobs.append(record)
    return jobs


async def mutate_job(job_id: str, mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None]) -> tuple[dict[str, Any] | None, bool]:
    """Apply ``mutate`` to the job's latest record under its transaction.
    ``mutate`` returns the new record, or None to change nothing; it may
    raise to abort. Returns the record as it now is and whether it changed."""

    def apply(current: Any) -> dict[str, Any]:
        latest = current if isinstance(current, dict) else None
        updated = mutate(latest)
        if updated is None:
            return {"value": latest, "changed": False}
        updated["revision"] = int((latest or {}).get("revision") or 0) + 1
        return {"value": updated, "changed": True}

    result = await mutate_json_record(namespace=NAMESPACE, key=job_id, filePath=job_path(job_id), fallback=None, mutate=apply)
    return result["value"], result["changed"]
