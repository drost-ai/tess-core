from __future__ import annotations

from ...session.persisted_record_scanner import listPersistedRecordIssues
from ...storage import getStateRoot
from typing import Any, Callable


async def _report_persisted_record_issues(
    emit: Callable[[str, dict[str, Any], str | None], Any],
    reported: set[str],
) -> None:
    for issue in listPersistedRecordIssues(None, getStateRoot()):
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
