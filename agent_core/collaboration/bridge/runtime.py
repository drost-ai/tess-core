from __future__ import annotations

def _knob(name: str):
    from ...config._knobs import knobValue

    return knobValue(name)


from .generation import requireBridgeLedger as _require_ledger

import asyncio
import hashlib
import inspect
import threading
import uuid
from typing import Any

from ..._utils import now_iso
from .admission import admitBridgeMessage
from .generation import closeCurrentBridgeGeneration, isCurrentBridgeGeneration
from .recovery import activateBridgeGeneration
from ...runtime.paths import getActiveRuntimePaths
from ...runtime._ticker import run_scoped_async, start_oneshot, start_ticker
from ...storage import (
    loadSessionRecordShallow,
    saveSession,
    withRecordTransaction,
)
from ...storage.remote import (
    appendBridgeEvent,
    loadBridge,
    loadBridgeDeliveryLedger,
    loadBridgeEvents,
    loadBridgeInbox,
    loadRemoteSession,
    saveBridge,
    saveBridgeDeliveryLedger,
    saveRemoteSession,
)



class LifecycleClosedError(RuntimeError):
    code = "lifecycle.closed"


class BridgeRuntimeController:
    def __init__(self, bridge, emit, paths, args, reused):
        self.bridge = bridge
        self.generation = bridge["generation"]
        self._emit = emit
        self._paths = paths
        self._args = args
        self._reused = reused
        self._closed = False
        self._closing = False
        self._close_complete = False
        self._close_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._poll_condition = threading.Condition()
        self._poll_active = False
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._emit_lock = threading.Lock()
        self._active_emit: dict[str, Any] | None = None
        self._reported_failures: set[str] = set()
        self._poll_halted = False

    async def start(self):
        payload = _omit_none({
            "bridge_id": self.bridge["id"], "bridge_generation": self.generation,
            "session_id": self.bridge["sessionId"],
            "remote_session_id": self.bridge.get("remoteSessionId"),
            "name": self.bridge.get("name"), "reused": self._reused,
        })
        try:
            await self._record_event("bridge.started", payload)
            if not await isCurrentBridgeGeneration(self.bridge):
                raise RuntimeError("Bridge generation was superseded during construction.")
            await self._emit_external(
                "bridge.started", payload,
                f"bridge-started:{self.bridge['id']}:{self.generation}",
                "Bridge start notification deadline exceeded.",
            )
            if not await isCurrentBridgeGeneration(self.bridge):
                raise RuntimeError("Bridge generation was superseded during construction.")
            if self._args.get("startPolling", True):
                self._poll_thread = start_ticker(
                    name=f"agent-core-bridge-{self.bridge['id'][:8]}",
                    stop=self._stop,
                    interval=self._poll_interval(),
                    run_once=self._poll_once,
                    halted=lambda: self._poll_halted,
                )
            return self.surface()
        except Exception:
            self._closed = True
            self._stop.set()
            self._abort_emit("Bridge construction failed.")
            await closeCurrentBridgeGeneration(self.bridge, "bridge.construction_failed")
            await self._wait_emit_settlement(1.0)
            raise

    def surface(self):
        async def send(text: str, **kwargs):
            return await admitBridgeMessage({
                "bridgeId": self.bridge["id"], "generation": self.generation,
                "text": text, **kwargs,
            })

        return {
            "bridge": self.bridge, "generation": self.generation,
            "send": send, "recordEvent": self.record_event,
            "pollInbox": self.poll, "pollBridgeInbox": self.poll,
            "diagnostics": self.diagnostics, "getDiagnostics": self.diagnostics,
            "close": self.close,
        }

    async def record_event(self, event: str, data: dict[str, Any] | None = None):
        if self._closed:
            raise LifecycleClosedError("Cannot record bridge event after bridge close began.")
        return await self._record_event(event, data or {})

    async def _record_event(self, event, data):
        async with withRecordTransaction("bridge-session", self.bridge["sessionId"]):
            if self._closed or not await isCurrentBridgeGeneration(self.bridge):
                return
            timestamp = now_iso()
            await appendBridgeEvent(_omit_none({
                "id": str(uuid.uuid4()), "bridgeId": self.bridge["id"],
                "generation": self.generation, "createdAt": timestamp,
                "event": event, "data": data,
            }))
            await _update_current_bridge(self.bridge, {"lastEventName": event})
            if self.bridge.get("remoteSessionId"):
                remote = await loadRemoteSession(self.bridge["remoteSessionId"])
                if remote and _remote_owns(remote, self.bridge):
                    if not _terminal_remote(remote.get("status")):
                        remote.update({
                            "status": _remote_status(event), "bridgeStatus": "active",
                            "lastEventName": event, "lastEventAt": timestamp,
                            "lastEventSummary": _truncate(_event_summary(event, data)),
                            "updatedAt": timestamp,
                        })
                        await saveRemoteSession(remote)

    async def poll(self):
        if self._closed or self._poll_halted or not self._poll_lock.acquire(blocking=False):
            return []
        with self._poll_condition:
            self._poll_active = True
        try:
            try:
                return await self._poll_locked()
            except Exception as exc:
                await self._report_poll_failure(exc)
                raise
        finally:
            with self._poll_condition:
                self._poll_active = False
                self._poll_condition.notify_all()
            self._poll_lock.release()

    async def _poll_locked(self):
        delivered = []
        async with withRecordTransaction("bridge-session", self.bridge["sessionId"]):
            if self._closed or not await isCurrentBridgeGeneration(self.bridge):
                return delivered
            async with withRecordTransaction("bridge-delivery", self.bridge["id"]):
                inbox = await loadBridgeInbox(self.bridge["id"])
                events = await loadBridgeEvents(self.bridge["id"])
                ledger = await _require_ledger(self.bridge)
                while ledger["cursor"] < len(inbox):
                    if self._closed or not await isCurrentBridgeGeneration(self.bridge):
                        return delivered
                    message = inbox[ledger["cursor"]]
                    if message.get("generation") != self.generation:
                        raise RuntimeError(f"Bridge inbox generation mismatch at cursor {ledger['cursor']}.")
                    key = message.get("idempotencyKey") or f"{self.bridge['id']}:{message['id']}"
                    entry = next((item for item in ledger["entries"] if item["key"] == key), None)
                    if not entry:
                        entry = {
                            "key": key, "sourceBridgeId": self.bridge["id"],
                            "messageId": message["id"], "disposition": "pending",
                            "createdAt": message["createdAt"],
                        }
                        ledger = await _commit_ledger(ledger, entries=[*ledger["entries"], entry])
                    if entry["messageId"] != message["id"]:
                        raise RuntimeError(f"Bridge delivery {key} maps to conflicting message ids.")
                    if entry["disposition"] == "delivered":
                        ledger = await _commit_ledger(ledger, cursor=ledger["cursor"] + 1)
                        continue
                    if entry["disposition"] != "pending":
                        raise RuntimeError(f"Bridge delivery {key} has incompatible disposition {entry['disposition']}.")
                    event_id = _delivery_event_id(key)
                    receipt = next((item for item in events if item["id"] == event_id), None)
                    if receipt and not _matching_receipt(receipt, self.bridge, message, key):
                        raise RuntimeError(f"Bridge delivery receipt {event_id} conflicts with {key}.")
                    payload = _omit_none({
                        "bridge_id": self.bridge["id"], "bridge_generation": self.generation,
                        "session_id": self.bridge["sessionId"],
                        "remote_session_id": self.bridge.get("remoteSessionId"),
                        "message_id": message["id"], "delivery_key": key,
                        "source": message["source"], "text": message["text"],
                    })
                    if not receipt:
                        await self._emit_external(
                            "bridge.message", payload, key, "Bridge poll deadline exceeded."
                        )
                    if self._closed or not await isCurrentBridgeGeneration(self.bridge):
                        return delivered
                    delivered_at = receipt["createdAt"] if receipt else now_iso()
                    if not receipt:
                        receipt = {
                            "id": event_id, "bridgeId": self.bridge["id"],
                            "generation": self.generation, "idempotencyKey": key,
                            "createdAt": delivered_at, "event": "bridge.message.received",
                            "data": {
                                "message_id": message["id"], "delivery_key": key,
                                "source": message["source"], "text": message["text"],
                            },
                        }
                        await appendBridgeEvent(receipt)
                        events.append(receipt)
                    entries = [
                        {**item, "disposition": "delivered", "deliveredAt": delivered_at}
                        if item["key"] == key else item for item in ledger["entries"]
                    ]
                    next_cursor = ledger["cursor"] + 1
                    await _update_current_bridge(self.bridge, {
                        "status": "active", "lastEventName": "bridge.message.received",
                        "lastMessageAt": message["createdAt"], "lastMessageSource": message["source"],
                        "pendingMessages": max(0, len(inbox) - next_cursor),
                        "messageCount": sum(1 for item in entries if item["disposition"] == "delivered"),
                    })
                    await _update_remote_delivery(self.bridge, message, delivered_at, len(inbox) - next_cursor)
                    ledger = await _commit_ledger(ledger, cursor=next_cursor, entries=entries)
                    delivered.append(message)
        return delivered

    async def close(self):
        with self._close_lock:
            if self._close_complete:
                return
            self._closed = True
            self._closing = True
            self._stop.set()
            self._abort_emit("Bridge closed.")
        if self._poll_thread and self._poll_thread.is_alive() and threading.current_thread() is not self._poll_thread:
            await asyncio.to_thread(self._poll_thread.join, self._deadline())
        await asyncio.to_thread(self._wait_for_poll, self._deadline())
        if self._poll_active:
            self._closing = False
            raise RuntimeError("Bridge close did not drain its active poll before the deadline.")
        await closeCurrentBridgeGeneration(self.bridge)
        if not await self._wait_emit_settlement(min(self._deadline(), 1.0)):
            self._closing = False
            error = RuntimeError(f"Bridge {self.bridge['id']} external emit did not settle during close.")
            error.code = "bridge.close_unsettled"
            raise error
        self._close_complete = True
        self._closing = False

    async def diagnostics(self):
        bridge = await loadBridge(self.bridge["id"])
        events = await loadBridgeEvents(self.bridge["id"])
        inbox = await loadBridgeInbox(self.bridge["id"])
        ledger = await loadBridgeDeliveryLedger(self.bridge["id"])
        remote = await loadRemoteSession(bridge["remoteSessionId"]) if bridge and bridge.get("remoteSessionId") else None
        return {
            "bridge": bridge, "remote": remote, "status": (bridge or {}).get("status"),
            "remoteStatus": (remote or {}).get("status"), "bridgeStatus": (remote or {}).get("bridgeStatus"),
            "pendingMessages": (bridge or {}).get("pendingMessages", 0),
            "messageCount": (bridge or {}).get("messageCount", 0), "inboxCount": len(inbox),
            "deliveryCursor": (ledger or {}).get("cursor", 0), "events": events[-20:],
        }

    async def _report_poll_failure(self, error):
        reason = " ".join(str(error).split())[:512] or error.__class__.__name__
        if "Bridge poll deadline exceeded" in reason:
            self._poll_halted = True
        if self._closed or reason in self._reported_failures:
            return
        self._reported_failures.add(reason)
        payload = {
            "bridge_id": self.bridge["id"], "bridge_generation": self.generation,
            "session_id": self.bridge["sessionId"], "reason": reason,
        }
        await self._record_event("bridge.poll_failed", payload)
        with self._emit_lock:
            unsettled = self._active_emit and not self._active_emit["done"].is_set()
        if not unsettled:
            digest = hashlib.sha256(reason.encode()).hexdigest()[:32]
            try:
                await self._emit_external(
                    "bridge.poll_failed", payload,
                    f"bridge-poll-failed:{self.bridge['id']}:{self.generation}:{digest}",
                    "Bridge failure notification deadline exceeded.",
                )
            except Exception:
                pass

    async def _emit_external(self, event, data, idempotency_key, deadline_reason):
        with self._emit_lock:
            if self._active_emit and not self._active_emit["done"].is_set():
                raise RuntimeError("Bridge external emit overlap prevented while a prior emit remains unsettled.")
            state = {"done": threading.Event(), "abort": threading.Event(), "error": None}
            self._active_emit = state

        def worker():
            async def invoke():
                options = {"signal": state["abort"], "idempotencyKey": idempotency_key}
                result = _call_emit(self._emit, event, data, options)
                if inspect.isawaitable(result):
                    await result
            try:
                run_scoped_async(self._paths, invoke)
            except BaseException as exc:
                state["error"] = exc
            finally:
                state["done"].set()

        start_oneshot(f"bridge-emit-{self.bridge['id'][:8]}", worker)
        settled = await asyncio.to_thread(state["done"].wait, self._deadline())
        if not settled:
            state["abort"].set()
            raise TimeoutError(deadline_reason)
        if state["error"]:
            raise state["error"]

    def _abort_emit(self, _reason):
        with self._emit_lock:
            if self._active_emit:
                self._active_emit["abort"].set()

    async def _wait_emit_settlement(self, seconds):
        with self._emit_lock:
            state = self._active_emit
        return True if not state else await asyncio.to_thread(state["done"].wait, seconds)

    def _wait_for_poll(self, seconds):
        with self._poll_condition:
            self._poll_condition.wait_for(lambda: not self._poll_active, timeout=seconds)

    def _poll_once(self):
        try:
            run_scoped_async(self._paths, self.poll)
        except Exception:
            # Back off one extra interval after a failed poll; the ticker's
            # own wait handles prompt exit when stop is set meanwhile.
            self._stop.wait(self._poll_interval())

    def _poll_interval(self):
        return float(self._args.get("pollIntervalSeconds", self._args.get("pollIntervalMs", (int(_knob("collab.bridge_poll_ms")) / 1000) * 1000) / 1000))

    def _deadline(self):
        if "pollDeadlineSeconds" in self._args:
            return max(0.001, float(self._args["pollDeadlineSeconds"]))
        return max(0.001, float(self._args.get("pollDeadlineMs", (int(_knob("collab.bridge_poll_deadline_ms")) / 1000) * 1000)) / 1000)


