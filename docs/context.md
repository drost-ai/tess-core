# Context management

A model has a finite context window and a long conversation does not. Managing that gap is
most of what separates an agent that works for an afternoon from one that works for a
month. Tess Core does it for you.

## Token accounting

Before every request the runtime estimates its size: the system prompt, the tools, the
conversation, images. It knows the profile's context length and output budget, so an
oversized request is caught locally and handled, instead of being sent and rejected.

The estimate is refined with real usage from each response.

## Compaction

When the conversation approaches the window, the runtime **compacts** it:

1. It chooses a cutoff, keeping the recent turns intact.
2. A model writes a checkpoint of everything before the cutoff: the goal, decisions made,
   files touched, work in progress, open questions.
3. The checkpoint is **verified** against the original before it is accepted.
4. The conversation continues from the checkpoint plus the recent turns.

Nothing is thrown away. The full transcript stays on disk, and each compaction is recorded
under `compactions/` in the runtime home.

Compaction happens automatically at a threshold, and a failed or unverified compaction
never replaces good history. It can use a different, cheaper model than the conversation:
set `compactionProviderProfile` in settings.

## Large tool results

One `cat` of a log file can be bigger than the whole window. Tool results are budgeted:

- per result,
- per batch of parallel calls,
- per turn.

A result over budget is saved as an **artifact** and replaced in the conversation by a
bounded excerpt and an artifact id. The model reads more with `ReadToolArtifact` only if
it needs to. The original is never lost.

## Prompt caching

For providers that support it, the runtime arranges requests so the stable prefix (system
prompt, tools, earlier conversation) is cached. Long sessions get cheaper and faster with
no work from you. Cache behaviour is reported in usage and in `provider.*` events.

## Per-turn context from your host

Some context is true now and should not be written into history: the user's local time, a
retrieved memory, the page they are looking at. Supply it with `context_provider`:

```python
async def context_for(session_id):
    return f"Current time: {now_in_user_zone()}\nOpen ticket: {current_ticket(session_id)}"

SessionOptions(provider="claude", context_provider=context_for)
```

The text is added to the system prompt for that turn only. It grants no tools or
permissions, and it is never persisted as conversation.

## Images

Images in the conversation count against the window like text, at a known cost per image.
The runtime budgets them, downsizes where the provider allows, and drops the oldest when
it must.

## Limits you can set

| What | Where |
|---|---|
| Model calls, tool calls and time for one turn | `session.run_turn(..., limits=...)` |
| A shared ceiling across turns | `SessionOptions.execution_budget` |
| Ceilings for turns the runtime starts itself | `sessions.background_*` settings |
| Loop detection | `agent.no_progress_limit`, `agent.repeated_tool_cycle_limit` |

The **progress guard** deserves a mention: a turn that repeats the same tool cycle, or
makes no progress for too many steps, is stopped with a reason, instead of spending your
budget going in circles.

## Events

| Event | Meaning |
|---|---|
| `provider.request_preflight` | The size estimate for a request |
| `context.raw_pressure` | How full the window is |
| `compaction.decision`, `compaction.headroom` | Whether and why compaction ran |
| `tool.result_guard` | A tool result was budgeted |

## Settings

The `compaction`, `context`, `payloads` and `images` sections of the
[settings reference](reference/settings.md) hold the thresholds and budgets. The defaults
suit long coding and assistant sessions; change them when you have measured a reason.
