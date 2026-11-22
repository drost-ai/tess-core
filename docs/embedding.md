# Embedding guide

Tess Core is a library. Your application, the **host**, starts a runtime, tells it how
sessions should be configured, and listens to what happens. This page covers that
interface.

## Starting and stopping

```python
from tess_core import start_runtime

runtime = await start_runtime(
    "/var/lib/my-agent",            # runtime home; default: $TESS_CORE_HOME
    cwd="/srv/workspace",           # default working directory for sessions
    provider_registry="providers.yaml",
    session_options=options_for,    # called every time a session opens
    listener=host,                  # receives events and answers questions
    daemon_enabled=True,            # False: no background tasks or sub-agents
)
...
report = await runtime.stop()
```

`start_runtime` returns when the runtime is ready. `stop()` ends running turns, closes
sessions, stops workers and the daemon, and returns a report of what it did.

| Argument | Purpose |
|---|---|
| `runtime_home` | Where state lives. One host process per home. |
| `cwd` | Workspace for sessions that do not name their own. |
| `provider_registry` | A path or `ProviderRegistry`; pins the models every session may use. |
| `session_options` | `(session_id, reason) -> SessionOptions`. May be async. |
| `listener` | Your host object; every method is optional. |
| `daemon_enabled` | The task supervisor daemon. Required for sub-agents and background tasks. |
| `shared_mcp_servers` | Names of MCP servers to run once and share across sessions. |
| `startup_timeout_seconds` | Bound on daemon startup. |

## Configuring sessions

The runtime asks your `session_options` function how to configure a session whenever it
opens one: when you call `open()`, and also when it reopens a closed session by itself.
The `reason` tells you why (`client`, `child_message`, `scheduled`, and others).

```python
from tess_core import SessionOptions, Unavailable

def options_for(session_id, reason):
    if not tenant_is_active(session_id):
        return Unavailable("tenant suspended", retry=False)      # refuse this open
    return SessionOptions(
        provider="claude",
        model=None,                      # None: the profile's default
        permission_mode="default",
        cwd="/srv/workspaces/acme",
        add_dirs=("/srv/shared-docs",),  # extra directories file tools may use
        system_prompt=None,              # replace the base prompt
        timezone="America/Vancouver",
        host_tools=(orders,),            # your ToolDefinitions
        mcp_servers=(crm_server,),       # per-session MCP server descriptors
    )
```

The most used fields:

| Field | Meaning |
|---|---|
| `provider`, `model`, `effort` | Which profile, model and reasoning effort. Fixed at creation; a reopened session keeps what it was created with. |
| `permission_mode` | See [Permissions and security](security.md). |
| `cwd`, `add_dirs` | The workspace and additional allowed roots. |
| `system_prompt` | Replaces the base system prompt. |
| `context_provider` | `async (session_id) -> str`: text added to the system prompt for each turn, never stored in the conversation. |
| `host_tools` | Tools implemented by your application. |
| `extra` | Further configuration by key, such as the session's [tool selection](#choosing-a-sessions-tools). |
| `mcp_servers`, `strict_mcp_config` | Session MCP servers; strict mode ignores servers configured elsewhere. |
| `settings` | A settings file (JSON or YAML) for this session, e.g. permission rules. |
| `execution_budget` | A shared ceiling on model and tool calls across turns. |
| `stream_provider_messages` | Also emit the provider's raw stream frames as `provider.stream_event`. Text deltas arrive as `message.assistant.partial` either way. |
| `rebind_provider` | Move an existing session to this `provider`, `model` and `effort`. Without it an existing session keeps what it was opened with. See [Providers](providers.md). |
| `provider_override` | Replace the model transport, for tests. See [Testing](testing.md). |

Returning `Unavailable(reason, retry=True)` leaves pending work on disk and asks the
runtime to offer it again later.

## Sessions and turns

```python
session = await runtime.sessions.open()              # new session
session = await runtime.sessions.open(saved_id)      # resume

result = await session.run_turn(
    "Draft the release notes.",
    limits={"maxProviderCalls": 20, "maxToolCalls": 50, "timeoutMs": 300_000},
    tools=["Read", "Grep", "Glob"],     # narrow this one turn; [] for no tools
)
```

`run_turn` returns a dictionary:

| Key | Meaning |
|---|---|
| `text` | The assistant's final text |
| `stopReason` | Why the turn ended: `end_turn` for a normal completion, `max_tokens`, `refusal`, `cancelled`, or a limit the runtime applied. The same names are used for every provider. |
| `usage` | Token counts |
| `model` | The model that answered |
| `toolCount` | Tool calls made |
| `estimatedCostUsd` | Estimated cost when pricing is known |

Other calls on a session:

| Call | Purpose |
|---|---|
| `session.id`, `.model`, `.permission_mode`, `.message_count`, `.tool_names` | Read-only state. `tool_names` is the tools a turn is offered. |
| `session.busy`, `session.turns()` | Whether a turn is running, and the queue |
| `await session.cancel_turn(turn_id)` | Cancel one turn |
| `session.subscribe(fn)` | Receive this session's events; returns an unsubscribe function |
| `session.set_metadata(key, value)` | Attach a host-owned note to the session record |
| `await session.mcp_call(server, tool, arguments)` | Call one of the session's MCP tools from host code |
| `await session.close()` | Unload; the session stays resumable |
| `await session.end()` | Finish it for good |

