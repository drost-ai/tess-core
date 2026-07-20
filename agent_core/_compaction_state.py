from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .storage import loadCompactionManifest


def isCompactionStateNewer(candidate: dict[str, Any] | None, baseline: dict[str, Any] | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    if not isinstance(baseline, dict):
        return True

    candidate_generation = _safe_int(candidate.get("latestCompactionGeneration"))
    baseline_generation = _safe_int(baseline.get("latestCompactionGeneration"))
    if candidate_generation != baseline_generation:
        return candidate_generation > baseline_generation

    candidate_at = _parse_datetime(candidate.get("latestCompactionAt"))
    baseline_at = _parse_datetime(baseline.get("latestCompactionAt"))
    if candidate_at is None:
        return False
    if baseline_at is None:
        return True
    return candidate_at > baseline_at


async def refreshCompactionStateFromManifest(session: dict[str, Any]) -> bool:
    try:
        manifest = await loadCompactionManifest(session["id"])
    except Exception:
        return False
    if not isinstance(manifest, dict):
        return False

    archive_message_count = _safe_int(manifest.get("archiveMessageCount"))
    if archive_message_count > len(session.get("messages") or []):
        return False

    candidate = _compaction_state_from_manifest(manifest)
    metadata = session.setdefault("metadata", {})
    if not isCompactionStateNewer(candidate, metadata.get("compactionState")):
        return False

    metadata["compactionState"] = candidate
    metadata["compactions"] = max(
        _safe_int(metadata.get("compactions")),
        _safe_int(manifest.get("latestGeneration")),
    )
    return True


def _compaction_state_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "latestCompactionId": manifest.get("latestCompactionId"),
        "latestCompactionGeneration": _safe_int(manifest.get("latestGeneration")),
        "latestCompactionAt": manifest.get("updatedAt"),
        "compactedThroughMessageIndex": _safe_int(manifest.get("compactedThroughMessageIndex")),
        "compactedMessageCount": _safe_int(manifest.get("compactedMessageCount")),
        "recentMessageCount": _safe_int(manifest.get("recentMessageCount")),
        "archiveMessageCount": _safe_int(manifest.get("archiveMessageCount")),
        "rawTokenEstimate": _safe_int(manifest.get("rawTokenEstimate")),
        "compactedContextTokenEstimate": _safe_int(manifest.get("compactedContextTokenEstimate")),
        "workingContextTokenEstimate": _safe_int(manifest.get("workingContextTokenEstimate")),
        "workingContextTokenMeasurement": manifest.get("workingContextTokenMeasurement"),
        "lastTrigger": manifest.get("lastTrigger"),
        "qualityClassification": manifest.get("qualityClassification"),
        "compactionModel": manifest.get("compactionModel"),
        "verificationModel": manifest.get("verificationModel"),
        "verification": manifest.get("verification"),
        "artifacts": manifest.get("artifacts") or {},
    }


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
