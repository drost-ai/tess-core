from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..runtime.identifiers import parse_session_id, try_parse_bridge_id, try_parse_remote_session_id
from ..storage import (
    get_state_root,
    hydrate_session_head_from_body_bytes,
    is_session_head_v2,
)

MAX_SCAN_ENTRIES = 4_096
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 512 * 1024
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024


class BridgeRecoveryAnalysisError(RuntimeError):
    code = "bridge.recovery_source_unavailable"


async def analyze_bridge_recovery(
    sessionId: str,
    recoveryRevision: int,
    *,
    excludedBridgeIds: list[str] | None = None,
    logicallySupersededBridgeIds: list[str] | None = None,
) -> dict[str, Any]:
    session_id = parse_session_id(sessionId)
    root = Path(get_state_root()).resolve()
    excluded = set(excludedBridgeIds or [])
    superseded = set(logicallySupersededBridgeIds or [])
    sources: list[dict[str, Any]] = []
    blocked: list[str] = []
    session_path = root / "sessions" / f"{session_id}.json"
    session_raw = _read_source(session_path, MAX_SESSION_BYTES, required=True)
    sources.append(_source_record(session_path, session_raw))
    session_head = _parse_object(session_raw, f"session {session_id}")
    if is_session_head_v2(session_head):
        body_path = root / "sessions" / f"{session_id}.messages.jsonl"
        body_raw = _read_source(body_path, MAX_SOURCE_BYTES, required=True)
        sources.append(_source_record(body_path, body_raw))
        session = hydrate_session_head_from_body_bytes(session_id, session_head, body_raw)["record"]
    else:
        session = session_head
    if session.get("id") != session_id or not isinstance(session.get("metadata"), dict):
        raise BridgeRecoveryAnalysisError("Session record failed bridge recovery identity validation.")

    remotes, remote_sources, remote_manifest, remote_blocked = _scan_records(
        root / "remote-sessions", ".json", try_parse_remote_session_id, MAX_RECORD_BYTES
    )
    sources.extend(remote_sources)
    blocked.extend(remote_blocked)
    remotes = [item for item in remotes if item.get("sessionId") == session_id]

    bridges, bridge_sources, bridge_manifest, bridge_blocked = _scan_bridges(root / "bridges", excluded)
    sources.extend(bridge_sources)
    blocked.extend(bridge_blocked)
    session_bridges = [item for item in bridges if item.get("sessionId") == session_id]
    for bridge in session_bridges:
        if bridge.get("schemaVersion") not in (None, 2):
            blocked.append(f"unsupported_bridge_schema:{bridge.get('id')}")
        if bridge.get("schemaVersion") != 2 and bridge.get("generation") is not None:
            blocked.append(f"legacy_generation_present:{bridge.get('id')}")
    active = [
        item for item in session_bridges
        if item.get("status") == "active" and item.get("id") not in superseded
    ]

    claims = _pointer_claims(session, remotes, blocked)
    distinct = sorted({claim["bridgeId"] for claim in claims})
    selected: dict[str, Any] | None = None
    if len(distinct) > 1:
        blocked.append("pointer_conflict")
    elif len(distinct) == 1:
        selected = next((item for item in bridges if item.get("id") == distinct[0]), None)
        if selected is None:
            blocked.append("pointer_dangling")
        else:
            _validate_pointer_target(selected, session, remotes, claims, blocked)
    elif len(active) == 1:
        selected = active[0]
    elif len(active) > 1:
        blocked.append("multiple_active_without_authority")

    messages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    anomalies: list[str] = []
    for bridge in [item for item in active if item.get("schemaVersion") != 2]:
        inbox, inbox_source = _read_json_array_source(root, bridge["id"], "inbox")
        events, events_source = _read_json_array_source(root, bridge["id"], "events")
        sources.extend((inbox_source, events_source))
        valid_messages = _legacy_messages(inbox, bridge["id"], blocked)
        valid_events = _legacy_events(events, bridge["id"], blocked)
        receipts = _legacy_receipts(valid_events, valid_messages, bridge["id"], blocked, anomalies)
        seen: set[str] = set()
        for message in valid_messages:
            message_id = message["id"]
            if message_id in seen:
                blocked.append(f"duplicate_inbox_id:{bridge['id']}:{message_id}")
                continue
            seen.add(message_id)
            messages.append({
                "key": f"{bridge['id']}:{message_id}",
                "bridge": bridge,
                "message": message,
                "confirmed": message_id in receipts,
            })
        candidates.append({
            "bridgeId": bridge["id"],
            "remoteSessionId": bridge.get("remoteSessionId"),
            "status": bridge.get("status"),
            "inboxMessages": len(valid_messages),
            "receiptEvents": sum(
                1 for event in valid_events
                if isinstance(event, dict) and event.get("event") == "bridge.message.received"
            ),
        })

    if selected and selected.get("schemaVersion") == 2:
        inbox, inbox_source = _read_json_array_source(root, selected["id"], "inbox")
        ledger, ledger_source = _read_json_object_source(root, selected["id"], "ledger")
        sources.extend((inbox_source, ledger_source))
        blocked.extend(_validate_v2(selected, inbox, ledger))
        _validate_v2_pointers(selected, session, remotes, blocked)

    unconfirmed = sorted(item["key"] for item in messages if not item["confirmed"])
    if unconfirmed:
        blocked.append("delivery_unconfirmed")
    if sum(item["size"] for item in sources) > MAX_SOURCE_BYTES:
        raise BridgeRecoveryAnalysisError(f"Bridge recovery sources exceed {MAX_SOURCE_BYTES} bytes.")
    digest = _digest_sources(session_id, [remote_manifest, bridge_manifest], sources)
    inspection = {
        "sessionId": session_id,
        "recoveryRevision": recoveryRevision,
        "recoveryDigest": digest,
        "pointerClaims": claims,
        "candidates": sorted(candidates, key=lambda item: item["bridgeId"]),
        "deliveryUnconfirmedKeys": unconfirmed,
        "legacyAnomalies": sorted(set(anomalies)),
        "blockedReasons": sorted(set(blocked)),
    }
    return {
        "inspection": inspection,
        "session": session,
        "remotes": remotes,
        "bridges": bridges,
        "activeSources": active,
        "selected": selected,
        "selectedV2": selected if selected and selected.get("schemaVersion") == 2 else None,
        "messages": messages,
        "sourceFiles": sources,
        "directoryManifests": [remote_manifest, bridge_manifest],
    }


