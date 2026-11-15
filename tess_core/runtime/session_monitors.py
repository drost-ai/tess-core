"""Monitor deliveries through the manager.

The engine's durable monitor outbox (``tasks.monitor_delivery``) records what
a watched task did and what is still to be told. This module is the consumer
a host used to be: one intake loop per runtime observes every monitor, lists
pending deliveries in rotating pages, and offers each one to its session as a
job with the origin ``monitor``: the session is reopened through the host if
it is closed (``options_for(id, "monitor")``; ``Unavailable`` defers, visibly),
and the job waits in the session's queue behind the user's turns like any
background work.

One delivery is one operation: claim, author (a deterministic notice, or an
explicit continuation turn run inside the job's slot with its provisional
events suppressed), checkpoint, publish, retry bookkeeping and cleanup, under
one total deadline when the configuration sets one, with a late claim never
starting work and a late callback never publishing. The host supplies only
``await listener.publish(session_id, delivery) -> receipt``, idempotent by
delivery id: delivered means the receipt came back; nothing else proves it.
Unfinished cleanup quarantines the session until it settles.

Ceilings (total deadline, cleanup reserve, continuation turn limits) come
from the configuration or there are none; nothing is chosen here. The claim
lease is not a ceiling: unset, it is the controller's maximum, so a missing
setting never leaves deliveries unclaimed.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any, Callable

from .._utils import maybe_await, now_iso
from ..tasks.monitor_delivery import MAX_LEASE_MS, MonitorDeliveryController, MonitorDeliveryError
from .monitor_lifecycle import MonitorLifecycle
from .session_wake import background_limits

PAGE = 1000
TERMINAL_REASONS = {"completed", "failed", "stopped"}
FAILED_STOP_REASONS = {"error", "failed", "permission_denied"}
NO_UPDATE = "NO_MONITOR_UPDATE"


def _knob(name: str) -> int | None:
    from ..config._knobs import knob_value

    value = knob_value(name)
    return None if value is None else int(value)


def turn_failed(result: dict[str, Any]) -> bool:
    return bool(result.get("error")) or result.get("stopReason") in FAILED_STOP_REASONS


class MonitorIntake:
    """The runtime's monitor consumer: observe, page, offer to sessions."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._controller: MonitorDeliveryController | None = None
        self._task: asyncio.Task[None] | None = None
        self._cursor: str | None = None
        self._status: dict[str, Any] = {"pending": 0, "failed": 0, "muted": 0, "error": None, "lastPollAt": None}

    @property
    def controller(self) -> MonitorDeliveryController:
        if self._controller is None:
            self._controller = MonitorDeliveryController(self._manager.runtime.home)
        return self._controller

    def start(self) -> None:
        # A host that cannot publish can deliver nothing: no intake runs for
        # it, and its monitors are not even observed (a companion runtime on
        # a shared home must not touch the owner's monitor records).
        if not callable(getattr(self._manager.runtime.listener, "publish", None)):
            return
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="monitor-intake")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(int(_knob("sessions.monitor_poll_ms") or 0) / 1000)
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._status["error"] = "monitor.intake_failed"
                self._report("monitor.intake_failed", {"error": str(error)[:512]})

    async def poll(self) -> list[dict[str, Any]]:
        """One intake pass: observe every monitor, then offer what is due."""
        controller = self.controller
        await controller.observe()
        entries = await controller.list_pending(limit=PAGE, after=self._cursor)
        self._cursor = entries[-1]["cursor"] if len(entries) == PAGE else None
        now = int(time.time() * 1000)
        status: dict[str, Any] = {
            "pending": len(entries),
            "failed": sum(item["state"] == "failed" for item in entries),
            "muted": sum(bool(item["muted"]) for item in entries),
            "error": None,
            "lastPollAt": now_iso(),
        }
        runtime = self._manager.runtime
        publisher = getattr(runtime.listener, "publish", None)
        offered: list[dict[str, Any]] = []
        for item in entries:
            # Subprocess children have a durable owner, not a host mapping.
            # Their existing worker consumes this same outbox at turn boundaries;
            # even stopped/orphaned owners must never be reopened by the host.
            from ..tasks.runners.resident_monitors import subprocess_monitor_owner, fail_owner_delivery
            owner = await subprocess_monitor_owner(item["sessionId"], runtime.home)
            if owner is not None:
                status["residentPending"] = status.get("residentPending", 0) + 1
                if owner.get("status") in {"completed", "failed", "stopped", "terminated", "cancelled", "truncated", "owner_unavailable"}:
                    status["error"] = "monitor.owner_unavailable"
                    if owner.get("status") != "owner_unavailable" and item.get("error") != "monitor.owner_unavailable":
                        await fail_owner_delivery(controller, item["monitorId"], item["sessionId"])
                continue
            if item["muted"] or item["state"] == "failed" or item["availableAt"] > now or item["leaseUntil"] > now:
                continue
            if item["mode"] == "continuation" and not runtime.monitor_continuation_enabled:
                status["error"] = "monitor.continuation_disabled"
                continue
            if not callable(publisher):
                status["error"] = "monitor.publisher_missing"
                continue
            if self._manager.pending.submit(f"monitor:{item['deliveryId']}", item["sessionId"], "monitor", self._job(item)):
                offered.append(item)
        self._status = status
        return offered

    def _job(self, item: dict[str, Any]) -> Callable[[Any], Any]:
        async def run(live: Any) -> None:
            await live.turns.run(
                "",
                origin="monitor",
                limits=background_limits(),
                turn_id=uuid.uuid4().hex,
                job=lambda slot: deliver_monitor(slot, self.controller, item["monitorId"]),
            )

        return run

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _report(self, event: str, data: dict[str, Any]) -> None:
        sink = getattr(self._manager.runtime.listener, "on_runtime_event", None)
        if callable(sink):
            with contextlib.suppress(Exception):
                sink(event, data)


