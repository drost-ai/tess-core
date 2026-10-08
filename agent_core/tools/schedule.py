"""The agent's scheduler tools: ScheduleCreate, ScheduleList, ScheduleUpdate,
ScheduleCancel.

They exist only in sessions of a runtime whose scheduler timer runs (the
session build passes ``scheduler`` to the tool factory): elsewhere nothing
would fire. Scope never comes from model arguments: a job belongs to the
calling session, and only that session's own jobs can be listed or changed.

The model gives local wall-clock times and, if not the owner's, an IANA
zone; it never does UTC arithmetic. Every answer echoes the resolved time
(weekday, date, time, zone, how far away) and the current local time, for
the agent to confirm to the user. Codex-family models fill every optional
parameter with a zero value, so "", 0, [] and null all mean "not given".
"""
from __future__ import annotations

from typing import Any

from .._utils import pretty_json
from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult
from ..scheduler import jobs as job_rules
from ..scheduler import operations, rules
from ..scheduler.rules import RuleError
from ..scheduler.store import JobNotFound
from .definitions import ToolDefinition
from .schemas import builtin_tool

NAMES = ("ScheduleCreate", "ScheduleList", "ScheduleUpdate", "ScheduleCancel")


def schedule_tools() -> list[ToolDefinition]:
    return [_create_tool(), _list_tool(), _update_tool(), _cancel_tool()]


def _given(value: Any) -> Any:
    return None if value in (None, "", 0, [], {}) or value is False else value


def _session_id(context: ToolContext) -> str:
    session = context.get("session") or {}
    session_id = session.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("scheduler.no_session: scheduling needs a session")
    return session_id


def _timezone(input: dict[str, Any], context: ToolContext) -> str:
    given = _given(input.get("timezone"))
    owner = (context.get("config") or {}).get("ownerTimezone")
    chosen = given or owner
    if not chosen:
        raise RuleError("scheduler.timezone_required", "No time zone is configured for the owner; pass timezone (an IANA name such as America/Los_Angeles).")
    rules.zone(chosen)
    return str(chosen)


def _rule(input: dict[str, Any]) -> dict[str, Any] | None:
    at, clock, every = _given(input.get("at")), _given(input.get("time")), _given(input.get("every_minutes"))
    chosen = [name for name, value in (("at", at), ("time", clock), ("every_minutes", every)) if value is not None]
    if len(chosen) > 1:
        raise RuleError("scheduler.invalid_rule", f"Give one of at, time or every_minutes, not {' and '.join(chosen)}.")
    if at is not None:
        return {"kind": "once", "at": at}
    if clock is not None:
        return {"kind": "calendar", "time": clock, "days": _given(input.get("days")) or [], "monthDays": _given(input.get("month_days")) or []}
    if every is not None:
        if not isinstance(every, (int, float)) or every < 1:
            raise RuleError("scheduler.invalid_rule", "every_minutes must be at least 1.")
        return {"kind": "interval", "everySeconds": int(every * 60)}
    return None


def _answer(payload: dict[str, Any], timezone: str) -> ToolResult:
    now = operations.now_utc()
    return {"content": pretty_json({**payload, "now": rules.local_text(now, timezone)})}


def _failure(error: Exception) -> ToolResult:
    code = getattr(error, "code", "scheduler.error")
    return {"content": pretty_json({"error": code, "message": str(error)}), "isError": True}


def _create_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            timezone = _timezone(input, context)
            rule = _rule(input)
            if rule is None:
                raise RuleError("scheduler.invalid_rule", "Say when: at (one time), time (recurring at a time of day) or every_minutes.")
            instructions = str(_given(input.get("instructions")) or "")
            job = await operations.create_job(
                kind=job_rules.TURN, rule=rule, timezone=timezone, session_id=_session_id(context),
                label=str(_given(input.get("label")) or ""), instructions=instructions, created_by="agent",
            )
        except (RuleError, JobNotFound) as error:
            return _failure(error)
        seen = job_rules.view(job, operations.now_utc())
        return _answer({"scheduled": seen, "confirm": f"Tell the user it is set for {seen['nextFire']} ({seen['nextFireIn']})."}, timezone)

    return builtin_tool("ScheduleCreate", handler)


def _list_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        found = await operations.find_jobs(session_id=_session_id(context), include_finished=bool(_given(input.get("include_finished"))), kind=job_rules.TURN)
        now = operations.now_utc()
        try:
            timezone = _timezone({}, context)
        except RuleError:
            timezone = found[0]["timezone"] if found else "UTC"
        return _answer({"jobs": [job_rules.view(job, now) for job in found]}, timezone)

    return builtin_tool("ScheduleList", handler)


def _update_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            job_id = str(_given(input.get("job_id")) or "")
            current = await operations.get_job(job_id, session_id=_session_id(context))
            changes: dict[str, Any] = {
                "label": _given(input.get("label")),
                "instructions": _given(input.get("instructions")),
                "rule": _rule(input),
                "timezone": _given(input.get("timezone")),
            }
            state = _given(input.get("state"))
            if state is not None:
                if state not in ("active", "paused"):
                    raise RuleError("scheduler.invalid_state", "state is active (resume) or paused; use ScheduleCancel to cancel.")
                changes["state"] = state
            job = await operations.update_job(current["id"], changes, session_id=_session_id(context))
        except (RuleError, JobNotFound, ValueError) as error:
            return _failure(error)
        return _answer({"updated": job_rules.view(job, operations.now_utc())}, job["timezone"])

    return builtin_tool("ScheduleUpdate", handler)


def _cancel_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            job = await operations.update_job(str(_given(input.get("job_id")) or ""), {"state": "cancelled"}, session_id=_session_id(context))
        except (RuleError, JobNotFound, ValueError) as error:
            return _failure(error)
        return _answer({"cancelled": job_rules.view(job, operations.now_utc())}, job["timezone"])

    return builtin_tool("ScheduleCancel", handler)
