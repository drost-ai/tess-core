<div align="center">

<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/tess-core-dark.svg">
    <img src="docs/assets/tess-core-light.svg" alt="Tess Core" height="64">
  </picture>
</h1>

**The agent runtime for Python. Zero dependencies. Any model.**

A complete operating system for AI agents (sessions, tools, permissions, sub-agents,
MCP, scheduling and durable state) in one pure-Python package you can read, audit and embed.

[Quickstart](#quickstart) · [Why Tess Core](#why-tess-core) · [What's inside](#whats-inside) · [Built for production](#built-for-production) · [Documentation](https://continuityresearch.ai/tess-core/docs) · [Samples](https://github.com/drost-ai/tess-core-samples)

No transitive dependencies · Python 3.12+ · Apache-2.0

</div>

---

```python
import asyncio
from tess_core import SessionOptions, start_runtime

async def main():
    runtime = await start_runtime(
        "~/.my-agent",                              # durable state lives here, as plain files
        provider_registry="providers.yaml",         # your models, declared once
        session_options=lambda session_id, reason: SessionOptions(provider="claude"),
    )
    session = await runtime.sessions.open()
    result = await session.run_turn("Find every TODO in this repo and group them by owner.")
    print(result["text"])
    await runtime.wait_until_idle()   # let any sub-agents finish and report
    await runtime.stop()

asyncio.run(main())
```

That is a full agent. It reads and edits files, runs shell commands under a permission
policy, searches the web, delegates to sub-agents, calls your MCP servers, compacts its own
context when the conversation gets long, and can be stopped and resumed tomorrow from the
same session.

## Why Tess Core

Agents have outgrown the request/response loop. A useful agent holds a conversation for
weeks, runs work in the background, wakes itself on a schedule, asks for permission before
it touches something that matters, and survives a restart without losing its place. That is
not a prompt wrapper. It is a runtime.

The serious open-source agent runtimes are written in TypeScript. Python, the language most
AI teams actually build in, has been left with a different kind of tool: frameworks that
give you abstractions to assemble an agent from, and a dependency tree to go with them.

Tess Core is the runtime, in Python:

- **Zero dependencies.** `dependencies = []`. The standard library, 86,000 lines you can
  read, and nothing else in your supply chain. No version conflicts with your application.
- **Any model, one interface.** Anthropic, OpenAI (Responses and Chat Completions), any
  OpenAI-compatible endpoint, a model on your own hardware. Choose per session.
- **Complete, not assembled from parts.** The agent loop, 27 built-in tools, permissions,
  sub-agents, MCP, scheduling, background tasks and persistence are all here and already
  work together.
- **Embeddable.** It runs inside your process. Your application stays in charge: you decide
  how each session is configured, and you receive every event.

## Quickstart

**1. Create an environment and install.** With [uv](https://docs.astral.sh/uv/):

```sh
mkdir my-agent && cd my-agent
uv venv --python 3.12
uv pip install "tess-core @ git+https://github.com/drost-ai/tess-core.git"
```

**2. Put your credential in `.env`.** Your Codex sign-in file, or an API key for the
provider you use. You only need the one your profile names:

```sh
OPENAI_API_KEY=...
CODEX_AUTH_PATH=~/.codex/auth.json
```

**3. Declare your models in `providers.yaml`.** Credentials are named here, never written
down. Declare as many profiles as you like; only the one a session uses needs its key:

```yaml
schema_version: 1
providerProfiles:
  codex:
    kind: openai
    apiMode: codex
    defaultModel: gpt-6.1-sol
    reasoningEffort: xhigh
    auth:
      kind: openai_api_key_or_codex

  gpt:
    kind: openai
    apiMode: responses
    defaultModel: your-openai-model
    auth:
      kind: openai_api_key
      envVar: OPENAI_API_KEY

  claude:
    kind: anthropic
    defaultModel: claude-sonnet-5-5
    auth:
      kind: anthropic_api_key
      envVar: ANTHROPIC_API_KEY

  local:                                   # llama.cpp, vLLM, Ollama: anything OpenAI-compatible
    kind: openai
    apiMode: chat_completions
    baseUrl: http://127.0.0.1:8080/v1
    defaultModel: your-local-model
    auth:
      kind: none
```

Working profiles for a Codex sign-in, Kimi and Z.ai are in the
[provider samples](docs/providers.md#provider-samples).

**4. Write `main.py`:**

```python
import asyncio
import os

from tess_core import SessionOptions, start_runtime


async def main():
    runtime = await start_runtime(
        "agent-home",                        # durable state lives here, as plain files
        provider_registry="providers.yaml",  # your models, declared once
        session_options=lambda session_id, reason: SessionOptions(
            provider="codex",
            provider_auth_path=os.environ["CODEX_AUTH_PATH"],
            permission_mode="bypassPermissions",
        ),
    )
    session = await runtime.sessions.open()
    result = await session.run_turn("Read the codebase here and orient yourself. What are we building here?")
    print(result["text"])
    await runtime.wait_until_idle()   # let any sub-agents finish and report
    await runtime.stop()


asyncio.run(main())
```

**5. Run it:**

```sh
uv run --env-file .env main.py
```

### Using an API key instead?

Choose a profile that names a key, and drop the sign-in file:

```python
        session_options=lambda session_id, reason: SessionOptions(
            provider="gpt",
            permission_mode="bypassPermissions",
        ),
```

### Or skip the code

```sh
uv run --env-file .env python -m tess_core run \
    -p "Read the codebase here and orient yourself. What are we building here?" \
    --provider-registry providers.yaml --provider codex \
    --provider-auth-path ~/.codex/auth.json \
    --permission-mode bypassPermissions --output-format stream-json
```

The [getting started guide](docs/getting-started.md) takes it from here: streaming events,
permissions, your own tools, and resuming a session.

Want something bigger than a script?
[coding-agent-minimal](https://github.com/drost-ai/tess-core-samples/tree/main/coding-agent-minimal)
is a terminal coding agent in one file.

## What's inside

| | |
|---|---|
| **Sessions that last** | Open, close and reopen conversations by id. State is written after every turn and survives crashes and restarts. Idle sessions unload themselves and wake when something needs them. |
| **The agent loop** | Streaming turns, parallel tool calls, cancellation, per-turn limits on model calls, tool calls and time, and a progress guard that stops an agent going in circles. |
| **27 built-in tools** | Files (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `NotebookEdit`), `Bash`, `WebFetch`, `WebSearch`, sub-agents (`Agent`, `SendMessage`), background tasks, monitors and health checks, git worktrees, MCP, to-dos and user questions. |
| **Your tools** | Hand the runtime a `ToolDefinition` and the model can call your code, under the same permission rules as everything else. |
| **Permissions** | Five modes from ask-for-everything to fully autonomous, allow and deny rules, path confinement to the workspace, a shell command policy, and approval prompts your application answers. |
| **Sub-agents** | Agents that start agents. Children work in the background with depth and concurrency limits and their own permissions, can message their parent, and deliver their results back even if the parent has gone idle. |
| **MCP, contained** | stdio, HTTP and SSE servers with OAuth, bounded startup and frames, isolated child environments, and guaranteed cleanup of server processes. |
| **Context that manages itself** | Token accounting before every request, automatic checkpoint compaction with verification, prompt caching, and oversized tool results moved out of the conversation into retrievable artifacts. |
| **A scheduler** | Durable jobs (once, calendar, interval, in any time zone) that fire as agent turns or as your own handlers. The agent can schedule its own follow-ups. |
| **Background work** | A supervisor daemon runs long shell tasks, watches them, and reports back; work continues while the conversation moves on. |
| **Multi-provider** | Anthropic Messages, OpenAI Responses, OpenAI-compatible Chat Completions, sign-in with a ChatGPT account. Streaming, images, reasoning effort, usage and cost accounting, and recovery from interrupted streams. |
| **Observable** | Every turn, tool call, permission decision and provider request is an event your host receives. Nothing happens off the record. |

## Built for production

Tess Core runs a personal AI that stays up for months. The same properties are what an
engineering organisation asks of infrastructure:

- **An auditable supply chain.** No third-party runtime dependencies. What you review is
  what runs. Optional extras (OS keychain, encrypted credential file) are opt-in and named.
- **Credentials stay out of configuration.** Provider profiles name an environment variable
  or a managed credential reference. Literal keys in a registry file are rejected, and
  credential errors are redacted before they reach a log.
- **Explicit over ambient.** With a provider registry, nothing is discovered: no implicit
  auth files, no provider guessed from a model name, no fallback you did not configure.
- **Least privilege by default.** Tools ask before acting unless you say otherwise.
  File tools are confined to the workspace. Sub-agents cannot exceed their parent's
  authority. MCP servers can be started with a clean environment.
- **Durable, inspectable state.** Sessions, tasks, schedules and monitors are plain JSON
  files under one directory you choose. No database to operate, straightforward to back up.
- **One process, no fleet.** No broker, queue or external service is required. It scales
  the way your application does.
- **Bounded everything.** Startup, requests, tool output, frames, retries and cleanup all
  have limits. 160 documented settings tune them, each with an environment override.
- **Tested like infrastructure.** More than 1,400 tests cover crash recovery, concurrent
  writers, process cleanup and provider failure modes, all runnable offline against
  scripted models.
- **A stable contract.** The names you may import are listed in a
  [public API contract](docs/reference/api.md) enforced by the test suite. Everything else
  is internal and free to improve.
- **Apache-2.0.** Use it commercially, modify it, ship it.

## How it compares

| | Tess Core | TypeScript agent runtimes | Python agent frameworks |
|---|---|---|---|
| Language | Python | TypeScript | Python |
| Runtime dependencies | None | A Node toolchain and its packages | Typically dozens, transitively many more |
| What you get | A running agent system | A running agent system | Building blocks to assemble one |
| Embeds in a Python service | Yes, in-process | As a separate process | Yes |
| Durable sessions, sub-agents, scheduling, permissions | Built in | Varies | You build or integrate them |
| Model providers | Any, per session | Varies | Any, through adapters |

If your team writes Python and wants an agent that works on day one, without adopting a
framework's worldview or a second language's toolchain, this is what Tess Core is for.

## Architecture

```
                 your application  (a web service, a CLI, a bot, a worker)
                        │   start_runtime()        ▲  events, permission requests
                        ▼                          │
┌──────────────────────────────────────────────────────────────────────────┐
│  Runtime            one per process and home directory                    │
│  ├─ Session manager   open · idle close · reopen · end                    │
│  │    └─ Session        turn queue ─▶ agent loop ─▶ tools ─▶ provider     │
│  ├─ Scheduler         durable jobs, time zones, agent or host handlers    │
│  ├─ Task daemon       background commands, monitors, health checks        │
│  └─ MCP               stdio · HTTP · SSE, contained child processes       │
├──────────────────────────────────────────────────────────────────────────┤
│  Authority    permission modes · rules · path confinement · hooks         │
│  Context      token accounting · compaction · prompt cache · artifacts    │
│  Providers    Anthropic · OpenAI Responses · Chat Completions · local     │
│  Storage      plain files under the runtime home, written atomically      │
└──────────────────────────────────────────────────────────────────────────┘
```

Read [the architecture guide](docs/architecture.md) for how the pieces fit.

## Documentation

The full documentation, with search, is at **[continuityresearch.ai/tess-core/docs](https://continuityresearch.ai/tess-core/docs)**.
The same pages live in this repository:

- [Getting started](docs/getting-started.md): install, first agent, streaming, resuming
- [Concepts](docs/concepts.md): runtimes, sessions, turns, tools, authority
- [Embedding guide](docs/embedding.md): the host API, listeners, your own tools
- [Providers](docs/providers.md) · [Tools](docs/tools.md) · [Permissions and security](docs/security.md)
- [Sub-agents](docs/sub-agents.md) · [MCP](docs/mcp.md) · [Scheduler](docs/scheduler.md) · [Background tasks](docs/background-tasks.md)
- [Context management](docs/context.md) · [Sessions and storage](docs/storage.md) · [Events](docs/events.md)
- [Configuration](docs/configuration.md) · [Command line](docs/cli.md) · [Testing your agent](docs/testing.md)
- [API reference](docs/reference/api.md)

## Status

Tess Core is at version 0.1. It is in daily use as the engine of a personal AI that runs for months at a time, and its public
API is small and contract-tested, but it has not yet made a 1.0 stability promise: expect
additions, and read the release notes before upgrading. It is developed and tested on macOS
and Linux.

It began as a Python port of a TypeScript runtime and has since grown its own session
manager, scheduler and host API.

## Contributing

Issues and pull requests are welcome. Run the suite before you send one:

```sh
uv venv && uv pip install -e ".[dev,encrypted-credentials,chatgpt-account]" pyyaml
.venv/bin/python -m pytest -q -n auto
```

The tests need no network and no API keys.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Any third-party material
retains its own license and notices.

Created by Migel Tissera at [Continuity Research](https://continuityresearch.ai).