async def deliver_monitor(slot: Any, controller: MonitorDeliveryController, monitor_id: str) -> dict[str, Any] | None:
    """One delivery, inside the session's slot: claim, author, checkpoint,
    publish, with cleanup, under one deadline if one is configured."""
    live = slot.live
    runtime = live.manager.runtime
    session_id = live.id
    if live.monitor_cleanup_pending:
        raise RuntimeError("monitor.session_unavailable")
    # A lease is fencing, not a ceiling: with nothing configured the claim
    # takes the controller's maximum (owner's decision), renewed while the
    # work runs, so a missing setting never leaves deliveries unclaimed.
    lease_ms = _knob("sessions.monitor_claim_lease_ms") or MAX_LEASE_MS
    total_ms = _knob("sessions.monitor_total_deadline_ms")
    cleanup_ms = _knob("sessions.monitor_cleanup_ms")
    lifecycle = MonitorLifecycle(None if total_ms is None else total_ms / 1000, cleanup_seconds=0.0 if cleanup_ms is None else cleanup_ms / 1000)
    publish = runtime.listener.publish
    claim: dict[str, Any] | None = None
    worker: asyncio.Task[Any] | None = None
    failure: BaseException | None = None

    async def fenced_publisher(payload: dict[str, Any]) -> Any:
        lifecycle.check()  # a late callback never publishes
        return await maybe_await(publish(session_id, payload))

    async def author() -> dict[str, Any]:
        envelope = claim["envelope"]
        if not claim.get("result"):
            if envelope["mode"] == "notification":
                text: str | None = controller.notification_text(envelope)
            else:
                await controller.begin_continuation(claim)
                lifecycle.check()
                result = await slot.run_turn("", monitor_envelope=envelope, suppress_events=True)
                lifecycle.check()
                if turn_failed(result):
                    raise RuntimeError("monitor.continuation_failed")
                text = str(result.get("text") or "").strip()
                if text == NO_UPDATE and envelope["reason"] not in TERMINAL_REASONS:
                    text = None
                elif not text or text == NO_UPDATE:
                    raise RuntimeError("monitor.empty_terminal_response")
            await controller.checkpoint(claim, text, rationale="No meaningful progress to report" if text is None else "")
        lifecycle.check()
        return await controller.publish_to_session(claim, live.record, live.config, fenced_publisher)

    async def watch_authority() -> None:
        # Each renewal is also where a stop or mute is noticed: the claim is
        # revoked, the renewal fails, the work is abandoned.
        interval = int(_knob("sessions.monitor_renew_ms") or 0) / 1000
        while True:
            await asyncio.sleep(interval)
            lifecycle.check()
            await controller.renew(claim, lease_ms=lease_ms)
            lifecycle.check()

    async def operation() -> dict[str, Any] | None:
        nonlocal claim, worker
        claim = await controller.claim(
            monitor_id, session_id, str(uuid.uuid4()), lease_ms=lease_ms,
            continuation_supported=runtime.monitor_continuation_enabled,
        )
        lifecycle.check()  # a late claim never starts model or tools
        if claim is None:
            return None
        worker = lifecycle.start(author())
        watcher = lifecycle.start(watch_authority())
        done, _ = await asyncio.wait({worker, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            watcher.result()
        return await worker

    async def recover() -> None:
        try:
            await controller.retry(claim, getattr(failure, "code", None) or "monitor.processing_failed")
        except MonitorDeliveryError:
            pass  # stop, mute or expiry already revoked the claim

    try:
        return await lifecycle.run(operation())
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            await lifecycle.finish(recover if failure is not None and claim is not None else None)
        finally:
            live.monitor_cleanup.update(lifecycle.pending)