def _scan_records(directory: Path, suffix: str, parse_id, max_bytes: int, excluded_names=None):
    excluded_names = set(excluded_names or [])
    entries = sorted(
        (path for path in directory.iterdir() if path.name not in excluded_names),
        key=lambda path: path.name,
    ) if directory.exists() else []
    if len(entries) > MAX_SCAN_ENTRIES:
        raise BridgeRecoveryAnalysisError(f"Bridge recovery scan exceeds {MAX_SCAN_ENTRIES} entries in {directory}.")
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    blocked: list[str] = []
    for path in entries:
        if not path.name.endswith(suffix):
            continue
        record_id = parse_id(path.name[: -len(suffix)])
        if not record_id:
            continue
        try:
            raw = _read_source(path, max_bytes, required=True)
            sources.append(_source_record(path, raw))
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("id") != record_id:
                raise ValueError("record identity mismatch")
            records.append(value)
        except Exception as exc:
            blocked.append(f"invalid_record:{record_id}:{_bounded(exc)}")
    manifest = hashlib.sha256("\n".join(path.name for path in entries).encode()).hexdigest()
    return records, sources, manifest, blocked


def _scan_bridges(directory: Path, excluded: set[str]):
    excluded_names = {
        f"{bridge_id}{suffix}"
        for bridge_id in excluded
        for suffix in (".json", ".inbox.json", ".events.json", ".ledger.json")
    }
    return _scan_records(
        directory, ".json", _bridge_primary_id, MAX_RECORD_BYTES, excluded_names
    )


def _bridge_primary_id(value: str) -> str | None:
    if value.endswith((".inbox", ".events", ".ledger")):
        return None
    return try_parse_bridge_id(value)


def _read_source(path: Path, max_bytes: int, *, required: bool) -> bytes:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        if not required:
            return b"[]"
        raise BridgeRecoveryAnalysisError(f"Cannot inspect {path}: missing")
    if path.is_symlink() or not path.is_file():
        raise BridgeRecoveryAnalysisError(f"Bridge recovery source is not a regular file: {path}")
    if stat.st_size > max_bytes:
        raise BridgeRecoveryAnalysisError(f"Bridge recovery source exceeds {max_bytes} bytes: {path}")
    raw = path.read_bytes()
    after = path.stat()
    if len(raw) != stat.st_size or after.st_ino != stat.st_ino or after.st_dev != stat.st_dev:
        raise BridgeRecoveryAnalysisError(f"Bridge recovery source changed during read: {path}")
    return raw


