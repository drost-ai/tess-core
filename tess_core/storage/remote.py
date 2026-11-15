from __future__ import annotations

import json
import os
import re
from typing import Any

from .json_io import with_record_transaction, write_json_file
from .paths import get_state_root
from .records import omit_none_fields
from .persisted_record_scanner import read_persisted_json_record, scan_persisted_json_records
from .persisted_record_validation import is_bridge_record, is_remote_session_record
from ..runtime.identifiers import (
    parse_bridge_id,
    parse_remote_session_id,
    parse_session_id,
    try_parse_bridge_id,
    try_parse_remote_session_id,
)

BRIDGE_LOG_MAX_ENTRIES = 4_096
BRIDGE_LEDGER_MAX_ENTRIES = 8_192
BRIDGE_STORAGE_MAX_BYTES = 4 * 1024 * 1024


class BridgeStorageError(RuntimeError):
    code = "bridge.storage_unavailable"

    def __init__(self, message: str, issue: dict[str, Any] | None = None):
        super().__init__(message)
        self.issue = issue

def remote_session_path(remoteSessionId: str) -> str:
    return os.path.join(
        get_state_root(),
        "remote-sessions",
        f"{parse_remote_session_id(remoteSessionId)}.json",
    )

async def save_remote_session(record: dict[str, Any]) -> None:
    await write_json_file(remote_session_path(record["id"]), omit_none_fields(record))

async def load_remote_session(remoteSessionId: str) -> dict[str, Any] | None:
    remote_id = parse_remote_session_id(remoteSessionId)
    result = await read_persisted_json_record(
        family="remote_session",
        id=remote_id,
        filePath=remote_session_path(remote_id),
        validate=is_remote_session_record,
    )
    return result.get("record")

