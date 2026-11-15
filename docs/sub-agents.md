# Sub-agents

A sub-agent is an agent started by another agent. The parent hands off a focused task,
keeps working, and receives the result when the child is done. It is how one conversation
does several things at once without filling its own context with the details.

## How it works

The model calls the `Agent` tool:

```json
{
  "description": "Audit dependency licences",
  "prompt": "List every dependency in pyproject.toml with its licence. Flag anything that is not permissive."
}
```

- The call **returns immediately** with the child's id. The child runs in its own process,
  with its own session and context window.
- The parent carries on. It can start more children, up to the limit.
- When the child finishes, its result is delivered to the parent as a message. If the
  parent session has gone idle and been closed, the runtime **reopens it** to deliver the
  result.

Sub-agents need the task daemon, so start the runtime with `daemon_enabled=True` (the
default).

## Talking to each other

`SendMessage` carries messages in both directions:

- A child sends to `"parent"`: progress, or a question. With `expects_reply`, the child
  waits for the answer, up to a timeout.
- A parent sends to a child id: more instructions, or a reply to its question.

A child's question surfaces in the parent's next turn. If the parent never answers, the
runtime answers on its behalf when the reply timeout passes, so a child cannot wait
forever.

## Limits

| Setting | Default | Meaning |
|---|---|---|
| `subagents.max_depth` | 2 | How deep agents may nest |
| `subagents.max_live` | 8 | Live children per runtime. At the limit, `Agent` returns the list of running children instead of starting another. |
| `subagents.resident_idle_ttl_ms` | 15 minutes | How long an idle child stays before it stops for good |
| `comms.reply_timeout_ms` | 5 minutes | How long `SendMessage` waits for a reply |

See [Configuration](configuration.md) to change them.

## Authority

A child's authority is decided at launch and never grows:

- It starts with a permission mode and rules no broader than its parent's.
- It cannot switch modes, and prompts that would widen its reach are refused.
- It cannot be given directories outside its allowed roots after it starts.
- With `"isolation": "worktree"`, it works in its own git worktree, so its edits do not
  collide with the parent's.

This is enforced from the child's own session record, not from anything the child says.

## Reliability

Sub-agents are separate processes, so the runtime plans for them failing:

- A child that crashes is reconciled: the parent is told it failed, once.
- A parent that crashes does not orphan children: the daemon stops workers whose owner is
  gone.
- A result is delivered **exactly once**, even across restarts of the parent.
- Shutting down the runtime stops its children and waits for them, within a bound.

## For hosts

Your `session_options` function is called when the runtime reopens a parent to deliver a
child's message, with the reason `child_message`. Return the options as usual, or
`Unavailable(..., retry=True)` if now is not a good time; the message stays on disk and is
offered again.

Turns the runtime starts by itself to process a child's message have their own ceilings
(`sessions.background_max_provider_calls`, `background_max_tool_calls`,
`background_timeout_ms`), so background work cannot run away.

Events to watch: `task.registered`, `task.updated`, and the terminal `task.completed`,
`task.failed` and `task.stopped`.

## When to use sub-agents

Good uses:

- Independent pieces of work that can proceed in parallel
- Exploration that would otherwise bury the main conversation in file dumps
- A second opinion with a fresh context

Poor uses:

- A single lookup. The hand-off costs more than the work.
- Tightly coupled steps where each needs the last one's full detail