def _source_record(path: Path, raw: bytes) -> dict[str, Any]:
    try:
        stat = path.stat()
        dev, ino = stat.st_dev, stat.st_ino
    except FileNotFoundError:
        dev = ino = 0
    return {
        "path": str(path), "digest": hashlib.sha256(raw).hexdigest(),
        "size": len(raw), "dev": dev, "ino": ino,
    }


def _read_json_array_source(root: Path, bridge_id: str, kind: str):
    path = root / "bridges" / f"{bridge_id}.{kind}.json"
    if not path.exists():
        return [], {"path": str(path), "digest": hashlib.sha256(b"missing").hexdigest(), "size": 0, "dev": 0, "ino": 0}
    raw = _read_source(path, MAX_LOG_BYTES, required=True)
    try:
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        value = None
    if isinstance(value, list) and len(value) > 4_096:
        value = None
    return value, _source_record(path, raw)


def _read_json_object_source(root: Path, bridge_id: str, kind: str):
    path = root / "bridges" / f"{bridge_id}.{kind}.json"
    if not path.exists():
        return None, {"path": str(path), "digest": hashlib.sha256(b"missing").hexdigest(), "size": 0, "dev": 0, "ino": 0}
    raw = _read_source(path, MAX_LOG_BYTES, required=True)
    try:
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        value = None
    return value, _source_record(path, raw)


def _pointer_claims(session, remotes, blocked):
    claims: list[dict[str, Any]] = []
    _claim(session.get("metadata", {}), "session", session["id"], claims, blocked)
    for remote in remotes:
        _claim(remote, "remote_session", remote["id"], claims, blocked)
    return sorted(claims, key=lambda item: f"{item['owner']}:{item['ownerId']}")


def _claim(record, owner, owner_id, claims, blocked):
    bridge_id = record.get("bridgeId")
    generation = record.get("bridgeGeneration")
    label = "session" if owner == "session" else "remote"
    if bridge_id in (None, ""):
        if generation is not None:
            blocked.append(f"{label}_generation_without_pointer:{owner_id}")
        return
    if not try_parse_bridge_id(bridge_id):
        blocked.append(f"{label}_pointer_malformed:{owner_id}")
        return
    claim = {"owner": owner, "ownerId": owner_id, "bridgeId": bridge_id}
    if generation is not None:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            blocked.append(f"{label}_generation_malformed:{owner_id}")
        else:
            claim["bridgeGeneration"] = generation
    claims.append(claim)


def _validate_pointer_target(target, session, remotes, claims, blocked):
    if target.get("sessionId") != session["id"]:
        blocked.append("pointer_foreign_session")
    if target.get("status") != "active":
        blocked.append("pointer_target_not_active")
    for claim in claims:
        if claim["owner"] != "remote_session":
            continue
        remote = next((item for item in remotes if item["id"] == claim["ownerId"]), None)
        if not remote or remote.get("sessionId") != session["id"]:
            blocked.append("remote_pointer_foreign_session")
        if target.get("remoteSessionId") and target["remoteSessionId"] != claim["ownerId"]:
            blocked.append("remote_pointer_owner_mismatch")


def _legacy_messages(values, bridge_id, blocked):
    if not isinstance(values, list):
        blocked.append(f"malformed_inbox:{bridge_id}")
        return []
    output = []
    for value in values:
        valid = isinstance(value, dict) and value.get("bridgeId") == bridge_id and isinstance(value.get("id"), str) and bool(value["id"]) and _is_timestamp(value.get("createdAt")) and value.get("source") in {"bridge", "remote", "local"} and isinstance(value.get("text"), str)
        if valid:
            output.append(value)
        else:
            blocked.append(f"malformed_inbox:{bridge_id}")
    return output


def _legacy_events(values, bridge_id, blocked):
    if not isinstance(values, list):
        blocked.append(f"malformed_events:{bridge_id}")
        return []
    output = []
    for value in values:
        valid = isinstance(value, dict) and value.get("bridgeId") == bridge_id and isinstance(value.get("id"), str) and bool(value["id"]) and _is_timestamp(value.get("createdAt")) and isinstance(value.get("event"), str) and bool(value["event"]) and isinstance(value.get("data"), dict)
        if valid:
            output.append(value)
        else:
            blocked.append(f"malformed_event:{bridge_id}")
    return output


def _legacy_receipts(events, inbox, bridge_id, blocked, anomalies):
    ids = {item["id"] for item in inbox}
    receipts: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "bridge.message.received":
            continue
        message_id = (event.get("data") or {}).get("message_id")
        if not isinstance(message_id, str) or not message_id:
            blocked.append(f"malformed_receipt:{bridge_id}")
            continue
        if message_id not in ids:
            blocked.append(f"receipt_without_inbox:{bridge_id}:{message_id}")
            continue
        if message_id in receipts:
            anomalies.append(f"duplicate_receipt:{bridge_id}:{message_id}")
        receipts.add(message_id)
    return receipts


