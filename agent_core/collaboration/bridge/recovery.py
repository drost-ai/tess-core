from __future__ import annotations

from .generation import requireBridgeLedger as _require_ledger

import copy
import hashlib
import inspect
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from ..._utils import now_iso
from .recovery_analysis import analyzeBridgeRecovery
from ...runtime.identifiers import parseBridgeId, parseSessionId
from ...storage import (
    get_state_root,
    load_session_record_shallow,
    mutate_session_record_transaction,
    with_record_transaction,
)
from ...storage.remote import (
    load_bridge,
    load_bridge_delivery_ledger,
    load_bridge_inbox,
    load_bridge_recovery_record,
    load_remote_session,
    replace_bridge_inbox,
    save_bridge,
    save_bridge_delivery_ledger,
    save_bridge_recovery_record,
    save_remote_session,
)

_RECOVERY_PHASES = {
    "blocked", "prepared", "destination_written", "inbox_written",
    "authority_verified", "pointers_published", "committed",
}
_recovery_test_hook: Callable[[str, dict[str, Any]], Any] | None = None


class BridgeRecoveryBlockedError(RuntimeError):
    code = "bridge.recovery_blocked"

    def __init__(self, inspection: dict[str, Any]):
        self.inspection = inspection
        reasons = ", ".join(inspection["blockedReasons"])
        super().__init__(f"Bridge recovery is blocked for session {inspection['sessionId']}: {reasons}.")


def __setBridgeRecoveryTestHookForTests(hook) -> None:
    global _recovery_test_hook
    _recovery_test_hook = hook


async def inspectBridgeRecovery(sessionId: str) -> dict[str, Any]:
    session_id = parseSessionId(sessionId)
    async with with_record_transaction("bridge-session", session_id):
        journal = await _resume_pending(session_id)
        analysis = await _analyze_with_journal(session_id, journal)
        revision = _next_revision(journal, analysis["inspection"]["recoveryDigest"])
        inspection = {**analysis["inspection"], "recoveryRevision": revision}
        if not analysis["selectedV2"] and inspection["blockedReasons"]:
            await _save_blocked(analysis, inspection, journal)
        return inspection


