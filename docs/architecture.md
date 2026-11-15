# Architecture

This page is for people who want to know how Tess Core is built: to evaluate it, to debug
it, or to contribute. You do not need it to use the runtime.

## Design principles

1. **A library, not a server.** The runtime runs inside your process. There is nothing to
   deploy beside your application.
2. **No dependencies.** The core uses the Python standard library only: HTTP, streaming,
   JSON-RPC, YAML parsing, scheduling and storage are implemented here. What you audit is
   what runs.
3. **The file system is the database.** State is JSON files in one directory, written
   atomically and validated on read.
4. **Every wait has a bound.** Model calls, tool calls, child processes, shutdown: each
   has a timeout and a defined result when it expires.
5. **Fail closed, and say so.** A missing credential, a malformed setting, a permission
   that cannot be decided: each stops with a specific error instead of a guess.
6. **Authority is data, not conversation.** What an agent may do comes from records the
   runtime holds, never from what the model or a tool result says.

## Processes

```
┌──────────────────────────── your process ─────────────────────────────┐
│  your application                                                      │
│  ┌──────────────────────────── Runtime ──────────────────────────────┐ │
│  │  SessionManager ── Session ── agent loop ── provider client       │ │
│  │                          └── tools ── permission engine           │ │
│  │  Scheduler      MCP clients      event stream      storage        │ │
│  └───────────────────────────────┬───────────────────────────────────┘ │
└──────────────────────────────────┼─────────────────────────────────────┘
                                   │ local socket, authenticated
                     ┌─────────────┴─────────────┐
                     │        task daemon        │   one per runtime
                     └──┬──────────┬──────────┬──┘
                        │          │          │
                  sub-agent   background   health
                   worker      command     check
                        
        MCP stdio servers run as children of your process,
        each under a guardian that stops it if its owner dies.
```

- **Your process** hosts the runtime, the sessions and their turns. Turns are asyncio
  tasks; blocking file and network work runs in bounded thread pools.
- **The task daemon** owns anything that must outlive a turn: sub-agents, background
  commands, health checks. It is started with the runtime, authenticated with a secret
  only the runtime holds, and stops itself if the runtime disappears.
- **Sub-agent workers** are separate Python processes, each with its own session.
- **MCP servers** are child processes or remote endpoints.

## A turn

1. `session.run_turn(prompt)` takes the session's turn lock. One turn runs at a time per
   session; input that arrives meanwhile is queued.
2. The loop builds a request: system prompt, tools, conversation, per-turn context.
3. The request is measured. If it will not fit, the conversation is compacted first.
4. The provider client sends it and streams the response. Transient failures are retried
   with backoff; the fallback model takes over if the profile names one.
5. Tool calls in the response go to the dispatcher. Each call is checked by the permission
   engine, then run. Calls marked parallel run together; the rest run one at a time.
6. Results are budgeted, added to the conversation, and the loop returns to step 2.
7. When the model stops calling tools, the turn completes and the session is saved.

The loop is guarded throughout: by the turn's limits, the progress guard, and
cancellation, which is honoured at every await.

## Packages

| Package | Responsibility |
|---|---|
| `runtime` | `start_runtime`, the `Runtime`, the session manager, session lifecycle, events, credentials |
| `agent` | The agent loop: provider turns, tool dispatch, progress guard, cancellation, hooks |
| `providers` | Clients for the Anthropic and OpenAI protocols, streaming, retries, usage |
| `tools` | The built-in tools, the tool registry, the permission engine |
| `authority` | Path confinement, command inspection, the rules for what a child may do |
| `sandbox` | Operating-system sandbox backends: Seatbelt on macOS, Bubblewrap on Linux |
| `context` | Token accounting, compaction, checkpoint verification |
| `payloads` | Tool result budgets, artifacts, images |
| `mcp` | The MCP client: stdio, HTTP and SSE transports, OAuth, the shared broker |
| `daemon` | The task daemon and its protocol |
| `tasks` | Background tasks, monitors, health checks, monitor delivery |
| `subagents` | Launching, messaging and reconciling sub-agents |
| `scheduler` | Durable jobs, time-zone rules, the timer |
| `session` | Session records, transcripts, forking |
| `storage` | Paths, atomic writes, locks, record validation, the settings folder |
| `config` | Settings, the setting registry, the YAML subset, the provider registry, prompts |
| `process` | Child process control: groups, signals, bounded shutdown |
| `headless` | The `run` command |
| `remote` | Remote session bridges |
| `contracts` | Typed shapes shared between packages |

## The public surface

Applications import from four entry points: `tess_core`, `tess_core.shell_surface`,
`tess_core.platform_support` and `tess_core.testing`. Everything else is internal. A test
compares the exported names with a recorded contract, so the surface cannot change by
accident. See the [API reference](reference/api.md) and the
[contract](public-api-contract.md).

## Why zero dependencies

A dependency is code you run with your credentials and your users' data, chosen by someone
else and updated on their schedule. An agent runtime sits at the most sensitive point of
an application: it holds the model keys and executes the model's tool calls. Keeping that
layer free of third-party code makes it possible to review all of it, removes a class of
supply-chain risk, and means installing it can never conflict with what you already use.

Optional extras exist for operating-system credential stores and encrypted credential
files. They are not installed unless you ask.

## Reliability mechanisms

- **Atomic writes and file locks** for every durable record.
- **Ownership leases.** A runtime home has one owner; a daemon has one runtime.
- **Reconciliation at start.** What a crashed process left behind is settled once: lost
  tasks are marked lost, interrupted turns are recorded as interrupted.
- **Exactly-once delivery** of sub-agent results and monitor notifications, through a
  claim, checkpoint and publish sequence that survives a restart at any step.
- **Process containment.** Children run in their own process groups and are stopped,
  then killed, within a bound.

The [design notes](design/) go deeper on four of these: MCP process containment, the
shared MCP broker, sub-agent MCP connections, and monitor delivery.

## Lineage

Tess Core began as a Python port of a TypeScript agent runtime and has since been
developed independently. Some internal names still follow the original's conventions
(camelCase keys in stored records, for example); the stored formats are stable, and the
public Python surface is being moved to idiomatic names.