async def list_remote_sessions() -> list[dict[str, Any]]:
    directory = os.path.join(get_state_root(), "remote-sessions")
    scan = await scan_persisted_json_records(
        family="remote_session",
        directory=directory,
        select=lambda entry: _remote_json_candidate(directory, entry, try_parse_remote_session_id),
        validate=is_remote_session_record,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

def bridge_path(bridgeId: str) -> str:
    return os.path.join(get_state_root(), "bridges", f"{parse_bridge_id(bridgeId)}.json")

def bridge_inbox_path(bridgeId: str) -> str:
    return os.path.join(get_state_root(), "bridges", f"{parse_bridge_id(bridgeId)}.inbox.json")

def bridge_events_path(bridgeId: str) -> str:
    return os.path.join(get_state_root(), "bridges", f"{parse_bridge_id(bridgeId)}.events.json")

def bridge_ledger_path(bridgeId: str) -> str:
    return os.path.join(get_state_root(), "bridges", f"{parse_bridge_id(bridgeId)}.ledger.json")

def bridge_recovery_path(sessionId: str) -> str:
    return os.path.join(get_state_root(), "bridge-recovery", f"{parse_session_id(sessionId)}.json")

async def save_bridge(record: dict[str, Any]) -> None:
    """Save a remote bridge record."""
    _assert_serialized_bound(record, "bridge record", 512 * 1024)
    await write_json_file(bridge_path(record["id"]), omit_none_fields(record))

async def load_bridge(bridgeId: str) -> dict[str, Any] | None:
    """Load a remote bridge record, or ``None`` when it does not exist. A damaged record is an error."""
    bridge_id = parse_bridge_id(bridgeId)
    result = await read_persisted_json_record(
        family="bridge",
        id=bridge_id,
        filePath=bridge_path(bridge_id),
        validate=is_bridge_record,
    )
    issue = result.get("issue")
    if issue:
        raise BridgeStorageError(f"bridge {bridge_id} is unavailable: {issue['reason']}.", issue)
    return result.get("record")

async def list_bridges() -> list[dict[str, Any]]:
    directory = os.path.join(get_state_root(), "bridges")
    def select(entry: os.DirEntry[str]) -> dict[str, str] | None:
        if not entry.name.endswith(".json") or entry.name.endswith(
            (".inbox.json", ".events.json", ".ledger.json")
        ):
            return None
        return _remote_json_candidate(directory, entry, try_parse_bridge_id)

    scan = await scan_persisted_json_records(
        family="bridge",
        directory=directory,
        select=select,
        validate=is_bridge_record,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

async def load_bridge_inbox(bridgeId: str) -> list[dict[str, Any]]:
    bridge_id = parse_bridge_id(bridgeId)
    return await _load_bridge_array(
        family="bridge_inbox",
        bridge_id=bridge_id,
        file_path=bridge_inbox_path(bridge_id),
        validate_entry=lambda value: _is_bridge_inbox_message(value, bridge_id),
    )

async def append_bridge_inbox_message(message: dict[str, Any]) -> None:
    bridge_id = parse_bridge_id(message["bridgeId"])
    if not _is_bridge_inbox_message(message, bridge_id):
        raise BridgeStorageError("Bridge inbox message is invalid.")
    async with with_record_transaction("bridge-inbox", bridge_id):
        existing = await load_bridge_inbox(bridge_id)
        same_id = next((entry for entry in existing if entry.get("id") == message["id"]), None)
        if same_id is not None:
            if same_id == omit_none_fields(message):
                return
            raise BridgeStorageError(
                f"Bridge inbox message id {message['id']} conflicts with its persisted record."
            )
        key = message.get("idempotencyKey")
        same_key = next((entry for entry in existing if key and entry.get("idempotencyKey") == key), None)
        if same_key is not None:
            raise BridgeStorageError(
                f"Bridge inbox idempotency key {key} already belongs to message {same_key['id']}."
            )
        if len(existing) >= BRIDGE_LOG_MAX_ENTRIES:
            raise BridgeStorageError(f"Bridge inbox exceeds {BRIDGE_LOG_MAX_ENTRIES} entries.")
        value = omit_none_fields([*existing, message])
        _assert_serialized_bound(value, "bridge inbox", BRIDGE_STORAGE_MAX_BYTES)
        await write_json_file(bridge_inbox_path(bridge_id), value)

async def replace_bridge_inbox(bridgeId: str, messages: list[dict[str, Any]]) -> None:
    bridge_id = parse_bridge_id(bridgeId)
    if len(messages) > BRIDGE_LOG_MAX_ENTRIES or not all(
        _is_bridge_inbox_message(message, bridge_id) for message in messages
    ):
        raise BridgeStorageError("Bridge inbox is invalid or exceeds 4096 entries.")
    keys = [message.get("idempotencyKey") for message in messages if message.get("idempotencyKey")]
    if len(keys) != len(set(keys)):
        raise BridgeStorageError("Bridge inbox contains duplicate idempotency keys.")
    value = omit_none_fields(messages)
    _assert_serialized_bound(value, "bridge inbox", BRIDGE_STORAGE_MAX_BYTES)
    await write_json_file(bridge_inbox_path(bridge_id), value)

async def load_bridge_events(bridgeId: str) -> list[dict[str, Any]]:
    """The recorded events of a remote bridge, oldest first."""
    bridge_id = parse_bridge_id(bridgeId)
    return await _load_bridge_array(
        family="bridge_event_log",
        bridge_id=bridge_id,
        file_path=bridge_events_path(bridge_id),
        validate_entry=lambda value: _is_bridge_event_record(value, bridge_id),
    )

async def append_bridge_event(record: dict[str, Any]) -> None:
    bridge_id = parse_bridge_id(record["bridgeId"])
    if not _is_bridge_event_record(record, bridge_id):
        raise BridgeStorageError("Bridge event record is invalid.")
    async with with_record_transaction("bridge-events", bridge_id):
        existing = await load_bridge_events(bridge_id)
        same_id = next((entry for entry in existing if entry.get("id") == record["id"]), None)
        if same_id is not None:
            if same_id == omit_none_fields(record):
                return
            raise BridgeStorageError(
                f"Bridge event id {record['id']} conflicts with its persisted record."
            )
        if len(existing) >= BRIDGE_LOG_MAX_ENTRIES:
            raise BridgeStorageError(f"Bridge event log exceeds {BRIDGE_LOG_MAX_ENTRIES} entries.")
        value = omit_none_fields([*existing, record])
        _assert_serialized_bound(value, "bridge event log", BRIDGE_STORAGE_MAX_BYTES)
        await write_json_file(bridge_events_path(bridge_id), value)

async def load_bridge_delivery_ledger(bridgeId: str) -> dict[str, Any] | None:
    bridge_id = parse_bridge_id(bridgeId)
    result = await read_persisted_json_record(
        family="bridge_delivery_ledger",
        id=bridge_id,
        filePath=bridge_ledger_path(bridge_id),
        validate=lambda value, _candidate: _is_bridge_delivery_ledger(value, bridge_id),
    )
    issue = result.get("issue")
    if issue:
        raise BridgeStorageError(
            f"bridge delivery ledger {bridge_id} is unavailable: {issue['reason']}.", issue
        )
    return result.get("record")

async def save_bridge_delivery_ledger(ledger: dict[str, Any]) -> None:
    bridge_id = parse_bridge_id(ledger["bridgeId"])
    if not _is_bridge_delivery_ledger(ledger, bridge_id):
        raise BridgeStorageError("Bridge delivery ledger is invalid.")
    value = omit_none_fields(ledger)
    _assert_serialized_bound(value, "bridge delivery ledger", BRIDGE_STORAGE_MAX_BYTES)
    await write_json_file(bridge_ledger_path(bridge_id), value)

async def load_bridge_recovery_record(sessionId: str) -> dict[str, Any] | None:
    session_id = parse_session_id(sessionId)
    result = await read_persisted_json_record(
        family="bridge_recovery_journal",
        id=session_id,
        filePath=bridge_recovery_path(session_id),
        validate=lambda value, _candidate: _is_bridge_recovery_record(value, session_id),
    )
    issue = result.get("issue")
    if issue:
        raise BridgeStorageError(
            f"bridge recovery journal {session_id} is unavailable: {issue['reason']}.", issue
        )
    return result.get("record")

async def save_bridge_recovery_record(sessionId: str, value: dict[str, Any]) -> None:
    session_id = parse_session_id(sessionId)
    if not _is_bridge_recovery_record(value, session_id):
        raise BridgeStorageError("Bridge recovery journal is invalid.")
    compact = omit_none_fields(value)
    _assert_serialized_bound(compact, "bridge recovery journal", BRIDGE_STORAGE_MAX_BYTES)
    await write_json_file(bridge_recovery_path(session_id), compact)


def _append_unique_coordination_record(existing: Any, record: dict[str, Any]) -> dict[str, Any]:
    records = existing if isinstance(existing, list) else []
    record_id = record.get("id")
    if record_id and any(isinstance(item, dict) and item.get("id") == record_id for item in records):
        return {"value": records, "changed": False}
    return {"value": omit_none_fields([*records, record]), "changed": True}


async def _load_bridge_array(
    *,
    family: str,
    bridge_id: str,
    file_path: str,
    validate_entry,
) -> list[dict[str, Any]]:
    result = await read_persisted_json_record(
        family=family,
        id=bridge_id,
        filePath=file_path,
        validate=lambda value, _candidate: (
            isinstance(value, list)
            and len(value) <= BRIDGE_LOG_MAX_ENTRIES
            and all(validate_entry(item) for item in value)
        ),
    )
    issue = result.get("issue")
    if issue:
        raise BridgeStorageError(f"{family} {bridge_id} is unavailable: {issue['reason']}.", issue)
    return result.get("record") or []


def _remote_json_candidate(directory: str, entry: os.DirEntry[str], parse_id) -> dict[str, str] | None:
    if not entry.name.endswith(".json"):
        return None
    record_id = parse_id(entry.name[:-5])
    if record_id is None:
        return None
    return {"id": record_id, "filePath": os.path.join(directory, entry.name)}


def _is_bridge_inbox_message(value: Any, bridge_id: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("bridgeId") == bridge_id
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and isinstance(value.get("createdAt"), str)
        and value.get("source") in {"bridge", "remote", "local"}
        and isinstance(value.get("text"), str)
        and _optional_positive_integer(value.get("generation"))
        and _optional_nonempty_string(value.get("idempotencyKey"))
    )


def _is_bridge_event_record(value: Any, bridge_id: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("bridgeId") == bridge_id
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and isinstance(value.get("createdAt"), str)
        and isinstance(value.get("event"), str)
        and bool(value["event"])
        and isinstance(value.get("data"), dict)
        and _optional_positive_integer(value.get("generation"))
        and _optional_nonempty_string(value.get("idempotencyKey"))
    )


def _is_bridge_delivery_ledger(value: Any, bridge_id: str) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        return False
    entries = value["entries"]
    keys: set[str] = set()
    if not (
        value.get("schemaVersion") == 1
        and value.get("bridgeId") == bridge_id
        and isinstance(value.get("sessionId"), str)
        and bool(value["sessionId"])
        and _positive_integer(value.get("generation"))
        and _nonnegative_integer(value.get("revision"))
        and _nonnegative_integer(value.get("cursor"))
        and value["cursor"] <= len(entries) <= BRIDGE_LEDGER_MAX_ENTRIES
        and isinstance(value.get("updatedAt"), str)
    ):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        key = entry.get("key")
        disposition = entry.get("disposition")
        if not (
            isinstance(key, str) and key and key not in keys
            and isinstance(entry.get("sourceBridgeId"), str) and entry["sourceBridgeId"]
            and isinstance(entry.get("messageId"), str) and entry["messageId"]
            and disposition in {"pending", "delivered", "legacy_confirmed", "legacy_acknowledged"}
            and isinstance(entry.get("createdAt"), str)
            and (disposition != "delivered" or isinstance(entry.get("deliveredAt"), str))
            and (entry.get("warning") is None or entry.get("warning") == "legacy_replay_at_least_once")
        ):
            return False
        keys.add(key)
    return True


def _is_bridge_recovery_record(value: Any, session_id: str) -> bool:
    if not bool(
        isinstance(value, dict)
        and value.get("schemaVersion") == 1
        and value.get("sessionId") == session_id
        and _positive_integer(value.get("revision"))
        and isinstance(value.get("sourceDigest"), str)
        and len(value["sourceDigest"]) == 64
        and value.get("phase") in {
            "blocked", "prepared", "destination_written", "inbox_written",
            "authority_verified", "pointers_published", "committed",
        }
        and isinstance(value.get("blockedReasons"), list)
        and isinstance(value.get("sourceBridgeIds"), list)
        and isinstance(value.get("remoteSessionIds"), list)
        and isinstance(value.get("pointerClaims"), list)
        and isinstance(value.get("pointerBaselines"), list)
        and bool(value["pointerBaselines"])
        and isinstance(value.get("authorityDirectoryManifests"), list)
        and len(value["authorityDirectoryManifests"]) == 2
        and isinstance(value.get("messagePlans"), list)
        and isinstance(value.get("immutableSourceFiles"), list)
        and isinstance(value.get("destinationCwd"), str)
        and bool(value["destinationCwd"])
        and isinstance(value.get("createdAt"), str)
        and isinstance(value.get("updatedAt"), str)
    ):
        return False
    if not all(_sha256(value_) for value_ in value["authorityDirectoryManifests"]):
        return False
    if value.get("selectedLegacyBridgeId") is not None and try_parse_bridge_id(value["selectedLegacyBridgeId"]) is None:
        return False
    destination_id = value.get("destinationBridgeId")
    destination_generation = value.get("destinationGeneration")
    if (destination_id is None) != (destination_generation is None):
        return False
    if destination_id is not None and (try_parse_bridge_id(destination_id) is None or not _positive_integer(destination_generation)):
        return False
    if value.get("destinationRemoteSessionId") is not None and try_parse_remote_session_id(value["destinationRemoteSessionId"]) is None:
        return False
    if value.get("warning") not in (None, "legacy_replay_at_least_once"):
        return False
    if not _unique_valid_ids(value["sourceBridgeIds"], try_parse_bridge_id):
        return False
    if not _unique_valid_ids(value["remoteSessionIds"], try_parse_remote_session_id):
        return False
    plans = value["messagePlans"]
    plan_keys = [plan.get("key") for plan in plans if isinstance(plan, dict)]
    if len(plan_keys) != len(plans) or len(plan_keys) != len(set(plan_keys)):
        return False
    if not all(_valid_recovery_plan(plan) for plan in plans):
        return False
    baselines = value["pointerBaselines"]
    baseline_keys = [f"{item.get('owner')}:{item.get('ownerId')}" for item in baselines if isinstance(item, dict)]
    if len(baseline_keys) != len(baselines) or len(baseline_keys) != len(set(baseline_keys)):
        return False
    if not all(_valid_pointer_baseline(item, session_id) for item in baselines):
        return False
    if sum(1 for item in baselines if item.get("owner") == "session" and item.get("ownerId") == session_id) != 1:
        return False
    sources = value["immutableSourceFiles"]
    source_paths = [item.get("path") for item in sources if isinstance(item, dict)]
    if len(source_paths) != len(sources) or len(source_paths) != len(set(source_paths)):
        return False
    if sum(int(item.get("size") or 0) for item in sources) > 64 * 1024 * 1024:
        return False
    if not all(_valid_recovery_source(item) for item in sources):
        return False
    result = value.get("result")
    if value["phase"] == "committed":
        if not _valid_recovery_result(result):
            return False
    elif result is not None:
        return False
    return True


def _valid_recovery_plan(plan: Any) -> bool:
    return bool(
        isinstance(plan, dict) and isinstance(plan.get("key"), str) and plan["key"]
        and try_parse_bridge_id(plan.get("sourceBridgeId")) is not None
        and isinstance(plan.get("messageId"), str) and plan["messageId"]
        and plan.get("disposition") in {"pending", "delivered", "legacy_confirmed", "legacy_acknowledged"}
        and isinstance(plan.get("createdAt"), str)
        and plan.get("warning") in (None, "legacy_replay_at_least_once")
    )


def _valid_pointer_baseline(item: Any, session_id: str) -> bool:
    if not isinstance(item, dict) or item.get("owner") not in {"session", "remote_session"}:
        return False
    owner_id = item.get("ownerId")
    valid_owner = owner_id == session_id if item["owner"] == "session" else try_parse_remote_session_id(owner_id) is not None
    return bool(valid_owner and _sha256(item.get("authorityDigest")) and _sha256(item.get("normalizedRecordDigest")))


def _valid_recovery_source(item: Any) -> bool:
    if not isinstance(item, dict) or not os.path.isabs(str(item.get("path") or "")):
        return False
    path = os.path.realpath(item["path"])
    root = os.path.realpath(get_state_root())
    if os.path.dirname(path) not in {os.path.join(root, name) for name in ("sessions", "remote-sessions", "bridges")}:
        return False
    return bool(
        _sha256(item.get("digest")) and _nonnegative_integer(item.get("size"))
        and _nonnegative_integer(item.get("dev")) and _nonnegative_integer(item.get("ino"))
    )


def _valid_recovery_result(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("warning") not in (None, "legacy_replay_at_least_once"):
        return False
    bridge_id, generation = value.get("activeBridgeId"), value.get("generation")
    return (bridge_id is None and generation is None) or (
        try_parse_bridge_id(bridge_id) is not None and _positive_integer(generation)
    )


def _unique_valid_ids(values: Any, parser) -> bool:
    return isinstance(values, list) and len(values) == len(set(values)) and all(parser(value) is not None for value in values)


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def _assert_serialized_bound(value: Any, label: str, max_bytes: int) -> None:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
    if len(serialized.encode("utf-8")) > max_bytes:
        raise BridgeStorageError(f"{label} exceeds {max_bytes} serialized bytes.")


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_positive_integer(value: Any) -> bool:
    return value is None or _positive_integer(value)


def _optional_nonempty_string(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(value))
