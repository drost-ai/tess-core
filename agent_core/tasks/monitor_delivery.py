"""Durable monitor consumption. Providers run outside locks; publication is fenced.

The host supplies an idempotent, durable publisher keyed by delivery ID. Its
receipt is the publication boundary; an engine crash may repeat that callback,
but must never repeat the visible append. This is not an exactly-once tool API.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import time
from typing import Any, Callable
import uuid

from .._utils import maybe_await, now_iso, parse_iso_ms
from ..storage import list_monitors, read_task_meta, task_output_path
from ..storage.paths import scoped_runtime_home, resolve_runtime_home
from .monitor_delivery_state import MAX_ATTEMPTS, mute_monitor_record
from .monitor_transactions import commit_monitor_mutation


class MonitorDeliveryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MonitorDeliveryController:
    """Host consumer bound to an explicit runtime home and native session IDs."""

    def __init__(self, runtime_home: str, *, clock: Callable[[], int] | None = None):
        self.runtime_home = resolve_runtime_home(candidate=runtime_home)
        self.clock = clock or (lambda: int(time.time() * 1000))

    async def observe(self, *, session_id: str | None = None) -> None:
        from .supervisor_parts.monitor_watch import tick_daemon_monitors
        with scoped_runtime_home(self.runtime_home):
            await tick_daemon_monitors(lambda *_: None, session_id=session_id)

    async def list_pending(self, *, session_id: str | None = None, limit: int = 100,
                           after: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        result = []
        with scoped_runtime_home(self.runtime_home):
            for monitor in sorted(await list_monitors(), key=lambda item: item["id"]):
                if session_id is not None and monitor["sessionId"] != session_id:
                    continue
                state = monitor.get("delivery") or {}
                if state.get("runtimeHome") != self.runtime_home or monitor.get("deliveryStoppedAt"):
                    continue
                for item in sorted(state.get("queue", []), key=lambda item: item["id"]):
                    cursor = monitor["id"] + "/" + item["id"]
                    if after is not None and cursor <= after:
                        continue
                    result.append({"cursor": cursor, "monitorId": monitor["id"], "sessionId": monitor["sessionId"],
                                   "deliveryId": item["id"], "mode": state["mode"],
                                   "state": item["state"], "muted": bool(monitor.get("muted")),
                                   "reason": item["reason"], "observedAt": item["observedAt"],
                                   "availableAt": item.get("availableAt", 0),
                                   "leaseUntil": item.get("leaseUntil", 0),
                                   "consumer": item.get("owner"),
                                   "deliveryStatus": "held" if monitor.get("muted") else "waiting_for_consumer" if item["state"] == "pending" else item["state"],
                                   "lastAuthoredAt": item.get("authoredAt"), "lastPublishedAt": state.get("lastPublishedAt"),
                                   "error": item.get("error") or state.get("lastError"), "attempts": item["attempts"]})
                    if len(result) >= limit:
                        return result
        return result

    async def _mutate(self, monitor_id: str, session_id: str, fn: Callable) -> Any:
        async def mutate(latest):
            state = latest.get("delivery")
            if not isinstance(state, dict) or state.get("version") != 1 or state.get("runtimeHome") != self.runtime_home:
                raise MonitorDeliveryError("monitor.runtime_mismatch")
            record = deepcopy(latest)
            value = await maybe_await(fn(record))
            return {"record": record, "changed": record != latest, "value": value}
        with scoped_runtime_home(self.runtime_home):
            committed = await commit_monitor_mutation(monitorId=monitor_id, expectedSessionId=session_id, mutate=mutate)
            return committed["value"]

    async def claim(self, monitor_id: str, session_id: str, owner: str, *, lease_ms: int = 60_000,
                    continuation_supported: bool = False) -> dict[str, Any] | None:
        if not owner or not 1000 <= lease_ms <= 300_000:
            raise ValueError("owner and a lease between 1000 and 300000ms are required")
        async def mutate(record):
            if record.get("muted") or record.get("deliveryStoppedAt"):
                return None
            state = record["delivery"]
            if state["mode"] == "continuation" and not continuation_supported:
                state["lastError"] = "monitor.continuation_unsupported"
                return None
            queue = state["queue"]
            if not queue:
                return None
            if len(queue) > 1 and queue[0]["state"] == "failed" and queue[-1]["reason"] in {"completed", "failed", "stopped"}:
                failed = queue.pop(0)
                state["lastFailure"] = {"deliveryId": failed["id"], "error": failed.get("error"), "reason": failed["reason"]}
            item = queue[0]
            now = self.clock()
            if item["reason"] in {"progress", "heartbeat"}:
                from ..config._knobs import knob_value
                last = parse_iso_ms(state.get("lastProcessedAt"))
                if last and now - last < int(knob_value("tasks.monitor_narration_spacing_ms")):
                    return None
            if item.get("leaseUntil", 0) > now or item.get("availableAt", 0) > now or item["state"] == "failed":
                return None
            if item.get("executionStarted") and not item.get("result"):
                item.update(state="failed", error="monitor.execution_uncertain")
                return None
            if item["attempts"] >= MAX_ATTEMPTS:
                item.update(state="failed", error=item.get("error") or "monitor.retry_exhausted")
                return None
            task = await read_task_meta(record["taskId"])
            if task and task.get("sessionId") != session_id:
                state.update(queue=[], lastError="monitor.task_not_owned")
                record["deliveryStoppedAt"] = now_iso()
                return None
            item.update(token=str(uuid.uuid4()), owner=owner, leaseUntil=now + lease_ms,
                        state="authored" if item.get("result") else "claimed", attempts=item["attempts"] + 1)
            # Freeze bounded, untrusted context under the delivery identity.
            if "envelope" not in item:
                item["envelope"] = self._envelope(record, item)
            return {"monitorId": monitor_id, "sessionId": session_id, "runtimeHome": self.runtime_home,
                    "deliveryId": item["id"], "token": item["token"], "owner": owner,
                    "envelope": deepcopy(item["envelope"]), "result": deepcopy(item.get("result"))}
        return await self._mutate(monitor_id, session_id, mutate)

    def _envelope(self, record, item):
        excerpt = ""
        start, end = item["fromByte"], item["toByte"]
        omitted = max(0, end - start)
        path = Path(task_output_path(record["taskId"]))
        try:
            with path.open("rb") as stream:
                import os
                stat = os.fstat(stream.fileno())
                identity = f"{stat.st_dev}:{stat.st_ino}"
                if (not item.get("outputIdentity") or identity == item["outputIdentity"]) and stat.st_size >= end:
                    offset = max(start, end - 8192)
                    stream.seek(offset)
                    raw = stream.read(end - offset)
                    excerpt = raw.decode("utf-8", errors="replace")
                    omitted = offset - start
        except FileNotFoundError:
            pass
        return {"deliveryId": item["id"], "monitorId": record["id"], "taskId": record["taskId"],
                "sessionId": record["sessionId"], "mode": record["delivery"]["mode"],
                "reason": item["reason"], "taskStatus": item["taskStatus"], "exitCode": item.get("exitCode"),
                "goal": str(record.get("goal") or "")[:4096], "summaryStyle": str(record.get("summaryStyle") or "")[:1024],
                "outputGeneration": item["generation"], "fromByte": start, "toByte": end,
                "omittedBytes": omitted, "outputExcerpt": excerpt, "observedAt": item["observedAt"],
                "artifact": {"taskId": record["taskId"], "generation": item["generation"], "tool": "TaskOutput"}}

    def _owned(self, record, claim):
        if claim.get("runtimeHome") != self.runtime_home or record.get("deliveryStoppedAt") or record.get("muted"):
            raise MonitorDeliveryError("monitor.claim_revoked")
        item = next((item for item in record["delivery"]["queue"] if item["id"] == claim.get("deliveryId")), None)
        if not item or item.get("token") != claim.get("token") or item.get("owner") != claim.get("owner") or item.get("leaseUntil", 0) <= self.clock():
            raise MonitorDeliveryError("monitor.claim_expired")
        return item

    async def renew(self, claim: dict[str, Any], *, lease_ms: int = 60_000) -> None:
        if not 1000 <= lease_ms <= 300_000:
            raise ValueError("invalid lease")
        def mutate(record):
            self._owned(record, claim)["leaseUntil"] = self.clock() + lease_ms
        await self._mutate(claim["monitorId"], claim["sessionId"], mutate)

    async def begin_continuation(self, claim: dict[str, Any]) -> None:
        def mutate(record):
            item = self._owned(record, claim)
            if record["delivery"]["mode"] != "continuation" or item.get("executionStarted"):
                raise MonitorDeliveryError("monitor.execution_already_started")
            item["executionStarted"] = True
        await self._mutate(claim["monitorId"], claim["sessionId"], mutate)

    async def checkpoint(self, claim: dict[str, Any], text: str | None, *, rationale: str = "") -> None:
        if text is None and not rationale.strip():
            raise ValueError("no-visible-update requires a rationale")
        if text is not None and (not text.strip() or len(text) > 32768):
            raise ValueError("authored update must contain 1..32768 characters")
        def mutate(record):
            item = self._owned(record, claim)
            if text is None and item["reason"] in {"completed", "failed", "stopped"}:
                raise ValueError("terminal delivery requires a visible result")
            if item.get("result"):
                if item["result"]["text"] != text:
                    raise MonitorDeliveryError("monitor.result_conflict")
                return
            item.update(state="authored", result={"text": text, "rationale": rationale[:1024]}, authoredAt=now_iso())
        await self._mutate(claim["monitorId"], claim["sessionId"], mutate)

    async def publish(self, claim: dict[str, Any], publisher: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Validate authority, append idempotently, then acknowledge, under one lock.

        Only the bounded durable append belongs in publisher, never a model call
        or network fanout. Stop/mute use this same lock. The callback must return
        a JSON receipt and find a prior append after an ambiguous engine crash.
        """
        async def mutate(record):
            item = self._owned(record, claim)
            if not item.get("result"):
                raise MonitorDeliveryError("monitor.result_missing")
            task = await read_task_meta(record["taskId"])
            if task and task.get("sessionId") != claim["sessionId"]:
                raise MonitorDeliveryError("monitor.task_not_owned")
            payload = {**item["envelope"], **item["result"]}
            if payload["text"] is None:
                receipt = {"deliveryId": item["id"], "visible": False, "rationale": payload["rationale"]}
            else:
                async with asyncio.timeout(5):
                    receipt = await maybe_await(publisher(payload))
                if not isinstance(receipt, dict) or receipt.get("deliveryId") != item["id"]:
                    raise MonitorDeliveryError("monitor.invalid_receipt")
            state = record["delivery"]
            stamp = now_iso()
            state.update(lastReceipt=receipt, lastProcessedAt=stamp,
                         processedBytes=item["toByte"], processedGeneration=item["generation"])
            if payload["text"] is not None:
                state.update(publishedBytes=item["toByte"], publishedGeneration=item["generation"], lastPublishedAt=stamp)
                record.update(lastAssistantUpdateAt=stamp, lastAssistantUpdateSummary=payload["text"], lastNarratedOutputSize=item["toByte"])
            for queued in state["queue"]:
                if queued["generation"] == item["generation"] and queued["id"] != item["id"]:
                    queued["fromByte"] = max(queued["fromByte"], item["toByte"])
            state["queue"] = [queued for queued in state["queue"] if queued["id"] != item["id"]]
            return receipt
        return await self._mutate(claim["monitorId"], claim["sessionId"], mutate)

    async def publish_to_session(self, claim: dict[str, Any], session: dict[str, Any],
                                 config: dict[str, Any], publisher: Callable) -> dict[str, Any]:
        """Notification transcript persistence plus the host journal handshake.

        Host must serialize this with foreground/continuation session turns.
        A persisted authored result remains evidence if publication later fails.
        """
        if session.get("id") != claim["sessionId"]:
            raise MonitorDeliveryError("monitor.session_mismatch")
        async def append(payload):
            if payload["mode"] == "notification":
                from ..session.persistence import persist_session
                metadata = session.setdefault("metadata", {})
                ids = metadata.setdefault("monitorNotificationIds", [])
                if payload["deliveryId"] not in ids:
                    session.setdefault("messages", []).append({"role": "assistant", "content": payload["text"],
                                                               "metadata": {"monitorDeliveryId": payload["deliveryId"]}})
                    metadata["monitorNotificationIds"] = [*ids, payload["deliveryId"]][-64:]
                await persist_session(session, config)
            return await maybe_await(publisher(payload))
        return await self.publish(claim, append)

    @staticmethod
    def notification_text(envelope: dict[str, Any]) -> str:
        """Provider-independent, tool-free notice; never claims verification."""
        from .monitor_narration import _build_deterministic_monitor_update
        return _build_deterministic_monitor_update(
            {"goal": envelope["goal"], "taskId": envelope["taskId"]},
            {"status": envelope["taskStatus"], "exitCode": envelope.get("exitCode")},
            envelope["reason"], envelope.get("outputExcerpt", ""), envelope.get("omittedBytes", 0))

    async def retry(self, claim: dict[str, Any], error: str) -> None:
        def mutate(record):
            item = self._owned(record, claim)
            uncertain = item.get("executionStarted") and not item.get("result")
            item.update(state="failed" if uncertain or item["attempts"] >= MAX_ATTEMPTS else "retryable",
                        error="monitor.execution_uncertain" if uncertain else error[:512],
                        availableAt=self.clock() + min(60_000, 1000 * 2 ** item["attempts"]))
            for key in ("token", "owner", "leaseUntil"):
                item.pop(key, None)
        await self._mutate(claim["monitorId"], claim["sessionId"], mutate)

    async def set_muted(self, monitor_id: str, session_id: str, muted: bool) -> None:
        def mutate(record):
            replacement = deepcopy(mute_monitor_record(record, muted, now_iso())["record"])
            record.clear()
            record.update(replacement)
        await self._mutate(monitor_id, session_id, mutate)

    async def retry_failed(self, monitor_id: str, session_id: str) -> None:
        """Explicit operator retry; never repeat uncertain tool execution."""
        def mutate(record):
            for item in record["delivery"]["queue"]:
                if item["state"] != "failed":
                    continue
                if item.get("executionStarted") and not item.get("result"):
                    raise MonitorDeliveryError("monitor.execution_uncertain")
                item.update(state="authored" if item.get("result") else "pending", attempts=0, availableAt=self.clock())
                for key in ("token", "owner", "leaseUntil"):
                    item.pop(key, None)
        await self._mutate(monitor_id, session_id, mutate)

    async def stop(self, monitor_id: str, session_id: str) -> None:
        from .monitor_transitions import stop_monitor_record
        def mutate(record):
            replacement = deepcopy(stop_monitor_record(record, now_iso())["record"])
            record.clear()
            record.update(replacement)
        await self._mutate(monitor_id, session_id, mutate)