Turns in one session run in order. Calling `run_turn` while another turn runs queues it.
Pass `turn_id=` to name a turn before it starts, so you can cancel it or correlate events.

## The listener

A listener is any object. The runtime calls the methods it finds:

```python
class Host:
    def on_event(self, session_id, envelope):
        """Every event of every session: {"event": name, "data": {...}}."""

    def on_session_opened(self, session_id, reason): ...
    def on_session_closed(self, session_id, reason): ...

    def on_runtime_event(self, event, data):
        """Runtime-level events: scheduler changes, daemon restarts."""

    async def ask_question(self, session_id, question, options, detail=None):
        """The agent needs an answer: a permission approval or a question it
        asked the user. Return one of `options`."""
        return options[0]

    async def publish(self, session_id, delivery):
        """A background monitor's notification: something a watched task did
        that nobody was waiting on. Store it, show it, and return a receipt."""
```

Without `ask_question`, sessions are non-interactive: anything that would need an answer
is refused, and the model is told why.

Without `publish`, the runtime does not fire scheduled jobs or deliver monitor
notifications, because it would have nowhere to put output nobody is waiting for.

The reply to a scheduled turn and the result of a sub-agent do not come through `publish`.
They arrive as ordinary session events in `on_event`: a `turn.started` whose `origin` is
`scheduled` or `child_message`, then `message.assistant` and `turn.completed`.

## Your own tools

```python
import json
from tess_core import ToolDefinition

async def lookup_order(arguments, context):
    try:
        order = await orders_api.get(arguments["order_id"])
    except NotFound:
        return {"content": "No such order.", "isError": True}
    return {"content": json.dumps(order)}

orders = ToolDefinition(
    name="LookupOrder",
    description="Look up an order's status by its id.",
    inputSchema={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "The order number"}},
        "required": ["order_id"],
    },
    handler=lookup_order,
    concurrency="parallel",
)
```

- The handler receives the validated arguments and a context dictionary, and returns
  `{"content": str}`, with `"isError": True` when it failed.
- `concurrency="parallel"` lets the call run alongside others. Leave it out for tools with
  side effects; they run one at a time.
- Host tools are subject to the session's allow and deny rules like any other tool.

See [Tools](tools.md) for the built-in set.

### Choosing a session's tools

A session starts with every built-in tool plus your own. To change that, pass tool names
in `extra` when the session is set up:

```python
SessionOptions(
    host_tools=(orders, workspace),
    extra={"tools": ["Orders", "Workspace"]},          # only these; no built-in tools
)

SessionOptions(
    host_tools=(orders,),
    extra={"disallowedTools": ["Bash", "Write", "Edit"]},   # everything except these
)
```

`tools` names the only tools the session has, so list your host tools and any MCP tools
you want alongside the built-in ones you keep. `allowedTools` narrows the same way, and
`disallowedTools` removes names and keeps the rest. An empty `tools` list gives a session
with no tools.

Use the first form when your tools run somewhere other than the machine the runtime is
on, such as a remote sandbox: with the built-in file, shell and web tools left out, the
model can only act through the tools you supply.

The lists decide what the model is offered, and a tool that is left out cannot be
called. `session.tool_names` shows the result. To narrow a single turn instead, pass
`tools=` to `run_turn`.

## Runtime status

```python
runtime.status()            # sync: sessions, daemon, scheduler, pid, home
await runtime.inspect()     # async: a deeper report
await runtime.wait_until_idle(timeout=None)   # until turns and sub-agents are done
runtime.scheduler           # see the scheduler guide
```

`status()` is cheap and safe to call from a health endpoint.

## A web service in forty lines

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from tess_core import SessionOptions, start_runtime

class Host:
    def __init__(self): self.events = {}
    def on_event(self, session_id, envelope):
        self.events.setdefault(session_id, []).append(envelope)

host = Host()

@asynccontextmanager
async def lifespan(app):
    app.state.runtime = await start_runtime(
        "/var/lib/agent",
        provider_registry="providers.yaml",
        listener=host,
        session_options=lambda sid, reason: SessionOptions(provider="claude", permission_mode="auto"),
    )
    yield
    await app.state.runtime.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/sessions")
async def create():
    session = await app.state.runtime.sessions.open()
    return {"id": session.id}

@app.post("/sessions/{session_id}/turns")
async def turn(session_id: str, body: dict):
    session = await app.state.runtime.sessions.open(session_id)
    return await session.run_turn(body["prompt"])
```

FastAPI is not a dependency of Tess Core; any async framework works the same way.

## Rules of thumb

- Start one runtime per process and keep it for the life of the process.
- Keep `session_options` fast and free of side effects; it is called often.
- Treat the runtime home as the source of truth. Store session ids, not transcripts.
- Never hold a session handle across a restart. Reopen by id.