def _validate_v2(bridge, inbox, ledger):
    reasons: list[str] = []
    generation = bridge.get("generation")
    valid_inbox = isinstance(inbox, list) and all(
        isinstance(message, dict) and message.get("bridgeId") == bridge["id"]
        and message.get("generation") == generation and isinstance(message.get("idempotencyKey"), str)
        and bool(message["idempotencyKey"]) and _is_timestamp(message.get("createdAt"))
        and message.get("source") in {"bridge", "remote", "local"}
        and isinstance(message.get("text"), str) for message in inbox
    )
    if not valid_inbox:
        return [f"invalid_v2_inbox:{bridge['id']}"]
    if not isinstance(ledger, dict) or ledger.get("schemaVersion") != 1 or ledger.get("bridgeId") != bridge["id"] or ledger.get("sessionId") != bridge["sessionId"] or ledger.get("generation") != generation or not isinstance(ledger.get("revision"), int) or not isinstance(ledger.get("cursor"), int) or not isinstance(ledger.get("entries"), list) or ledger["cursor"] < 0 or ledger["cursor"] > len(inbox) or ledger["cursor"] > len(ledger["entries"]):
        return [f"invalid_v2_ledger:{bridge['id']}"]
    entries = {entry.get("key"): entry for entry in ledger["entries"] if isinstance(entry, dict)}
    if len(entries) != len(ledger["entries"]):
        return [f"invalid_v2_ledger:{bridge['id']}"]
    seen: set[str] = set()
    for index, message in enumerate(inbox):
        key = message["idempotencyKey"]
        if key in seen:
            reasons.append(f"duplicate_v2_delivery_key:{bridge['id']}:{key}")
        seen.add(key)
        entry = entries.get(key)
        if entry is None:
            if index < ledger["cursor"]:
                reasons.append(f"v2_delivery_conflict:{bridge['id']}:{key}")
            continue
        expected = "delivered" if index < ledger["cursor"] else "pending"
        if entry.get("messageId") != message.get("id") or entry.get("disposition") != expected:
            reasons.append(f"v2_delivery_conflict:{bridge['id']}:{key}")
    return reasons


def _validate_v2_pointers(bridge, session, remotes, blocked):
    generation = bridge.get("generation")
    if not isinstance(generation, int) or generation <= 0:
        blocked.append("v2_generation_invalid")
        return
    metadata = session["metadata"]
    if metadata.get("bridgeId") != bridge["id"] or metadata.get("bridgeGeneration") != generation:
        blocked.append("session_generation_mismatch")
    if bridge.get("remoteSessionId"):
        remote = next((item for item in remotes if item["id"] == bridge["remoteSessionId"]), None)
        if not remote or remote.get("bridgeId") != bridge["id"] or remote.get("bridgeGeneration") != generation:
            blocked.append("remote_generation_mismatch")


def _digest_sources(session_id, manifests, sources):
    digest = hashlib.sha256()
    digest.update(f"bridge-recovery-v3\0{session_id}\0".encode())
    for manifest in manifests:
        digest.update(f"dir\0{manifest}\0".encode())
    for source in sorted(sources, key=lambda item: item["path"]):
        digest.update(f"{source['path']}\0{source['size']}\0{source['dev']}\0{source['ino']}\0{source['digest']}\0".encode())
    return digest.hexdigest()


def _parse_object(raw: bytes, label: str):
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise BridgeRecoveryAnalysisError(f"Malformed {label}: {_bounded(exc)}") from exc
    if not isinstance(value, dict):
        raise BridgeRecoveryAnalysisError(f"Malformed {label}: expected object")
    return value


def _bounded(error: Exception) -> str:
    return " ".join(str(error).split())[:160]


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


async def verify_bridge_recovery_source_files(sources: list[dict[str, Any]]) -> None:
    for source in sources:
        path = Path(source["path"])
        try:
            raw = _read_source(path, max(int(source["size"]), 1), required=True)
            current = _source_record(path, raw)
        except Exception as exc:
            raise BridgeRecoveryAnalysisError(f"Bridge recovery source changed: {path}") from exc
        if any(current[key] != source[key] for key in ("digest", "size", "dev", "ino")):
            raise BridgeRecoveryAnalysisError(f"Bridge recovery source changed: {path}")


__all__ = [
    "BridgeRecoveryAnalysisError", "analyze_bridge_recovery",
    "verify_bridge_recovery_source_files",
]
