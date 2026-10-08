"""The runtime's one scheduler timer, in the host process.

One timer per runtime that can publish (as with the monitor intake: a
companion runtime on a shared home, such as a memory distiller, never
fires). It reads the job records, fires what is due and sleeps until the next
job is due, waking early whenever a job is added or changed through this
process. It never sleeps longer than ``scheduler.clock_check_ms``: asyncio
sleeps on a monotonic clock that may stop while the machine sleeps, the wall
clock may be changed, and another process may have written a job.

A fire is one operation on the job's record: claim (with the fire's key, the
scheduled instant), mark started, run, record the outcome and advance. A fire
whose claim was left ``started`` by a runtime that stopped or died is never
run again: its job gets an honest ``interrupted`` notice instead and moves on.
A fire later than ``scheduler.max_late_ms`` (when set) is skipped, visibly;
unset, it fires late and says by how much. Missed occurrences of a recurring
job collapse into one fire.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections import deque
from datetime import timedelta
from typing import Any, Awaitable, Callable

from .._utils import is_process_alive
from . import jobs as job_rules
from . import operations, rules
from .firing import fire_turn, publish_notice, run_host_job
from .store import list_jobs, mutate_job

HostHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any] | Any]


def _knob(name: str) -> int | None:
    from ..config._knobs import knob_value

    value = knob_value(name)
    return None if value is None else int(value)


class Scheduler:
    """The runtime's scheduler: the host's API and the timer."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._handlers: dict[str, HostHandler] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._firing: dict[str, asyncio.Task[None]] = {}
        self._recent: deque[dict[str, Any]] = deque(maxlen=20)
        self._counts: dict[str, int] = {}
        self._next: str | None = None
        self._error: str | None = None
        self._last_scan: str | None = None

    # -- lifecycle -----------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not callable(getattr(self.runtime.listener, "publish", None)) or self.running:
            return
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run(), name="scheduler")
        operations._TIMERS[self.runtime.home] = self

    async def stop(self) -> None:
        if operations._TIMERS.get(self.runtime.home) is self:
            del operations._TIMERS[self.runtime.home]
        tasks = [task for task in (self._task, *self._firing.values()) if task is not None and not task.done()]
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._firing.clear()

    def wake(self) -> None:
        loop, event = self._loop, self._wake
        if loop is None or event is None or loop.is_closed():
            return
        if threading.get_ident() == self._loop_thread:
            event.set()
        else:
            loop.call_soon_threadsafe(event.set)

    # -- the host's API ------------------------------------------------

    def register(self, kind: str, handler: HostHandler) -> None:
        """The host's handler for its own job kind; jobs of a kind nobody
        registered wait until someone does."""
        if kind == job_rules.TURN or not isinstance(kind, str) or not kind:
            raise ValueError("A host job kind needs a name other than 'turn'.")
        self._handlers[kind] = handler
        self.wake()

    async def add(self, **fields: Any) -> dict[str, Any]:
        return await operations.create_job(**fields)

    async def ensure(self, job_id: str, **fields: Any) -> dict[str, Any]:
        return await operations.ensure_job(job_id, **fields)

    async def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        return await operations.update_job(job_id, changes)

    async def pause(self, job_id: str) -> dict[str, Any]:
        return await operations.update_job(job_id, {"state": "paused"})

    async def resume(self, job_id: str) -> dict[str, Any]:
        return await operations.update_job(job_id, {"state": "active"})

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return await operations.update_job(job_id, {"state": "cancelled"})

    async def get(self, job_id: str) -> dict[str, Any]:
        return await operations.get_job(job_id)

    async def list(self, *, session_id: str | None = None, include_finished: bool = False) -> list[dict[str, Any]]:
        return await operations.find_jobs(session_id=session_id, include_finished=include_finished)

    def view(self, job: dict[str, Any]) -> dict[str, Any]:
        return job_rules.view(job, operations.now_utc())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "jobs": dict(self._counts),
            "nextFireAt": self._next,
            "firing": sorted(self._firing),
            "hostKinds": sorted(self._handlers),
            "lastScanAt": self._last_scan,
            "error": self._error,
            "recent": list(self._recent),
        }

    def job_changed(self, job: dict[str, Any], reason: str) -> None:
        self.report("scheduler.job_changed", {"job_id": job["id"], "session_id": job.get("sessionId"), "kind": job["kind"], "state": job["state"], "next_fire_at": job.get("nextFireAt"), "reason": reason})
        self.wake()

    def report(self, event: str, data: dict[str, Any]) -> None:
        sink = getattr(self.runtime.listener, "on_runtime_event", None)
        if callable(sink):
            with contextlib.suppress(Exception):
                sink(event, data)

    # -- the timer -----------------------------------------------------

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                delay = await self.scan()
                self._error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._error = str(error)[:500]
                self.report("scheduler.scan_failed", {"error": self._error})
                delay = self._clock_check()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
            self._wake.clear()

    async def scan(self) -> float:
        """Fire what is due; return how long to sleep."""
        now = operations.now_utc()
        self._last_scan = rules.iso(now)
        counts: dict[str, int] = {}
        earliest = None
        for job in await list_jobs():
            counts[job.get("state", "?")] = counts.get(job.get("state", "?"), 0) + 1
            if job["id"] in self._firing:
                continue
            claim = job.get("claim")
            if claim and not self._stale(job["id"], claim):
                continue
            if claim or job_rules.due(job, now):
                if job["kind"] != job_rules.TURN and job["kind"] not in self._handlers:
                    continue  # waits for its handler
                self._firing[job["id"]] = asyncio.get_running_loop().create_task(self._fire(job["id"]), name=f"scheduler-fire-{job['id']}")
                continue
            at = job_rules.wake_at(job)
            if at is not None and (earliest is None or at < earliest):
                earliest = at
        self._counts = counts
        self._next = rules.iso(earliest) if earliest else None
        check = self._clock_check()
        return check if earliest is None else max(0.0, min(check, (earliest - now).total_seconds()))

    def _stale(self, job_id: str, claim: dict[str, Any]) -> bool:
        """A claim nobody is working on: ours but forgotten, or another
        runtime's that stopped (same process) or died."""
        if claim.get("instanceId") == self.runtime.instance_id:
            return job_id not in self._firing
        pid = claim.get("pid")
        return pid == os.getpid() or not is_process_alive(pid)

    async def _fire(self, job_id: str) -> None:
        try:
            await self._fire_once(job_id)
        except asyncio.CancelledError:
            raise  # a started claim stays: the next runtime reports it interrupted
        except Exception as error:
            self._error = str(error)[:500]
            self.report("scheduler.fire_failed", {"job_id": job_id, "error": self._error})
        finally:
            self._firing.pop(job_id, None)
            self.wake()

    async def _fire_once(self, job_id: str) -> None:
        now = operations.now_utc()
        interrupted: dict[str, Any] | None = None

        def claim(current: dict[str, Any] | None) -> dict[str, Any] | None:
            nonlocal interrupted
            if current is None or current.get("state") != "active":
                return None
            held = current.get("claim")
            if held and not self._stale(job_id, held):
                return None
            if held and held.get("started"):
                interrupted = dict(held)
                return {**current, "claim": {**held, "instanceId": self.runtime.instance_id, "pid": os.getpid()}}
            if not job_rules.due(current, now):
                return None
            scheduled = current["nextFireAt"]
            return {**current, "claim": {"fireKey": f"{job_id}@{scheduled}", "scheduledFor": scheduled, "instanceId": self.runtime.instance_id,
                                         "pid": os.getpid(), "claimedAt": rules.iso(now), "started": False}}

        job, changed = await mutate_job(job_id, claim)
        if not changed or job is None:
            return
        held = job["claim"]
        late = max(0, int((now - rules.parse(held["scheduledFor"])).total_seconds()))
        fire = {"fireKey": held["fireKey"], "scheduledFor": held["scheduledFor"], "firedAt": rules.iso(now), "lateSeconds": late}
        if interrupted is not None:
            await publish_notice(self, job, fire, "interrupted", None)
            await self._finish(job, fire, {"outcome": "interrupted", "detail": None})
            return
        max_late = _knob("scheduler.max_late_ms")
        if max_late is not None and late * 1000 > max_late:
            await publish_notice(self, job, fire, "skipped_late", f"late by {rules.span(late)}")
            await self._finish(job, fire, {"outcome": "skipped_late", "detail": f"late by {rules.span(late)}"})
            return
        job, marked = await mutate_job(job_id, lambda current: self._started(current, fire["fireKey"]))
        if not marked or job is None:
            return  # cancelled or taken over between the claim and now
        if job["kind"] == job_rules.TURN:
            outcome = await fire_turn(self, job, fire)
        else:
            try:
                outcome = await run_host_job(self._handlers[job["kind"]], job, fire)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                outcome = {"outcome": "failed", "detail": str(error)[:500] or type(error).__name__}
        if "deferred" in outcome:
            await self._defer(job, fire, str(outcome["deferred"]))
            return
        await self._finish(job, fire, outcome)

    @staticmethod
    def _started(current: dict[str, Any] | None, fire_key: str) -> dict[str, Any] | None:
        held = (current or {}).get("claim") or {}
        if held.get("fireKey") != fire_key:
            return None
        return {**current, "claim": {**held, "started": True}}

    async def _defer(self, job: dict[str, Any], fire: dict[str, Any], reason: str) -> None:
        delay_ms = _knob("sessions.pending_retry_ms") or _knob("scheduler.clock_check_ms") or 0
        retry_at = rules.iso(operations.now_utc() + timedelta(milliseconds=delay_ms))

        def release(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if not current or (current.get("claim") or {}).get("fireKey") != fire["fireKey"]:
                return None
            return {**current, "claim": None, "retryAt": retry_at}

        await mutate_job(job["id"], release)
        self._note(job, fire, {"outcome": "deferred", "detail": reason, "retryAt": retry_at})

    async def _finish(self, job: dict[str, Any], fire: dict[str, Any], outcome: dict[str, Any]) -> None:
        now = operations.now_utc()
        record = {**fire, "outcome": outcome["outcome"], "detail": outcome.get("detail"), "finishedAt": rules.iso(now)}

        def complete(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if not current or (current.get("claim") or {}).get("fireKey") != fire["fireKey"]:
                return None
            if current.get("state") != "active":  # paused or cancelled while it ran
                return {**current, "claim": None, "lastFire": record, "fireCount": int(current.get("fireCount") or 0) + 1}
            return job_rules.advance(current, fire=record, now=now, next_override=outcome.get("next"), cancel=bool(outcome.get("cancel")))

        saved, changed = await mutate_job(job["id"], complete)
        if changed and saved is not None:
            self._note(saved, fire, record)
            self.job_changed(saved, "fired")

    def _note(self, job: dict[str, Any], fire: dict[str, Any], outcome: dict[str, Any]) -> None:
        self._recent.appendleft({"jobId": job["id"], "kind": job["kind"], "sessionId": job.get("sessionId"), **fire, **outcome})

    @staticmethod
    def _clock_check() -> float:
        return int(_knob("scheduler.clock_check_ms") or 0) / 1000