async def createBridgeRuntimeController(args):
    session = args.get("session") or {}
    if not await loadSessionRecordShallow(session.get("id")):
        await saveSession(session)
    requested = args.get("bridgeId") or args.get("bridge_id")
    if requested and not await loadBridge(requested):
        raise RuntimeError(f"Bridge not found for reconnect: {requested}")
    paths = args.get("runtimePaths") or getActiveRuntimePaths()
    activated = await activateBridgeGeneration({
        "sessionId": session["id"], "cwd": args.get("cwd") or session["cwd"],
        "remoteSessionId": args.get("remoteSessionId"), "name": args.get("name"),
        "reuseActive": args.get("reuseActive", False),
    })
    controller = BridgeRuntimeController(
        activated["bridge"], args.get("emit") or (lambda *_args: None),
        paths, args, activated["reused"],
    )
    return await controller.start()




async def _commit_ledger(ledger, **updates):
    next_ledger = {**ledger, **updates, "revision": ledger["revision"] + 1, "updatedAt": now_iso()}
    await saveBridgeDeliveryLedger(next_ledger)
    return next_ledger


async def _update_current_bridge(identity, updates):
    current = await loadBridge(identity["id"])
    if not current or current.get("status") != "active" or current.get("generation") != identity.get("generation"):
        return
    await saveBridge({
        **current, **updates, "id": current["id"], "sessionId": current["sessionId"],
        "schemaVersion": 2, "generation": current["generation"],
        "revision": int(current.get("revision") or 0) + 1, "updatedAt": now_iso(),
    })


