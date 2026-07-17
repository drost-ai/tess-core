from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
from collections.abc import Callable, Sequence
from typing import Any


GLOBAL_RECORD_HARD_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_SCAN_ENTRIES = 4_096
DEFAULT_MAX_SCAN_BYTES = 64 * 1024 * 1024
DEFAULT_READ_CONCURRENCY = 8
MAX_SCAN_ISSUES = 256
MAX_ISSUE_REGISTRY = 512
MAX_ISSUE_DETAIL_CHARS = 240
SCALAR_READ_ATTEMPTS = 4

FAMILY_MAX_BYTES = {
    "agent": 512 * 1024,
    "bridge": 512 * 1024,
    "bridge_delivery_ledger": 4 * 1024 * 1024,
    "bridge_event_log": 4 * 1024 * 1024,
    "bridge_inbox": 4 * 1024 * 1024,
    "bridge_recovery_journal": 4 * 1024 * 1024,
    "daemon_state": 256 * 1024,
    "health_check": 512 * 1024,
    "monitor": 512 * 1024,
    "plan_approval": 512 * 1024,
    "remote_session": 512 * 1024,
    "task_meta": 1024 * 1024,
    "task_record": 1024 * 1024,
}

_issue_registry: dict[str, dict[str, Any]] = {}
_read_observer: Callable[[str, int, int], Any] | None = None
_before_open_observer: Callable[[str], Any] | None = None


