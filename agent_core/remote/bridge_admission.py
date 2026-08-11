from __future__ import annotations

from .bridge_generation import requireBridgeLedger as _require_ledger

import hashlib
import uuid
from typing import Any

from .._utils import now_iso
from ..storage import load_session_record_shallow, with_record_transaction
from ..storage.remote import (
    append_bridge_event,
    append_bridge_inbox_message,
    load_bridge,
    load_bridge_delivery_ledger,
    load_bridge_inbox,
    load_remote_session,
    save_bridge,
    save_bridge_delivery_ledger,
    save_remote_session,
)

MAX_BRIDGE_MESSAGE_BYTES = 256 * 1024
MAX_IDEMPOTENCY_KEY_BYTES = 4 * 1024


async def sendBridgeMessage(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Admit one generation-bound, idempotent bridge message."""

    return await admitBridgeMessage({**(args or {}), **kwargs})


async def admitBridgeMessage(args: dict[str, Any]) -> dict[str, Any]:
    _assert_input(args)
    initial = await load_bridge(args["bridgeId"])
    if not initial:
        raise RuntimeError(f"Bridge not found: {args['bridgeId']}")
    async with with_record_transaction("bridge-session", initial["sessionId"]):
        async with with_record_transaction("bridge-delivery", initial["id"]):
            return await _admit_under_lock(initial, args)


async def _admit_under_lock(initial, args):
    bridge = await _require_active_generation(initial, args.get("generation"))
    ledger = await _require_ledger(bridge)
    inbox = await load_bridge_inbox(bridge["id"])
    source = args.get("source") if args.get("source") is not None else "bridge"
    requested_key = args.get("idempotencyKey")
    message = _unique_match(inbox, "idempotencyKey", requested_key, "inbox idempotency key") if requested_key else None
    entry = _unique_match(ledger["entries"], "key", requested_key, "ledger idempotency key") if requested_key else None
    if message:
        if message.get("bridgeId") != bridge["id"] or message.get("generation") != bridge["generation"] or message.get("text") != args["text"] or message.get("source") != source:
            raise RuntimeError(f"Bridge idempotency key {requested_key} was reused with a different payload.")
        if entry and entry.get("messageId") != message["id"]:
            raise RuntimeError(f"Bridge idempotency key {requested_key} maps to conflicting message ids.")
        if not entry:
            entry = _pending_entry(requested_key, bridge["id"], message)
            ledger = await _append_ledger(ledger, entry, message["createdAt"])
    elif entry:
        raise RuntimeError(
            f"Bridge delivery ledger key {requested_key} has no matching durable inbox message; refusing ambiguous retry."
        )
    else:
        message_id = str(uuid.uuid4())
        key = requested_key or f"{bridge['id']}:{message_id}"
        message = {
            "id": message_id, "bridgeId": bridge["id"], "generation": bridge["generation"],
            "idempotencyKey": key, "createdAt": now_iso(), "source": source, "text": args["text"],
        }
        await append_bridge_inbox_message(message)
        inbox = [*inbox, message]
        entry = _pending_entry(key, bridge["id"], message)
        ledger = await _append_ledger(ledger, entry, message["createdAt"])
    await _reconcile_side_effects(bridge, message, entry, inbox, ledger)
    return message


async def _require_active_generation(initial, requested_generation):
    bridge = await load_bridge(initial["id"])
    session = await load_session_record_shallow(initial["sessionId"])
    remote = await load_remote_session(bridge["remoteSessionId"]) if bridge and bridge.get("remoteSessionId") else None
    valid = bool(
        bridge and bridge.get("schemaVersion") == 2 and bridge.get("generation")
        and bridge.get("status") == "active" and session
        and session.get("metadata", {}).get("bridgeId") == bridge["id"]
        and session.get("metadata", {}).get("bridgeGeneration") == bridge["generation"]
        and (not bridge.get("remoteSessionId") or (
            remote and remote.get("sessionId") == bridge["sessionId"]
            and remote.get("bridgeId") == bridge["id"]
            and remote.get("bridgeGeneration") == bridge["generation"]
        ))
    )
    if not valid:
        raise RuntimeError(f"Bridge is not the active session generation: {initial['id']}")
    if requested_generation is not None and requested_generation != bridge["generation"]:
        raise RuntimeError(
            f"Bridge generation is stale: expected {bridge['generation']}, received {requested_generation}."
        )
    return bridge




async def _append_ledger(ledger, entry, updated_at):
    next_ledger = {
        **ledger, "revision": ledger["revision"] + 1,
        "entries": [*ledger["entries"], entry], "updatedAt": updated_at,
    }
    await save_bridge_delivery_ledger(next_ledger)
    return next_ledger


async def _reconcile_side_effects(identity, message, entry, inbox, ledger):
    key = message["idempotencyKey"]
    await append_bridge_event({
        "id": _queued_event_id(key), "bridgeId": identity["id"],
        "generation": identity["generation"], "idempotencyKey": key,
        "createdAt": message["createdAt"], "event": "bridge.message.queued",
        "data": {
            "message_id": message["id"], "delivery_key": key,
            "text": message["text"], "source": message["source"],
        },
    })
    pending = max(0, len(inbox) - ledger["cursor"])
    current = await load_bridge(identity["id"])
    if not current or current.get("status") != "active" or current.get("generation") != identity["generation"]:
        raise RuntimeError("Bridge generation changed while reconciling admitted input.")
    latest = entry["disposition"] == "pending" and (
        not current.get("lastMessageAt") or current["lastMessageAt"] < message["createdAt"]
    )
    if latest or current.get("pendingMessages") != pending:
        updated = {
            **current, "revision": int(current.get("revision") or 0) + 1,
            "updatedAt": now_iso(), "pendingMessages": pending,
        }
        if latest:
            updated.update({
                "lastEventName": "bridge.message.queued", "lastMessageAt": message["createdAt"],
                "lastMessageSource": message["source"],
            })
        await save_bridge(updated)
    if identity.get("remoteSessionId"):
        remote = await load_remote_session(identity["remoteSessionId"])
        if not remote or remote.get("sessionId") != identity["sessionId"] or remote.get("bridgeId") != identity["id"] or remote.get("bridgeGeneration") != identity["generation"]:
            raise RuntimeError("Remote-session bridge generation changed while reconciling admitted input.")
        remote.update({"bridgeStatus": "active", "unreadBridgeMessages": pending, "updatedAt": now_iso()})
        if latest:
            remote.update({
                "status": "awaiting_input", "lastEventName": "bridge.message.queued",
                "lastEventAt": message["createdAt"],
                "lastEventSummary": _truncate(f"Queued bridge input: {message['text']}"),
            })
        await save_remote_session(remote)


def _pending_entry(key, source_bridge_id, message):
    return {
        "key": key, "sourceBridgeId": source_bridge_id, "messageId": message["id"],
        "disposition": "pending", "createdAt": message["createdAt"],
    }


def _unique_match(values, field, key, label):
    matches = [item for item in values if item.get(field) == key]
    if len(matches) > 1:
        raise RuntimeError(f"Bridge {label} is duplicated.")
    return matches[0] if matches else None


def _assert_input(args):
    text = args.get("text")
    if not isinstance(text, str):
        raise TypeError("Bridge message text must be a string.")
    if len(text.encode("utf-8")) > MAX_BRIDGE_MESSAGE_BYTES:
        raise RuntimeError("Bridge message exceeds the 262144-byte input limit.")
    key = args.get("idempotencyKey")
    if key is not None and (not isinstance(key, str) or not key or len(key.encode("utf-8")) > MAX_IDEMPOTENCY_KEY_BYTES):
        raise RuntimeError("Bridge idempotency key must contain 1 to 4096 UTF-8 bytes.")
    if args.get("source") not in (None, "bridge", "remote", "local"):
        raise RuntimeError("Bridge message source is invalid.")


def _queued_event_id(key):
    return f"queued-{hashlib.sha256(key.encode()).hexdigest()[:48]}"


def _truncate(value, limit=96):
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


__all__ = ["admitBridgeMessage", "sendBridgeMessage"]