async def resolveBridgeRecovery(
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_id = parseSessionId(args["sessionId"])
    async with with_record_transaction("bridge-session", session_id):
        existing = await _resume_pending(session_id)
        analysis = await _analyze_with_journal(session_id, existing)
        revision = _next_revision(existing, analysis["inspection"]["recoveryDigest"])
        if revision != args.get("expectedRecoveryRevision") or analysis["inspection"]["recoveryDigest"] != args.get("recoveryDigest"):
            raise RuntimeError("Bridge recovery inspection is stale; inspect again before resolving.")
        selected_id = args.get("selectedLegacyBridgeId")
        decisions = args.get("deliveryDecisions") or {}
        if selected_id is None:
            if decisions:
                raise RuntimeError("Close-all bridge recovery does not accept delivery decisions.")
            journal = _create_journal(analysis, revision, None, {})
        else:
            selected_id = parseBridgeId(selected_id)
            selected = next(
                (bridge for bridge in analysis["activeSources"] if bridge["id"] == selected_id and bridge.get("schemaVersion") != 2),
                None,
            )
            if selected is None:
                raise RuntimeError("Selected bridge is not an active session-owned legacy candidate.")
            _assert_resolvable(analysis["inspection"]["blockedReasons"])
            _assert_exact_decisions(analysis["inspection"]["deliveryUnconfirmedKeys"], decisions)
            journal = _create_journal(analysis, revision, selected, decisions)
        await _persist_phase(journal, "prepared")
        applied = await _apply_journal(journal)
        return applied["result"]


async def activateBridgeGeneration(args: dict[str, Any]) -> dict[str, Any]:
    session_id = parseSessionId(args["sessionId"])
    async with with_record_transaction("bridge-session", session_id):
        existing = await _resume_pending(session_id)
        analysis = await _analyze_with_journal(session_id, existing)
        if analysis["inspection"]["blockedReasons"]:
            revision = _next_revision(existing, analysis["inspection"]["recoveryDigest"])
            inspection = {**analysis["inspection"], "recoveryRevision": revision}
            await _save_blocked(analysis, inspection, existing)
            raise BridgeRecoveryBlockedError(inspection)
        if analysis["selectedV2"] and args.get("reuseActive"):
            ledger = await _require_ledger(analysis["selectedV2"])
            return {"bridge": analysis["selectedV2"], "ledger": ledger, "reused": True}
        revision = _next_revision(existing, analysis["inspection"]["recoveryDigest"])
        if not analysis["selectedV2"] and analysis["activeSources"]:
            journal = _create_journal(analysis, revision, analysis["selected"], {})
        else:
            journal = await _create_fresh_journal(analysis, revision, args)
        await _persist_phase(journal, "prepared")
        applied = await _apply_journal(journal)
        bridge = await _require_bridge(applied["result"]["activeBridgeId"])
        return {"bridge": bridge, "ledger": await _require_ledger(bridge), "reused": False}


async def _resume_pending(session_id: str) -> dict[str, Any] | None:
    journal = await load_bridge_recovery_record(session_id)
    if not journal or journal.get("phase") in {"blocked", "committed"}:
        return journal
    return await _apply_journal(journal)


async def _analyze_with_journal(session_id: str, journal: dict[str, Any] | None):
    ignored = journal.get("sourceBridgeIds", []) if journal and journal.get("phase") == "committed" else []
    analysis = await analyzeBridgeRecovery(
        session_id,
        int((journal or {}).get("revision") or 1),
        logicallySupersededBridgeIds=ignored,
    )
    analysis["logicallySupersededBridgeIds"] = list(ignored)
    return analysis


def _create_journal(analysis, revision, selected, decisions):
    created_at = now_iso()
    destination_id = None if selected is None else str(uuid.uuid4())
    generation = _next_generation(analysis["bridges"]) if destination_id else None
    plans = []
    if selected is not None:
        for item in analysis["messages"]:
            decision = decisions.get(item["key"])
            disposition = (
                "legacy_confirmed" if item["confirmed"]
                else "legacy_acknowledged" if decision == "acknowledge_processed"
                else "pending"
            )
            plan = {
                "key": item["key"], "sourceBridgeId": item["bridge"]["id"],
                "messageId": item["message"]["id"], "disposition": disposition,
                "createdAt": item["message"]["createdAt"],
            }
            if decision == "replay_at_least_once":
                plan["warning"] = "legacy_replay_at_least_once"
            plans.append(plan)
    return _journal_base(
        analysis, revision, created_at, destination_id, generation,
        selected.get("remoteSessionId") if selected else None,
        selected.get("name") if selected else None,
        selected.get("cwd") if selected else analysis["session"]["cwd"],
        plans,
        selected.get("id") if selected and selected.get("schemaVersion") != 2 else None,
    )


async def _create_fresh_journal(analysis, revision, args):
    replacement = analysis["selectedV2"]
    plans: list[dict[str, Any]] = []
    if replacement:
        ledger = await _require_ledger(replacement)
        inbox = await load_bridge_inbox(replacement["id"])
        entries = {entry["key"]: entry for entry in ledger["entries"]}
        for message in inbox[ledger["cursor"] :]:
            key = message.get("idempotencyKey") or f"{replacement['id']}:{message['id']}"
            if entries.get(key, {}).get("disposition") == "delivered":
                continue
            plans.append({
                "key": key, "sourceBridgeId": replacement["id"],
                "messageId": message["id"], "disposition": "pending",
                "createdAt": message["createdAt"],
            })
    created_at = now_iso()
    return _journal_base(
        analysis, revision, created_at, str(uuid.uuid4()), _next_generation(analysis["bridges"]),
        args.get("remoteSessionId") if args.get("remoteSessionId") is not None else (replacement or {}).get("remoteSessionId"),
        args.get("name") if args.get("name") is not None else (replacement or {}).get("name"),
        args.get("cwd") or (replacement or {}).get("cwd") or analysis["session"]["cwd"],
        plans, None,
    )


def _journal_base(analysis, revision, created_at, destination_id, generation, remote_id, name, cwd, plans, selected_legacy):
    warning = "legacy_replay_at_least_once" if any(item.get("warning") for item in plans) else None
    return {
        "schemaVersion": 1,
        "sessionId": analysis["session"]["id"],
        "revision": revision,
        "sourceDigest": analysis["inspection"]["recoveryDigest"],
        "phase": "prepared",
        "blockedReasons": list(analysis["inspection"]["blockedReasons"]),
        "selectedLegacyBridgeId": selected_legacy,
        "sourceBridgeIds": sorted(set(analysis.get("logicallySupersededBridgeIds", [])) | {
            bridge["id"] for bridge in analysis["activeSources"]
        }),
        "remoteSessionIds": sorted(remote["id"] for remote in analysis["remotes"]),
        "pointerClaims": copy.deepcopy(analysis["inspection"]["pointerClaims"]),
        "pointerBaselines": _pointer_baselines(analysis["session"], analysis["remotes"]),
        "authorityDirectoryManifests": list(analysis["directoryManifests"]),
        "destinationBridgeId": destination_id,
        "destinationGeneration": generation,
        "destinationRemoteSessionId": remote_id,
        "destinationName": name,
        "destinationCwd": cwd,
        "messagePlans": plans,
        "immutableSourceFiles": list(analysis["sourceFiles"]),
        "warning": warning,
        "createdAt": created_at,
        "updatedAt": created_at,
    }


async def _apply_journal(journal):
    if journal["phase"] == "prepared":
        if journal.get("destinationBridgeId"):
            now = now_iso()
            await save_bridge({
                "id": journal["destinationBridgeId"], "sessionId": journal["sessionId"],
                "remoteSessionId": journal.get("destinationRemoteSessionId"),
                "name": journal.get("destinationName"), "createdAt": now, "updatedAt": now,
                "status": "starting", "cwd": journal["destinationCwd"],
                "schemaVersion": 2, "generation": journal["destinationGeneration"],
                "revision": 0, "supersedes": journal["sourceBridgeIds"],
                "recoveryWarning": journal.get("warning"),
                "pendingMessages": sum(1 for item in journal["messagePlans"] if item["disposition"] == "pending"),
                "messageCount": 0,
            })
        await _persist_phase(journal, "destination_written")
    if journal["phase"] == "destination_written":
        if journal.get("destinationBridgeId"):
            inbox = await _build_replay_inbox(journal)
            await replace_bridge_inbox(journal["destinationBridgeId"], inbox)
            await save_bridge_delivery_ledger({
                "schemaVersion": 1, "bridgeId": journal["destinationBridgeId"],
                "sessionId": journal["sessionId"], "generation": journal["destinationGeneration"],
                "revision": 0, "cursor": 0,
                "entries": [copy.deepcopy(item) for item in journal["messagePlans"]],
                "updatedAt": now_iso(),
            })
        await _persist_phase(journal, "inbox_written")
    if journal["phase"] == "inbox_written":
        current = await analyzeBridgeRecovery(
            journal["sessionId"], journal["revision"],
            excludedBridgeIds=[journal["destinationBridgeId"]] if journal.get("destinationBridgeId") else [],
        )
        if current["inspection"]["recoveryDigest"] != journal["sourceDigest"]:
            raise RuntimeError("Bridge recovery authority changed before pointer publication; inspect again before resolving.")
        await _persist_phase(journal, "authority_verified")
    if journal["phase"] == "authority_verified":
        await _publish_pointers(journal)
        await _persist_phase(journal, "pointers_published")
    if journal["phase"] == "pointers_published":
        await _terminalize_sources(journal)
        if journal.get("destinationBridgeId"):
            bridge = await _require_bridge(journal["destinationBridgeId"])
            if bridge.get("schemaVersion") != 2 or bridge.get("generation") != journal["destinationGeneration"]:
                raise RuntimeError("Bridge recovery destination identity changed before activation.")
            await save_bridge({
                **bridge, "status": "active", "revision": int(bridge.get("revision") or 0) + 1,
                "updatedAt": now_iso(), "lastEventName": "bridge.started",
            })
        journal["result"] = {
            "activeBridgeId": journal.get("destinationBridgeId"),
            "generation": journal.get("destinationGeneration"),
            "warning": journal.get("warning"),
        }
        await _persist_phase(journal, "committed")
    return journal


async def _publish_pointers(journal):
    published_at = journal["createdAt"]
    baseline = _require_baseline(journal, "session", journal["sessionId"])

    def mutate(session):
        _assert_baseline(baseline, session, journal.get("destinationBridgeId"), journal.get("destinationGeneration"))
        metadata = {**session["metadata"]}
        if journal.get("destinationBridgeId"):
            metadata.update({"bridgeId": journal["destinationBridgeId"], "bridgeGeneration": journal["destinationGeneration"]})
        else:
            metadata.pop("bridgeId", None)
            metadata.pop("bridgeGeneration", None)
        updated = {**session, "metadata": metadata, "updatedAt": published_at}
        return {"session": updated, "changed": updated != session}

    await mutate_session_record_transaction(journal["sessionId"], mutate, requireExisting=True)
    await _call_hook("session_pointer_written", journal)
    claims = {item["ownerId"]: item for item in journal["pointerClaims"] if item["owner"] == "remote_session"}
    for remote_id in journal["remoteSessionIds"]:
        remote = await load_remote_session(remote_id)
        if not remote:
            raise RuntimeError(f"Remote session disappeared during bridge pointer publication: {remote_id}")
        remote_baseline = _require_baseline(journal, "remote_session", remote_id)
        owns = remote_id == journal.get("destinationRemoteSessionId")
        destination_id = journal.get("destinationBridgeId") if owns else None
        destination_generation = journal.get("destinationGeneration") if owns else None
        _assert_baseline(remote_baseline, remote, destination_id, destination_generation)
        updated = {**remote, "bridgeStatus": "active" if destination_id else "closed", "lastEventAt": published_at, "lastEventSummary": "Bridge attached" if destination_id else "Bridge superseded", "updatedAt": published_at}
        if destination_id:
            updated.update({"bridgeId": destination_id, "bridgeGeneration": destination_generation})
        else:
            updated.pop("bridgeId", None)
            updated.pop("bridgeGeneration", None)
        await save_remote_session(updated)
        await _call_hook("remote_pointer_written", journal)


async def _terminalize_sources(journal):
    for bridge_id in journal["sourceBridgeIds"]:
        bridge = await _require_bridge(bridge_id)
        if bridge.get("schemaVersion") != 2 or bridge_id == journal.get("destinationBridgeId"):
            continue
        if bridge.get("status") in {"closed", "failed", "superseded"}:
            continue
        await save_bridge({
            **bridge, "status": "superseded", "supersededBy": journal.get("destinationBridgeId"),
            "revision": int(bridge.get("revision") or 0) + 1,
            "updatedAt": now_iso(), "lastEventName": "bridge.superseded",
        })


async def _build_replay_inbox(journal):
    sources: dict[str, list[dict[str, Any]]] = {}
    output = []
    for plan in journal["messagePlans"]:
        if plan["disposition"] != "pending":
            continue
        if plan["sourceBridgeId"] not in sources:
            sources[plan["sourceBridgeId"]] = await load_bridge_inbox(plan["sourceBridgeId"])
        message = next((item for item in sources[plan["sourceBridgeId"]] if item["id"] == plan["messageId"]), None)
        if not message:
            raise RuntimeError(f"Legacy bridge message disappeared during recovery: {plan['key']}")
        output.append({
            **message, "bridgeId": journal["destinationBridgeId"],
            "generation": journal["destinationGeneration"], "idempotencyKey": plan["key"],
        })
    return output


async def _save_blocked(analysis, inspection, existing):
    if existing and existing.get("phase") == "blocked" and existing.get("sourceDigest") == inspection["recoveryDigest"] and existing.get("revision") == inspection["recoveryRevision"]:
        return
    created_at = now_iso()
    journal = _journal_base(
        analysis, inspection["recoveryRevision"], created_at, None, None,
        None, None, analysis["session"]["cwd"], [], None,
    )
    journal["phase"] = "blocked"
    journal["blockedReasons"] = list(inspection["blockedReasons"])
    await save_bridge_recovery_record(analysis["session"]["id"], journal)


async def _persist_phase(journal, phase):
    if phase not in _RECOVERY_PHASES:
        raise ValueError(f"Invalid bridge recovery phase: {phase}")
    journal["phase"] = phase
    journal["updatedAt"] = now_iso()
    await save_bridge_recovery_record(journal["sessionId"], journal)
    await _call_hook(phase, journal)


async def _call_hook(phase, journal):
    if _recovery_test_hook is None:
        return
    result = _recovery_test_hook(phase, copy.deepcopy(journal))
    if inspect.isawaitable(result):
        await result


def _pointer_baselines(session, remotes):
    return sorted(
        [_baseline("session", session["id"], session)]
        + [_baseline("remote_session", remote["id"], remote) for remote in remotes],
        key=lambda item: f"{item['owner']}:{item['ownerId']}",
    )


def _baseline(owner, owner_id, record):
    return {
        "owner": owner, "ownerId": owner_id,
        "authorityDigest": _authority_digest(record),
        "normalizedRecordDigest": _record_digest(record),
    }


def _authority_digest(record):
    source = record.get("metadata", {}) if "metadata" in record else record
    return _digest_json([source.get("bridgeId", ["undefined"]), source.get("bridgeGeneration", ["undefined"])])


def _record_digest(record):
    value = copy.deepcopy(record)
    value.pop("updatedAt", None)
    value.pop("revision", None)
    if "metadata" in value:
        value["metadata"].pop("bridgeId", None)
        value["metadata"].pop("bridgeGeneration", None)
    else:
        for key in ("bridgeId", "bridgeGeneration", "bridgeStatus", "lastEventAt", "lastEventSummary"):
            value.pop(key, None)
    return _digest_json(value)


def _assert_baseline(baseline, current, destination_id, destination_generation):
    if _record_digest(current) != baseline["normalizedRecordDigest"]:
        raise RuntimeError(f"Bridge pointer source changed before publication: {baseline['owner']}:{baseline['ownerId']}")
    authority = _authority_digest(current)
    destination = _digest_json([destination_id if destination_id is not None else ["undefined"], destination_generation if destination_generation is not None else ["undefined"]])
    if authority not in {baseline["authorityDigest"], destination}:
        raise RuntimeError(f"Bridge pointer authority changed before publication: {baseline['owner']}:{baseline['ownerId']}")


def _require_baseline(journal, owner, owner_id):
    baseline = next((item for item in journal["pointerBaselines"] if item["owner"] == owner and item["ownerId"] == owner_id), None)
    if not baseline:
        raise RuntimeError(f"Bridge recovery pointer baseline is missing: {owner}:{owner_id}")
    return baseline


def _digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _next_revision(journal, digest):
    if not journal:
        return 1
    return int(journal["revision"]) if journal.get("sourceDigest") == digest else int(journal["revision"]) + 1


def _next_generation(bridges):
    return max([0] + [int(bridge.get("generation") or 0) for bridge in bridges if bridge.get("schemaVersion") == 2]) + 1


async def _require_bridge(bridge_id):
    bridge = await load_bridge(bridge_id)
    if not bridge:
        raise RuntimeError(f"Bridge disappeared during lifecycle transition: {bridge_id}")
    return bridge




def _assert_exact_decisions(keys, decisions):
    if sorted(keys) != sorted(decisions):
        raise RuntimeError("Bridge recovery requires exactly one decision for every delivery-unconfirmed key.")
    if any(value not in {"replay_at_least_once", "acknowledge_processed"} for value in decisions.values()):
        raise RuntimeError("Bridge recovery delivery decision is invalid.")


def _assert_resolvable(reasons):
    allowed = {
        "delivery_unconfirmed", "multiple_active_without_authority", "pointer_conflict",
        "pointer_dangling", "pointer_foreign_session", "pointer_target_not_active",
        "remote_pointer_foreign_session", "remote_pointer_owner_mismatch",
    }
    hard = [reason for reason in reasons if reason.split(":", 1)[0] not in allowed]
    if hard:
        raise RuntimeError(f"Bridge recovery sources are malformed: {', '.join(hard)}.")


__all__ = [
    "BridgeRecoveryBlockedError", "activateBridgeGeneration",
    "inspectBridgeRecovery", "resolveBridgeRecovery",
]
