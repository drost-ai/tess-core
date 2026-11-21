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

One `cat` of a log file can be bigger than the whole window, so a tool result has an
allowance. It is the same for every model:

- A result of up to 30,000 characters is returned whole.
- A larger one is saved to a file in the runtime home. The model gets a short notice in its
  place: the size, the file's path, and the first 2 KB of the output. Nothing from the middle
  or the end is shown, so a preview is never mistaken for the whole result.
- The model reads the rest with `Read`, on that path, when it needs it. The original is
  never lost.

`Read` bounds itself instead. It returns 2,000 lines unless the call gives a `limit`, and a
read that would be more than about 25,000 tokens is refused, not cut: the model is told to
page with `offset` and `limit`. Calling several tools at once does not shrink what any one of
them may return.

## Prompt caching

For providers that support it, the runtime arranges requests so the stable prefix (system
prompt, tools, earlier conversation) is cached. Long sessions get cheaper and faster with
no work from you. Cache behaviour is reported in usage and in `provider.*` events.

How it is done depends on the provider:

- **Anthropic** caches what a request marks. The runtime places the marks and moves them
  forward as the conversation grows.
- **OpenAI** caches automatically. The runtime gives each session a stable cache key so
  its requests reach the same cache.
- **Google Gemini** caches automatically and has nothing to mark or to key. A request is
  served from cache when it begins exactly as an earlier one did, so the runtime builds
  every request of a session to begin with the one before it.

Every result's usage reports `cache_read_input_tokens`: the input served from cache. For
Anthropic and Gemini, `input_tokens` is the input that was not.

For Anthropic and Gemini sessions, each model call also emits a `provider.prompt_cache`
event with what the cache did and one reason: `cache_read_observed`, or why not. The
common misses are a first call (`warmup_no_previous_state`), a changed system prompt,
tool list or thinking level, a rewritten conversation (`compaction_or_relief_rewrite`),
a request too small to cache (`below_cache_minimum`: Gemini caches little or nothing
under about 4,000 tokens), a long pause (`idle_gap_may_have_expired`), and a hit on an
older entry than the previous request (`partial_cache_read`), which happens now and then
on the provider's side and corrects itself on the next call.

**What to expect from Gemini.** Measured in November 2026 on `gemini-4-hydrogen`, over a
30-call tool loop whose tool results were about 2,200 tokens each: from the fourth call
on, the whole previous request was served from cache every time and only the new tokens
were billed at the full rate. 91% of the loop's input was cached and it cost 86% less
than it would have uncached. `gemini-3.8-flash` behaved the same way. When a call adds
fewer than about 2,050 tokens, Gemini reads the last 2,050 tokens of the previous
request again at the full rate, so a loop of small tool results caches less: 62% in the
same test with 300-token results. The saving grows with the conversation either way.
A pause did not cost the cache: a request sent 20 minutes after the previous one was
still served from it in full. Google does not publish how long an entry is kept.

Three things keep the cache warm: leave the system prompt and tool list alone during a
session, put per-turn context such as the time in the turn rather than the system prompt,
and expect one cold request after each compaction.

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
| `tool.result_guard` | A tool result was too large to return whole and was saved |

## Settings

The `compaction`, `context`, `payloads` and `images` sections of the
[settings reference](reference/settings.md) hold the thresholds and budgets. The defaults
suit long coding and assistant sessions; change them when you have measured a reason.
