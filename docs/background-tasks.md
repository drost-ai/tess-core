# Background tasks

Some work takes longer than a turn should: a test suite, a build, a data export, a server
you need to keep an eye on. Tess Core runs these in the background, lets the agent carry
on, and brings the result back when there is something to say.

## The task daemon

Background work is owned by a small supervisor process, the **task daemon**, started and
stopped with the runtime (`daemon_enabled=True`, the default). It:

- runs background shell commands and sub-agents,
- records their output and status under the runtime home,
- notices when they finish or die,
- wakes the session that is waiting for them.

Because it is a separate process, a task survives the turn that started it. Because it is
tied to the runtime, nothing survives the host: if your application exits or crashes, the
daemon stops its workers.

## Background commands

The model starts one by calling `Bash` with `run_in_background`. It gets a task id back at
once and can:

- read output with `TaskOutput` (optionally waiting, with a timeout),
- stop it with `TaskStop`.

## Monitors

Polling is wasteful, so the agent can ask to be told. `TaskMonitorStart` registers a
**monitor** on a task with a goal ("tell me when the tests finish, and whether they
passed").

A monitor delivers in one of two modes:

- **notification** (default): when the task changes state, a short factual message is
  added to the conversation. No model call is made.
- **continuation**: the agent is woken to look at the result and act on it. Use this when
  the user asked for follow-up ("and fix whatever fails").

Monitors are durable. If the session is closed when the task finishes, the runtime reopens
it to deliver. Delivery happens once, and a host can mute, retry or stop a delivery.
Continuation turns run under tight limits on model calls, tool calls and time.

Hosts that deliver to their own channels (a chat app, a notification) drive this through
`MonitorDeliveryController`. The [monitor delivery design note](design/resident-monitor-delivery.md)
describes the claim, checkpoint and publish steps.

## Health checks

`TaskHealthCheckStart` runs a periodic check of a URL or a command: an interval, a
timeout, a failure threshold and backoff. The agent is told when the check starts failing
and when it recovers, not on every success.

## Events

| Event | Meaning |
|---|---|
| `task.registered` | A background task started |
| `task.updated`, `task.snapshot` | Status or output changed |
| `task.completed`, `task.failed`, `task.stopped` | It ended |
| `monitor.snapshot` | A monitor observed its task |

## Settings

The `tasks`, `daemon` and `process` sections of the
[settings reference](reference/settings.md) control output caps, heartbeat and restart
timing, and how long shutdown waits for work to drain.

## Reliability notes

- **Restart recovery.** When a runtime starts, the daemon reconciles what the last process
  left: tasks whose worker is gone are marked lost, once.
- **Bounded output.** Task output is capped; the newest output is kept.
- **Clean shutdown.** `runtime.stop()` stops workers and waits within a bound. A worker
  that ignores a polite stop is killed.
- **No daemon, no background work.** With `daemon_enabled=False`, sub-agents and
  background tasks are unavailable, and the model is told so.
