from __future__ import annotations

from ...storage import load_health_check_task, load_task_record, read_task_meta
from .records import _build_task_record
from typing import Any


async def _inspect_latest_task_record(task_id: str) -> dict[str, Any] | None:
    """Prefer the newest health projection while its three stores converge."""

    record = await load_task_record(task_id)
    meta = await read_task_meta(task_id)
    if (record or {}).get("kind") == "health_check" or (meta or {}).get("type") == "health_check":
        health = await load_health_check_task(task_id)
        latest = max(
            (item for item in (record, meta, health) if isinstance(item, dict)),
            key=lambda item: str(item.get("updatedAt") or ""),
            default=None,
        )
        if latest is not record:
            return _build_task_record(latest, None)
    return record or (_build_task_record(meta, None) if meta else None)