async def scanPersistedJsonRecords(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    options = {**(args or {}), **kwargs}
    family = _require_family(options.get("family"))
    directory = os.path.abspath(os.fspath(options["directory"]))
    select = options["select"]
    validate = options["validate"]
    max_entries = max(1, int(options.get("maxEntries", DEFAULT_MAX_SCAN_ENTRIES)))
    max_total_bytes = max(1, int(options.get("maxTotalBytes", DEFAULT_MAX_SCAN_BYTES)))
    concurrency = max(1, int(options.get("concurrency", DEFAULT_READ_CONCURRENCY)))
    _clear_registered_issues(family, directory)
    issues: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    examined_entries = 0
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                examined_entries += 1
                if examined_entries > max_entries:
                    truncated = True
                    _add_issue(issues, _issue(
                        family,
                        directory,
                        "scan_entry_limit",
                        detail=f"Record scan stopped after {max_entries} directory entries.",
                    ))
                    break
                candidate = select(entry)
                if candidate:
                    candidates.append(_candidate(candidate))
    except FileNotFoundError:
        pass
    except Exception as exc:
        _add_issue(issues, _error_issue(family, directory, "read_failed", exc))

    candidates.sort(key=lambda item: item["filePath"])
    admitted: list[dict[str, Any]] = []
    admitted_bytes = 0
    for candidate in candidates:
        try:
            current = os.lstat(candidate["filePath"])
        except FileNotFoundError:
            continue
        except Exception as exc:
            _add_issue(issues, _error_issue(
                family,
                candidate["filePath"],
                "read_failed",
                exc,
                candidate.get("id"),
            ))
            continue
        if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
            _add_issue(issues, _issue(
                family,
                candidate["filePath"],
                "not_regular_file",
                record_id=candidate.get("id"),
                size=current.st_size,
            ))
            continue
        max_record_bytes = persistedRecordMaxBytes(family)
        if current.st_size > max_record_bytes:
            _add_issue(issues, _record_too_large(family, candidate, current.st_size, max_record_bytes))
            continue
        if admitted_bytes + current.st_size > max_total_bytes:
            truncated = True
            _add_issue(issues, _issue(
                family,
                candidate["filePath"],
                "scan_byte_limit",
                record_id=candidate.get("id"),
                size=current.st_size,
                detail=f"Record omitted after the {max_total_bytes}-byte scan budget was exhausted.",
            ))
            continue
        admitted_bytes += current.st_size
        admitted.append({**candidate, "_stats": current})

    results = await _map_with_concurrency(
        admitted,
        concurrency,
        lambda candidate: asyncio.to_thread(_read_candidate_sync, family, candidate, validate),
    )
    records: list[Any] = []
    for result in results:
        if "record" in result:
            records.append(result["record"])
            _issue_registry.pop(result["filePath"], None)
        elif result.get("issue"):
            _add_issue(issues, result["issue"])
    return {
        "records": records,
        "issues": issues,
        "examinedEntries": examined_entries,
        "admittedBytes": admitted_bytes,
        "truncated": truncated,
    }


async def readPersistedJsonRecord(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    options = {**(args or {}), **kwargs}
    family = _require_family(options.get("family"))
    candidate = _candidate({"id": options.get("id"), "filePath": options["filePath"]})
    final_issue: dict[str, Any] | None = None
    for attempt in range(SCALAR_READ_ATTEMPTS):
        try:
            current = os.lstat(candidate["filePath"])
        except FileNotFoundError:
            if attempt + 1 < SCALAR_READ_ATTEMPTS and final_issue and final_issue["reason"] == "record_changed_during_read":
                await asyncio.sleep((1 << min(attempt, 3)) / 1000)
                continue
            _issue_registry.pop(candidate["filePath"], None)
            return {}
        except Exception as exc:
            final_issue = _error_issue(
                family,
                candidate["filePath"],
                "read_failed",
                exc,
                candidate.get("id"),
            )
            break
        if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
            final_issue = _issue(
                family,
                candidate["filePath"],
                "not_regular_file",
                record_id=candidate.get("id"),
                size=current.st_size,
            )
            break
        maximum = persistedRecordMaxBytes(family)
        if current.st_size > maximum:
            final_issue = _record_too_large(family, candidate, current.st_size, maximum)
            break
        result = await asyncio.to_thread(
            _read_candidate_sync,
            family,
            {**candidate, "_stats": current},
            options["validate"],
        )
        if "record" in result:
            _issue_registry.pop(candidate["filePath"], None)
            return {"record": result["record"]}
        final_issue = result.get("issue")
        if not final_issue or final_issue.get("reason") != "record_changed_during_read":
            break
        if attempt + 1 < SCALAR_READ_ATTEMPTS:
            await asyncio.sleep((1 << min(attempt, 3)) / 1000)
    if final_issue:
        _remember_issue(final_issue)
        return {"issue": dict(final_issue)}
    return {}


def listPersistedRecordIssues(
    families: Sequence[str] | None = None,
    root: str | None = None,
) -> list[dict[str, Any]]:
    selected = set(families) if families is not None else None
    normalized_root = os.path.abspath(root) if root else None
    return [
        dict(issue)
        for issue in _issue_registry.values()
        if (selected is None or issue["family"] in selected)
        and (normalized_root is None or _within_root(normalized_root, issue["filePath"]))
    ]


def getPersistedRecordIssue(filePath: str) -> dict[str, Any] | None:
    issue = _issue_registry.get(os.path.abspath(filePath))
    return dict(issue) if issue else None


def setPersistedRecordReadObserverForTesting(observer: Callable[[str, int, int], Any] | None) -> None:
    global _read_observer
    _read_observer = observer


def setPersistedRecordBeforeOpenObserverForTesting(observer: Callable[[str], Any] | None) -> None:
    global _before_open_observer
    _before_open_observer = observer


def persistedRecordMaxBytes(family: str) -> int:
    return min(FAMILY_MAX_BYTES[_require_family(family)], GLOBAL_RECORD_HARD_MAX_BYTES)


def _read_candidate_sync(family: str, candidate: dict[str, Any], validate: Callable[..., bool]) -> dict[str, Any]:
    file_path = candidate["filePath"]
    expected = candidate["_stats"]
    descriptor: int | None = None
    try:
        if _before_open_observer:
            _before_open_observer(file_path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(file_path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            return _result_issue(_issue(
                family,
                file_path,
                "record_changed_during_read",
                record_id=candidate.get("id"),
                size=opened.st_size,
            ))
        maximum = persistedRecordMaxBytes(family)
        if opened.st_size > maximum:
            return _result_issue(_record_too_large(family, candidate, opened.st_size, maximum))
        requested = min(maximum + 1, max(1, opened.st_size + 1))
        chunks: list[bytes] = []
        total = 0
        while total < requested:
            chunk = os.read(descriptor, requested - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if _read_observer:
            _read_observer(file_path, requested, total)
        if total > maximum:
            return _result_issue(_record_too_large(family, candidate, total, maximum))
        try:
            value = json.loads(b"".join(chunks))
        except Exception as exc:
            return _result_issue(_error_issue(
                family,
                file_path,
                "malformed_json",
                exc,
                candidate.get("id"),
                total,
            ))
        try:
            valid = bool(validate(value, {key: candidate.get(key) for key in ("id", "filePath")}))
        except Exception as exc:
            return _result_issue(_error_issue(
                family,
                file_path,
                "invalid_record",
                exc,
                candidate.get("id"),
                total,
            ))
        if not valid:
            return _result_issue(_issue(
                family,
                file_path,
                "invalid_record",
                record_id=candidate.get("id"),
                size=total,
                detail="Record failed its minimum identity/schema validation.",
            ))
        return {"record": value, "filePath": file_path}
    except FileNotFoundError:
        return _result_issue(_issue(
            family,
            file_path,
            "record_changed_during_read",
            record_id=candidate.get("id"),
            size=expected.st_size,
        ))
    except OSError as exc:
        reason = "record_changed_during_read" if exc.errno == errno.ENOENT else "read_failed"
        return _result_issue(_error_issue(
            family,
            file_path,
            reason,
            exc,
            candidate.get("id"),
            expected.st_size,
        ))
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


async def _map_with_concurrency(
    values: list[Any],
    concurrency: int,
    worker: Callable[[Any], Any],
) -> list[Any]:
    output: list[Any] = [None] * len(values)
    cursor = 0

    async def consume() -> None:
        nonlocal cursor
        while cursor < len(values):
            index = cursor
            cursor += 1
            output[index] = await worker(values[index])

    await asyncio.gather(*(consume() for _ in range(min(len(values), concurrency))))
    return output


def _candidate(value: dict[str, Any]) -> dict[str, Any]:
    result = {"filePath": os.path.abspath(os.fspath(value["filePath"]))}
    if value.get("id") is not None:
        result["id"] = value["id"]
    return result


def _require_family(family: Any) -> str:
    if family not in FAMILY_MAX_BYTES:
        raise ValueError(f"Unsupported persisted record family: {family}")
    return str(family)


def _result_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {"issue": issue, "filePath": issue["filePath"]}


def _record_too_large(family: str, candidate: dict[str, Any], size: int, maximum: int) -> dict[str, Any]:
    return _issue(
        family,
        candidate["filePath"],
        "record_too_large",
        record_id=candidate.get("id"),
        size=size,
        detail=f"Record is {size} bytes; the {family} limit is {maximum} bytes.",
    )


def _error_issue(
    family: str,
    file_path: str,
    reason: str,
    error: BaseException,
    record_id: Any = None,
    size: int | None = None,
) -> dict[str, Any]:
    return _issue(family, file_path, reason, record_id=record_id, size=size, detail=str(error))


def _issue(
    family: str,
    file_path: str,
    reason: str,
    *,
    record_id: Any = None,
    size: int | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    issue = {
        "family": family,
        "filePath": os.path.abspath(file_path),
        "reason": reason,
        "unavailable": True,
    }
    if record_id is not None:
        issue["id"] = record_id
    if size is not None:
        issue["size"] = size
    bounded = _bounded_detail(detail)
    if bounded:
        issue["detail"] = bounded
    return issue


def _add_issue(issues: list[dict[str, Any]], issue: dict[str, Any]) -> None:
    _remember_issue(issue)
    if len(issues) < MAX_SCAN_ISSUES:
        issues.append(dict(issue))


def _remember_issue(issue: dict[str, Any]) -> None:
    remembered = dict(issue)
    detail = _bounded_detail(remembered.get("detail"))
    if detail:
        remembered["detail"] = detail
    else:
        remembered.pop("detail", None)
    file_path = remembered["filePath"]
    if file_path not in _issue_registry and len(_issue_registry) >= MAX_ISSUE_REGISTRY:
        _issue_registry.pop(next(iter(_issue_registry)))
    _issue_registry[file_path] = remembered


def _bounded_detail(detail: Any) -> str | None:
    if not detail:
        return None
    normalized = " ".join(str(detail).replace("\t", " ").splitlines()).strip()
    return normalized if len(normalized) <= MAX_ISSUE_DETAIL_CHARS else f"{normalized[:MAX_ISSUE_DETAIL_CHARS - 1]}…"


def _within_root(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, os.path.abspath(candidate))) == root
    except ValueError:
        return False


def _clear_registered_issues(family: str, directory: str) -> None:
    for file_path, issue in list(_issue_registry.items()):
        if issue["family"] == family and _within_root(directory, file_path):
            _issue_registry.pop(file_path, None)


scan_persisted_json_records = scanPersistedJsonRecords
read_persisted_json_record = readPersistedJsonRecord
list_persisted_record_issues = listPersistedRecordIssues
get_persisted_record_issue = getPersistedRecordIssue
persisted_record_max_bytes = persistedRecordMaxBytes


__all__ = [
    "DEFAULT_MAX_SCAN_BYTES",
    "DEFAULT_MAX_SCAN_ENTRIES",
    "DEFAULT_READ_CONCURRENCY",
    "FAMILY_MAX_BYTES",
    "GLOBAL_RECORD_HARD_MAX_BYTES",
    "getPersistedRecordIssue",
    "get_persisted_record_issue",
    "listPersistedRecordIssues",
    "list_persisted_record_issues",
    "persistedRecordMaxBytes",
    "persisted_record_max_bytes",
    "readPersistedJsonRecord",
    "read_persisted_json_record",
    "scanPersistedJsonRecords",
    "scan_persisted_json_records",
    "setPersistedRecordBeforeOpenObserverForTesting",
    "setPersistedRecordReadObserverForTesting",
]
