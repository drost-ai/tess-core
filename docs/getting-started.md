# Getting started

This guide takes you from an empty directory to an agent that streams its work, asks
before it changes anything, calls a tool you wrote, and picks up tomorrow where it left
off. It takes about ten minutes.

## Requirements

- Python 3.12 or newer
- An API key for a model provider, or a local OpenAI-compatible server
- macOS or Linux

## Install

```sh
pip install "tess-core @ git+https://github.com/drost-ai/tess-core.git"
```

Tess Core has no runtime dependencies, so this installs exactly one package.

## 1. Declare a model

Create `providers.yaml`. A profile names a provider, a default model, and where the
credential comes from:

```yaml
schema_version: 1
providerProfiles:
  claude:
    kind: anthropic
    defaultModel: claude-sonnet-5-5
    auth:
      kind: anthropic_api_key
      envVar: ANTHROPIC_API_KEY
```

Export the key in your shell:

```sh
export ANTHROPIC_API_KEY=...
```

The key never goes in the file. See [Providers](providers.md) for OpenAI, compatible
endpoints and local models.

## 2. Run one turn

```python
# agent.py
import asyncio
from tess_core import SessionOptions, start_runtime

async def main():
    runtime = await start_runtime(
        "./agent-home",
        provider_registry="providers.yaml",
        session_options=lambda session_id, reason: SessionOptions(provider="claude"),
    )
    try:
        session = await runtime.sessions.open()
        result = await session.run_turn("List the Python files here and say what each one does.")
        print(result["text"])
        print("session:", session.id)
    finally:
        await runtime.stop()

asyncio.run(main())
```

```sh
python agent.py
```

Three things just happened:

1. `start_runtime` created `./agent-home`, the **runtime home**. Everything durable lives
   there as plain files.
2. `runtime.sessions.open()` created a **session**, a conversation with its own history,
   tools and permissions.
3. `run_turn` ran a **turn**: the model read your prompt, called tools such as `Glob` and
   `Read`, and answered. The result has `text`, `usage`, `model`, `toolCount`,
   `stopReason` and `estimatedCostUsd`.

`session_options` is a function because the runtime calls it every time a session is
opened or reopened, including when it wakes one by itself. It receives the session id and
the reason.

## 3. Watch it work

A turn produces a stream of events. Subscribe before you run it:

```python
def show(envelope):
    event = envelope["event"]
    if event == "tool.started":
        print("→", envelope["data"]["name"], envelope["data"]["input"])
    elif event == "message.assistant":
        print("assistant:", envelope["data"].get("text", "")[:200])

session.subscribe(show)
await session.run_turn("Find the largest file in this directory.")
```

Each envelope is a dictionary with an `event` name and a `data` payload. Text arrives as
the model writes it in `message.assistant.partial` events.
[Events](events.md) lists what you can listen for.

## 4. Decide what it may do

By default a session asks before it edits a file or runs a shell command that could change
something. In a script there is nobody to ask, so pick a policy:

```python
SessionOptions(provider="claude", permission_mode="acceptEdits")
```

| Mode | Behaviour |
|---|---|
| `default` | Ask before edits and before shell commands that are not clearly read-only |
| `acceptEdits` | Edit files without asking; still ask for other changes |
| `auto` / `dontAsk` | Never ask: allow what policy allows and refuse the rest |
| `bypassPermissions` | Allow everything inside the workspace |

To answer questions yourself, give the runtime a listener with an `ask_question` method.
[Permissions and security](security.md) covers rules, path confinement and hooks, and the
[embedding guide](embedding.md) shows the listener.

## 5. Give it a tool

A tool is a name, a description, a JSON schema and an async function:

```python
import json
from tess_core import ToolDefinition

async def lookup_order(arguments, context):
    order = {"order": arguments["order_id"], "status": "shipped"}   # your code here
    return {"content": json.dumps(order)}

orders = ToolDefinition(
    name="LookupOrder",
    description="Look up an order's status by its id.",
    inputSchema={
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    },
    handler=lookup_order,
    concurrency="parallel",          # safe to run alongside other tool calls
)

SessionOptions(provider="claude", host_tools=(orders,))
```

The model now sees `LookupOrder` beside the built-in tools. See [Tools](tools.md).

## 6. Come back later

A session is saved after every turn. Keep its id and reopen it in another process, another
day:

```python
session = await runtime.sessions.open("226348f1fe124b938dd1ffe2890cf4e4")
await session.run_turn("Carry on from where we stopped.")
```

The history, the model, the permission mode and the provider profile it was created with
all come back. [Sessions and storage](storage.md) describes what is on disk.

## Without writing code

The same runtime is available from the command line:

```sh
python -m tess_core run -p "Explain this repository in three sentences" \
    --provider anthropic --permission-mode default --output-format json
```

See [Command line](cli.md).

## Next

- [Concepts](concepts.md) for the model behind these calls
- [Embedding guide](embedding.md) to put it inside a service
- [Testing your agent](testing.md) to test all of this with no network
