"""Firing a ``turn`` job: an agent turn in the job's session, or a notice.

The session is reopened through the host if it is closed (``options_for(id,
"scheduled")``; a refusal with ``retry`` defers the fire, one without skips
it), and the turn waits behind the owner's turns like any background work,
with the origin ``scheduled``. Its prompt says it was started by the
scheduler, not by a message, and carries the job's instructions.

A fire that cannot reach the agent (the turn failed, the runtime was stopped
mid-turn, the fire came too late) is never silent: the host's
``publish_schedule_notice(session_id, notice)`` receives a deterministic
notice with the job's own words, keyed by ``noticeId`` so the host keeps it
once.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from .._utils import maybe_await
from ..runtime.session_monitors import turn_failed
from ..runtime.session_options import RuntimeStopped, SessionEnded, SessionUnavailableError, UnknownSessionError
from ..runtime.session_wake import background_limits
from . import rules

REASON_TEXT = {
    "failed": "could not run",
    "interrupted": "was interrupted (the host stopped while it ran) and was not repeated",
    "skipped_late": "was skipped: it came too late to be useful",
}


def scheduled_prompt(job: dict[str, Any], fire: dict[str, Any]) -> str:
    timezone = job["timezone"]
    scheduled = rules.parse(fire["scheduledFor"])
    fired = rules.parse(fire["firedAt"])
    late = fire.get("lateSeconds") or 0
    lines = [
        "<scheduled-wake>",
        "The scheduler started this turn; it is not a new message from the user.",
        f"Job: {job['label']} (id {job['id']}, created by {job.get('createdBy') or 'unknown'})",
        f"Scheduled for: {rules.local_text(scheduled, timezone)}",
        f"Fired at: {rules.local_text(fired, timezone)}" + (f" (late by {rules.span(late)})" if late >= 60 else ""),
    ]
    if rules.recurring(job["rule"]):
        lines.append(f"Repeats: {rules.describe(job['rule'], timezone)}")
    lines += ["Instructions:", job.get("instructions") or "", "</scheduled-wake>"]
    return "\n".join(lines)


async def fire_turn(scheduler: Any, job: dict[str, Any], fire: dict[str, Any]) -> dict[str, Any]:
    """Run the job's turn. Returns the outcome for the job's record:
    ``{"outcome", "detail", "cancel"?}``, or ``{"deferred": reason}`` when the
    host asked to be asked again later (nothing ran)."""
    manager = scheduler.runtime.sessions
    session_id = job["sessionId"]
    try:
        await manager.open(session_id, reason="scheduled")
    except SessionEnded:
        return {"outcome": "refused", "detail": "session_ended", "cancel": True}
    except UnknownSessionError:
        return {"outcome": "refused", "detail": "unknown_session", "cancel": True}
    except SessionUnavailableError as refused:
        if refused.retry:
            return {"deferred": refused.reason}
        return {"outcome": "refused", "detail": refused.reason}
    except RuntimeStopped:
        return {"deferred": "runtime_stopped"}
    live = manager.live(session_id)
    if live is None:
        return {"deferred": "session_closed"}
    turn_id = f"sched-{uuid.uuid4().hex[:16]}"
    failure: str | None = None
    try:
        result = await live.turns.run(scheduled_prompt(job, fire), origin="scheduled", limits=background_limits(), turn_id=turn_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        failure = str(error)[:500] or type(error).__name__
    else:
        if result.get("stopReason") == "cancelled":
            return {"outcome": "cancelled", "detail": turn_id}
        if turn_failed(result):
            failure = str(result.get("error") or result.get("stopReason") or "failed")[:500]
    if failure is not None:
        await publish_notice(scheduler, job, fire, "failed", failure)
        return {"outcome": "failed", "detail": failure}
    return {"outcome": "completed", "detail": turn_id}


async def publish_notice(scheduler: Any, job: dict[str, Any], fire: dict[str, Any], reason: str, detail: str | None) -> None:
    """Tell the owner, in the job's own words, that its fire did not reach
    the agent. Only for jobs that belong to a session."""
    session_id = job.get("sessionId")
    if not session_id:
        return
    scheduled = rules.parse(fire["scheduledFor"])
    text = f"Scheduled: “{job['label']}” for {rules.local_text(scheduled, job['timezone'])} {REASON_TEXT.get(reason, reason)}"
    text += f" ({detail})." if detail else "."
    if job.get("instructions"):
        text += f"\nIt was to: {job['instructions']}"
    notice = {
        "noticeId": f"{fire['fireKey']}:{reason}",
        "jobId": job["id"],
        "label": job["label"],
        "reason": reason,
        "detail": detail,
        "scheduledFor": fire["scheduledFor"],
        "timezone": job["timezone"],
        "text": text,
    }
    publisher = getattr(scheduler.runtime.listener, "publish_schedule_notice", None)
    if not callable(publisher):
        scheduler.report("scheduler.notice_unpublished", {"session_id": session_id, **notice})
        return
    try:
        await maybe_await(publisher(session_id, notice))
    except Exception as error:
        scheduler.report("scheduler.notice_failed", {"session_id": session_id, "job_id": job["id"], "error": str(error)[:500]})


async def run_host_job(handler: Any, job: dict[str, Any], fire: dict[str, Any]) -> dict[str, Any]:
    """A host kind's handler; it may name the next fire (``nextFireAt``: an
    ISO instant or an aware datetime) and its outcome."""
    returned = await maybe_await(handler(dict(job), dict(fire)))
    outcome: dict[str, Any] = {"outcome": "completed", "detail": None}
    if isinstance(returned, dict):
        outcome["outcome"] = str(returned.get("outcome") or "completed")
        outcome["detail"] = returned.get("detail")
        following = returned.get("nextFireAt")
        if following is not None:
            with contextlib.suppress(Exception):
                outcome["next"] = following if hasattr(following, "astimezone") else rules.parse(str(following))
    return outcome
