from __future__ import annotations

from ...storage.persisted_record_scanner import list_persisted_record_issues
from ...storage import get_state_root
from typing import Any, Callable


async def _report_persisted_record_issues(
    emit: Callable[[str, dict[str, Any], str | None], Any],
    reported: set[str],
) -> None:
    for issue in list_persisted_record_issues(None, get_state_root()):
        key = ":".join(
            str(value)
            for value in (
                issue.get("family"),
                issue.get("filePath"),
                issue.get("reason"),
                issue.get("size", ""),
            )
        )
        if key in reported:
            continue
        if len(reported) >= 256:
            reported.pop()
        reported.add(key)
        await emit(
            "storage.record_unavailable",
            {
                "record_family": issue.get("family"),
                "record_id": issue.get("id"),
                "record_path": issue.get("filePath"),
                "reason": issue.get("reason"),
                "size": issue.get("size"),
                "detail": issue.get("detail"),
                "unavailable": True,
            },
        )
