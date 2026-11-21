# Events

Everything the runtime does is reported as an event. Events are how you stream text to a
user interface, draw a tool call as it happens, write an audit log, or measure cost.

## Receiving events

For one session:

```python
unsubscribe = session.subscribe(lambda envelope: print(envelope["event"]))
```

For every session, through your listener:

```python
class Listener:
    def on_event(self, session_id, envelope): ...
    def on_runtime_event(self, event, data): ...     # not tied to one session
```

Handlers are called in order and should return quickly. Do slow work elsewhere.

## The envelope

```python
{
    "event": "tool.started",
    "data": {"name": "Read", "tool_use_id": "toolu_01", "input": {...}, "session_id": "..."},
    "typedEvent": {
        "event": "tool.started",
        "kind": "...",
        "payload": {...},
        "createdAt": "2027-03-01T17:04:11.512Z",
        "sessionId": "...",
    },
}
```

- `event` is the name.
- `data` is the payload as the runtime produced it.
- `typedEvent` is the same event normalised, with a timestamp and session id. Use it when
  you store or forward events.

## A turn, in events

```
turn.started
  message.user
  provider.request_preflight         the size of the request about to be sent
  message.assistant.partial ...      text as it arrives
  provider.response_completed        usage for this model call
  message.assistant                  a complete assistant message
  permission.requested               only if the call needs approval
  permission.decided
  tool.started
  tool.completed
  provider.request_preflight         the model sees the tool result
  ...
  message.assistant
turn.completed
```

## Catalogue

### Turns and messages

| Event | Meaning |
|---|---|
| `turn.started` | A turn began |
| `turn.completed` | It finished. `data`: `stop_reason`, `message`, `model` |
| `turn.failed` | It ended with an error |
| `turn.cancelling`, `turn.cancelled` | A cancel was requested, then took effect |
| `message.user`, `message.assistant` | A message was added to the conversation |
| `message.assistant.partial` | Text as it is generated. `data`: `text_delta`, and `text` so far |
| `message.assistant.thinking.partial` | Reasoning as it is generated, for models that expose it |
| `message.queued`, `message.delivered`, `message.requeued` | Input waiting for, or handed to, a turn |

### Tools and permissions

| Event | Meaning |
|---|---|
| `tool.started` | A tool call began. `data`: `name`, `tool_use_id`, `input` |
| `tool.completed` | It ended, with its result or error |
| `tool.result_guard` | A result was too large to return whole and was saved to a file |
| `permission.requested` | A call needs a decision |
| `permission.decided` | The decision, and whether a rule, the mode or a person made it |
| `hook.completed` | A hook ran |

### Providers

| Event | Meaning |
|---|---|
| `provider.request_preflight` | Size estimate before sending |
| `provider.request_started` | A model call was sent |
| `provider.stream_event` | A raw frame of the provider's stream. Needs `stream_provider_messages=True`. |
| `provider.activity` | The call is alive: a heartbeat for progress indicators |
| `provider.response_completed` | The call finished, with usage |
| `provider.retrying`, `provider.recovered` | A transient failure and its recovery |
| `provider.fallback` | The fallback model took over |
| `provider.failed` | The call failed for good |
| `provider.prompt_cache` | What the prompt cache did |

### Context

| Event | Meaning |
|---|---|
| `context.raw_pressure` | How full the context window is |
| `compaction.decision` | Whether compaction will run, and why |
| `compaction.started`, `compaction.completed`, `compaction.failed`, `compaction.skipped` | Its progress |

### Background work

| Event | Meaning |
|---|---|
| `task.registered` | A background task or sub-agent started |
| `task.updated`, `task.snapshot`, `task.output_delta` | Progress |
| `task.completed`, `task.failed`, `task.stopped` | It ended |
| `subagent.status_changed`, `subagent.message`, `subagent.question` | Sub-agent activity |
| `monitor.created`, `monitor.snapshot`, `monitor.status_changed` | Monitor activity |

### MCP

| Event | Meaning |
|---|---|
| `mcp.server_connected`, `mcp.server_connection_failed` | Connection results |
| `mcp.tools_changed`, `mcp.resources_changed` | A server changed what it offers |
| `mcp.authorization_required` | A remote server is waiting for sign-in |

### Runtime

These arrive through `on_runtime_event`.

| Event | Meaning |
|---|---|
| `scheduler.job_changed`, `scheduler.job_firing`, `scheduler.fire_failed` | Scheduler activity |
| `daemon.lost`, `daemon.recovering`, `daemon.recovered` | The task daemon failed and was restarted |

New events are added over time. Ignore names you do not recognise.

## Recipes

**Stream text to a user interface.** Append each `message.assistant.partial` delta, and
treat `message.assistant` as the authoritative text when it arrives.

**Show tool activity.** Pair `tool.started` and `tool.completed` by `tool_use_id`.

**Audit log.** Persist `typedEvent` for `permission.requested`, `permission.decided`,
`tool.started`, `tool.completed` and `turn.completed`. Together they answer: what was
asked, who allowed it, what ran and what came back.

**Cost tracking.** Sum `usage` from `provider.response_completed`, or read `usage` and
`estimatedCostUsd` from each `run_turn` result.

## Sensitive content

Events carry tool inputs and results, which may contain user data. Image payloads and
large inline data are redacted from the command line's `stream-json` output, and long
strings are truncated there. If you forward events to a third party, apply your own
policy to `data.input` and tool results first.
