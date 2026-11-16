# Scheduler

Agents need a sense of time: "remind me on Friday", "check the build every hour", "run the
nightly report". Tess Core has a scheduler built into the runtime. Jobs are durable, they
understand time zones, and they fire either as agent turns or as functions in your host.

## Requirements

The scheduler runs in a started runtime whose listener can deliver output, because a job
that fires with nobody waiting needs somewhere to put its result. Give your listener a
`publish` method (see the [embedding guide](embedding.md#the-listener)).

Reach it at `runtime.scheduler`.

## Two kinds of job

**Turn jobs** run an agent turn in a session:

```python
job = await runtime.scheduler.add(
    kind="turn",
    session_id=session.id,
    rule={"kind": "calendar", "time": "09:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
    timezone="Europe/London",
    label="Morning briefing",
    instructions="Summarise overnight alerts and anything due today.",
    created_by="owner",
)
```

When it fires, the session is reopened if it was closed, and the turn runs with the origin
`scheduled`, so the agent knows this was a timer and not a person. The agent's reply arrives
in your listener's `on_event`, like the events of any other turn.

**Host jobs** call your code:

```python
async def rotate_logs(job, fire):
    await do_rotation()
    return {"outcome": "rotated"}

runtime.scheduler.register("logs.rotate", rotate_logs)
await runtime.scheduler.ensure(
    "nightly-log-rotation",
    kind="logs.rotate",
    rule={"kind": "interval", "everySeconds": 86400},
    timezone="UTC",
    label="Rotate logs",
    created_by="host",
)
```

`ensure` creates a standing job if it is missing and otherwise leaves it alone, so it is
safe to call on every start. A handler may return `nextFireAt` to choose its own next
time.

## Rules

| Rule | Example |
|---|---|
| Once, local time | `{"kind": "once", "at": "2027-03-01T14:30"}` |
| Once, UTC | `{"kind": "once", "atUtc": "2027-03-01T22:30:00Z"}` |
| Calendar, every day | `{"kind": "calendar", "time": "09:00"}` |
| Calendar, days of the week | `{"kind": "calendar", "time": "09:00", "days": ["mon", "fri"]}` |
| Calendar, days of the month | `{"kind": "calendar", "time": "08:00", "monthDays": [1, 15]}` |
| Every hour, at a minute | `{"kind": "hourly", "minute": 0}` |
| Interval | `{"kind": "interval", "everySeconds": 3600}` |

Time zones are IANA names. Daylight-saving changes are handled deliberately: a one-time
local time that does not exist or occurs twice is refused with a clear error, and a
recurring job fires at the mapped instant.

## Managing jobs

```python
await runtime.scheduler.list(session_id=None, include_finished=False)
await runtime.scheduler.get(job_id)
await runtime.scheduler.update(job_id, rule=..., label=..., instructions=...)
await runtime.scheduler.pause(job_id)
await runtime.scheduler.resume(job_id)
await runtime.scheduler.cancel(job_id)
runtime.scheduler.view(job)        # a job rendered in its own time zone
runtime.scheduler.status()         # running, counts, next fire, recent fires, last error
```

Jobs are stored under `scheduler/jobs` in the runtime home and survive restarts.

## The agent schedules itself

Sessions of a runtime with a working scheduler get four tools: `ScheduleCreate`,
`ScheduleList`, `ScheduleUpdate` and `ScheduleCancel`. "Remind me to call Alex at three"
becomes a job, and at three the agent wakes and tells you. Set `SessionOptions.timezone`
so the agent's times mean what the user means.

## When a fire cannot run

Things go wrong at the scheduled moment: the process was down, the session is busy, the
host says not now.

- If your `session_options` returns `Unavailable(retry=True)`, the fire is deferred.
  Without `retry`, it is skipped.
- If the session has ended or no longer exists, the job is cancelled.
- If a fire is too late to be useful (`scheduler.max_late_ms`), it is skipped.
- If the turn fails or is interrupted, the runtime calls
  `publish_schedule_notice(session_id, notice)` on your listener with the job's own words,
  so the user still learns the reminder existed.

## Events

| Event | When |
|---|---|
| `scheduler.job_changed` | A job is created, updated, cancelled or fired |
| `scheduler.job_firing` | Just before a turn job's turn, with how late it is |
| `scheduler.fire_failed`, `scheduler.scan_failed` | Something went wrong |
| `scheduler.notice_failed`, `scheduler.notice_unpublished` | A notice could not be delivered |

These arrive through `on_runtime_event`.

## Settings

| Knob | Default | Meaning |
|---|---|---|
| `scheduler.clock_check_ms` | 30000 | How often due jobs are checked |
| `scheduler.max_late_ms` | unset | Skip fires later than this |