async def _update_remote_delivery(bridge, message, delivered_at, pending):
    if not bridge.get("remoteSessionId"):
        return
    remote = await loadRemoteSession(bridge["remoteSessionId"])
    if not remote or not _remote_owns(remote, bridge):
        return
    remote.update({
        "status": "awaiting_input", "bridgeStatus": "active",
        "unreadBridgeMessages": max(0, pending), "lastEventName": "bridge.message.received",
        "lastEventAt": delivered_at, "lastEventSummary": _truncate(f"Bridge message: {message['text']}"),
        "updatedAt": delivered_at,
    })
    await saveRemoteSession(remote)


def _matching_receipt(receipt, bridge, message, key):
    return receipt.get("bridgeId") == bridge["id"] and receipt.get("generation") == bridge["generation"] and receipt.get("idempotencyKey") == key and receipt.get("event") == "bridge.message.received" and receipt.get("data", {}).get("message_id") == message["id"] and receipt.get("data", {}).get("delivery_key") == key


def _delivery_event_id(key):
    return f"delivery-{hashlib.sha256(key.encode()).hexdigest()[:48]}"


def _remote_owns(remote, bridge):
    return remote.get("sessionId") == bridge["sessionId"] and remote.get("bridgeId") == bridge["id"] and remote.get("bridgeGeneration") == bridge.get("generation")


def _terminal_remote(status):
    return status in {"completed", "cancelled", "failed", "closed"}


def _remote_status(event):
    return "completed" if event == "session.completed" else "active"


def _event_summary(event, data):
    value = data.get("text") or data.get("summary")
    return f"{event}: {value}" if isinstance(value, str) and value.strip() else event


def _call_emit(callback, event, data, options):
    try:
        signature = inspect.signature(callback)
        signature.bind(event, data, options)
    except (TypeError, ValueError):
        return callback(event, data)
    return callback(event, data, options)


def _truncate(value, limit=96):
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def _omit_none(value):
    return {key: item for key, item in value.items() if item is not None}


__all__ = [
    "BridgeRuntimeController", "LifecycleClosedError", "closeCurrentBridgeGeneration",
    "createBridgeRuntimeController", "isCurrentBridgeGeneration",
]
