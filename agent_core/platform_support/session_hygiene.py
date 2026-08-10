from __future__ import annotations

from .._utils import is_process_alive as _is_process_alive

import asyncio
import copy
import datetime
import hashlib
import json
import os
import re
from typing import Any

from ..storage import (
    MAX_SHALLOW_SESSION_BYTES,
    fileLock,
    getStateRoot,
    listLiveDaemonStates,
    loadSessionRecordShallow,
    readJsonFileTolerant,
    sessionPath,
    sessionTranscriptLogPath,
    writeJsonFile,
)
from ..tasks.supervisor_parts.live_work import (
    _resolve_session_live_work,
    _session_has_long_lived_work,
)


_SESSION_FILE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$",
    re.IGNORECASE,
)
_SESSION_BODY_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.messages\.jsonl$",
    re.IGNORECASE,
)
_ARCHIVE_TAIL_BYTES = 1024 * 1024
_MAX_SAMPLES = 100
_MAX_STORE_ENTRIES = 100_000


async def buildSessionHygieneReport(options: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    options = {**(options or {}), **kwargs}
    sample_limit = max(1, min(int(options.get("sampleLimit") or _MAX_SAMPLES), _MAX_SAMPLES))
    directory = os.path.join(getStateRoot(), "sessions")
    report: dict[str, Any] = {
        "sessionCount": 0,
        "storeBytes": 0,
        "entriesScanned": 0,
        "reportTruncated": False,
        "markerSessionCount": 0,
        "liveSessionCount": 0,
        "staleMarkerSessionCount": 0,
        "markerSessionIds": [],
        "liveSessionIds": [],
        "staleMarkerSessionIds": [],
        "deadRunningLikeRecordIds": [],
        "damagedTimestampCount": 0,
        "damagedTimestamps": [],
        "archiveTimestampDisagreementCount": 0,
        "archiveTimestampDisagreements": [],
        "unprovableArchiveTimestampCount": 0,
        "unprovableArchiveTimestamps": [],
        "foreignEntries": [],
        "malformedRecords": [],
        "oversizedRecords": [],
        "corruptBackups": [],
        "strayTempFiles": [],
        "lockFiles": [],
        "archiveReads": 0,
        "archiveBytesRead": 0,
        "sampleLimit": sample_limit,
    }
    try:
        entries, truncated = _bounded_directory_entries(directory)
        report["reportTruncated"] = truncated
    except FileNotFoundError:
        return report
    except OSError as exc:
        report["storeError"] = f"{type(exc).__name__}: {exc}"
        return report

    for entry in entries:
        report["entriesScanned"] += 1
        report["storeBytes"] += _bounded_entry_size(entry.path, _MAX_STORE_ENTRIES - report["entriesScanned"])
        if not entry.is_file(follow_symlinks=False):
            continue
        name = entry.name
        if ".corrupt-" in name:
            _sample(report["corruptBackups"], name, sample_limit)
            continue
        if name.endswith(".lock"):
            _sample(report["lockFiles"], name, sample_limit)
            continue
        if ".tmp" in name or name.endswith(".temp"):
            _sample(report["strayTempFiles"], name, sample_limit)
            continue
        if name.endswith(".messages.jsonl"):
            if not _SESSION_BODY_UUID_RE.fullmatch(name):
                _sample(report["foreignEntries"], name, sample_limit)
            continue
        if not name.endswith(".json"):
            _sample(report["foreignEntries"], name, sample_limit)
            continue
        if not _SESSION_FILE_UUID_RE.fullmatch(name):
            _sample(report["foreignEntries"], name, sample_limit)
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError as exc:
            _sample(report["malformedRecords"], {"name": name, "reason": type(exc).__name__}, sample_limit)
            continue
        session_id = name[:-5]
        if size > MAX_SHALLOW_SESSION_BYTES:
            _sample(report["oversizedRecords"], {"name": name, "bytes": size}, sample_limit)
            continue
        record = await loadSessionRecordShallow(session_id)
        if record is None:
            _sample(report["malformedRecords"], {"name": name, "bytes": size, "reason": "invalid_json_or_schema"}, sample_limit)
            continue
        report["sessionCount"] += 1

        evidence = _timestamp_evidence(record)
        if evidence["archiveInspected"]:
            report["archiveReads"] += 1
            report["archiveBytesRead"] += evidence["archiveBytesRead"]
        if evidence.get("disagreement"):
            report["archiveTimestampDisagreementCount"] += 1
            _sample(report["archiveTimestampDisagreements"], evidence["disagreement"], sample_limit)
        if evidence.get("unprovable"):
            report["unprovableArchiveTimestampCount"] += 1
            _sample(report["unprovableArchiveTimestamps"], evidence["unprovable"], sample_limit)
        if evidence.get("damage"):
            report["damagedTimestampCount"] += 1
            _sample(report["damagedTimestamps"], evidence["damage"], sample_limit)

        if not _session_has_long_lived_work(record):
            continue
        report["markerSessionCount"] += 1
        _sample(report["markerSessionIds"], record["id"], sample_limit)
        verdict = await _resolve_session_live_work(record, reconcile=False)
        for item in verdict["reconciled"]:
            _sample(report["deadRunningLikeRecordIds"], item, sample_limit)
        if verdict["live"]:
            report["liveSessionCount"] += 1
            _sample(report["liveSessionIds"], record["id"], sample_limit)
        else:
            report["staleMarkerSessionCount"] += 1
            _sample(report["staleMarkerSessionIds"], record["id"], sample_limit)

    report["damagedTimestamps"].sort(key=lambda item: item["sessionId"])
    return report


async def repairSessionTimestamps(options: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    options = {**(options or {}), **kwargs}
    apply = options.get("apply") is True
    if apply and options.get("allowWithDaemonRunning") is not True:
        live_daemons = await listLiveDaemonStates()
        if live_daemons:
            return {
                "planned": [],
                "applied": False,
                "appliedRepairs": [],
                "invalidated": [],
                "refusedDaemonRunning": True,
            }

    planned = await _collect_repair_candidates()
    result = {
        "planned": planned,
        "applied": apply,
        "appliedRepairs": [],
        "invalidated": [],
        "refusedDaemonRunning": False,
    }
    if not apply:
        return result

    # Give concurrent writers a scheduling boundary before each candidate is
    # re-read and proven under its record lock.
    await asyncio.sleep(0)
    for candidate in planned:
        path = sessionPath(candidate["sessionId"])
        async with fileLock(path):
            before_raw = _read_raw(path)
            if before_raw is None or _sha256(before_raw) != candidate["beforeHash"]:
                result["invalidated"].append({"sessionId": candidate["sessionId"], "reason": "record_changed"})
                continue
            current = await readJsonFileTolerant(path, None)
            if not isinstance(current, dict):
                result["invalidated"].append({"sessionId": candidate["sessionId"], "reason": "record_unavailable"})
                continue
            current_evidence = _timestamp_evidence(current)
            damage = current_evidence.get("damage")
            if not damage or not _same_damage(candidate, damage):
                result["invalidated"].append({"sessionId": candidate["sessionId"], "reason": "evidence_changed"})
                continue
            repaired = copy.deepcopy(current)
            repaired["updatedAt"] = damage["repairedUpdatedAt"]
            log = repaired.setdefault("metadata", {}).setdefault("fullTranscriptLog", {})
            log["lastArchivedAt"] = damage["archiveLastPersistedAt"]
            await writeJsonFile(path, repaired)
            after_raw = _read_raw(path) or b""
            result["appliedRepairs"].append(
                {
                    **damage,
                    "beforeHash": _sha256(before_raw),
                    "afterHash": _sha256(after_raw),
                }
            )
    return result


async def _collect_repair_candidates() -> list[dict[str, Any]]:
    directory = os.path.join(getStateRoot(), "sessions")
    try:
        entries, _truncated = _bounded_directory_entries(directory)
    except OSError:
        return []
    planned: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".json"):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_size > MAX_SHALLOW_SESSION_BYTES:
                continue
        except OSError:
            continue
        record = await loadSessionRecordShallow(entry.name[:-5])
        if not record:
            continue
        evidence = _timestamp_evidence(record)
        if evidence.get("damage"):
            planned.append(evidence["damage"])
    return sorted(planned, key=lambda item: item["sessionId"])


def _timestamp_evidence(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    full_log = metadata.get("fullTranscriptLog") if isinstance(metadata.get("fullTranscriptLog"), dict) else {}
    stamped = full_log.get("lastArchivedAt")
    if not isinstance(stamped, str) or not stamped:
        return {"archiveInspected": False, "archiveBytesRead": 0}
    archive = _read_archive_tail(sessionTranscriptLogPath(record["id"]))
    result: dict[str, Any] = {
        "archiveInspected": True,
        "archiveBytesRead": archive["bytesRead"],
    }
    actual = archive.get("latestPersistedAt")
    if not actual:
        result["unprovable"] = {"sessionId": record["id"], "reason": archive.get("reason") or "no_valid_persisted_at"}
        return result
    stamped_dt = _parse_iso(stamped)
    actual_dt = _parse_iso(actual)
    if stamped_dt is None or actual_dt is None:
        result["unprovable"] = {"sessionId": record["id"], "reason": "invalid_timestamp"}
        return result
    if stamped != actual:
        result["disagreement"] = {
            "sessionId": record["id"],
            "stampedLastArchivedAt": stamped,
            "archiveLastPersistedAt": actual,
        }
    if stamped_dt <= actual_dt:
        return result
    if record.get("updatedAt") != stamped:
        result["unprovable"] = {
            "sessionId": record["id"],
            "reason": "newer_or_ambiguous_semantic_activity",
            "updatedAt": record.get("updatedAt"),
            "stampedLastArchivedAt": stamped,
            "archiveLastPersistedAt": actual,
        }
        return result
    created = _parse_iso(record.get("createdAt"))
    repaired = actual if created is None or actual_dt >= created else record["createdAt"]
    raw = _read_raw(sessionPath(record["id"])) or b""
    result["damage"] = {
        "sessionId": record["id"],
        "updatedAt": record.get("updatedAt"),
        "stampedLastArchivedAt": stamped,
        "archiveLastPersistedAt": actual,
        "repairedUpdatedAt": repaired,
        "evidenceKind": "append_only_archive_latest_persisted_at",
        "fields": ["updatedAt", "metadata.fullTranscriptLog.lastArchivedAt"],
        "beforeHash": _sha256(raw),
    }
    return result


def _read_archive_tail(path: str) -> dict[str, Any]:
    try:
        size = os.path.getsize(path)
        start = max(0, size - _ARCHIVE_TAIL_BYTES)
        with open(path, "rb") as handle:
            handle.seek(start)
            raw = handle.read(_ARCHIVE_TAIL_BYTES)
    except FileNotFoundError:
        return {"bytesRead": 0, "reason": "archive_missing"}
    except OSError as exc:
        return {"bytesRead": 0, "reason": f"archive_unavailable:{type(exc).__name__}"}
    if start > 0:
        newline = raw.find(b"\n")
        if newline < 0:
            return {"bytesRead": len(raw), "reason": "archive_tail_entry_oversized"}
        raw = raw[newline + 1 :]
    latest: tuple[datetime.datetime, str] | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        value = entry.get("persisted_at") if isinstance(entry, dict) else None
        parsed = _parse_iso(value)
        if parsed is not None and (latest is None or parsed >= latest[0]):
            latest = (parsed, value)
    return {"bytesRead": len(raw), "latestPersistedAt": latest[1] if latest else None}


def _bounded_entry_size(path: str, remaining: int) -> int:
    total = 0
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            return os.path.getsize(path)
        if not os.path.isdir(path) or os.path.islink(path):
            return 0
        with os.scandir(path) as children:
            for index, child in enumerate(children):
                if index >= max(0, remaining):
                    break
                if child.is_file(follow_symlinks=False):
                    try:
                        total += child.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
    except OSError:
        return total
    return total


def _bounded_directory_entries(path: str) -> tuple[list[os.DirEntry[str]], bool]:
    entries: list[os.DirEntry[str]] = []
    truncated = False
    with os.scandir(path) as scanner:
        for entry in scanner:
            if len(entries) >= _MAX_STORE_ENTRIES:
                truncated = True
                break
            entries.append(entry)
    return entries, truncated


def _parse_iso(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _read_raw(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_SHALLOW_SESSION_BYTES + 1)
    except OSError:
        return None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _same_damage(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        left.get(key) == right.get(key)
        for key in ("sessionId", "updatedAt", "stampedLastArchivedAt", "archiveLastPersistedAt", "repairedUpdatedAt", "beforeHash")
    )


def _sample(target: list[Any], value: Any, limit: int) -> None:
    if len(target) < limit and value not in target:
        target.append(value)


