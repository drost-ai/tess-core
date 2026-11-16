# Tess Core documentation

Tess Core is an agent runtime for Python: the part of an AI product that runs
conversations, executes tools, enforces permissions and keeps state. These pages explain
how to use it and how it works.

## Start here

| | |
|---|---|
| [Getting started](getting-started.md) | Install, run your first agent, stream events, resume a session |
| [Samples](samples.md) | Complete programs to run, read and copy, starting with a coding agent |
| [Concepts](concepts.md) | The vocabulary: runtime, session, turn, tool, authority |
| [Architecture](architecture.md) | How the subsystems fit together and why |

## Build with it

| | |
|---|---|
| [Embedding guide](embedding.md) | The host API: starting a runtime, configuring sessions, listening to events, answering questions |
| [Providers](providers.md) | Declaring models, credentials, local models, switching per session |
| [Tools](tools.md) | The 27 built-in tools and how to add your own |
| [Permissions and security](security.md) | Permission modes, rules, path confinement, hooks, credential handling |
| [Sub-agents](sub-agents.md) | Delegation, limits, messaging between agents |
| [MCP](mcp.md) | Connecting Model Context Protocol servers safely |
| [Scheduler](scheduler.md) | Durable jobs that fire as agent turns or host handlers |
| [Background tasks](background-tasks.md) | Long-running commands, monitors and health checks |
| [Context management](context.md) | Token accounting, compaction, prompt caching, artifacts |

## Operate it

| | |
|---|---|
| [Sessions and storage](storage.md) | What is written where, backup, and recovery |
| [Events](events.md) | The event stream and what each family means |
| [Configuration](configuration.md) | The settings folder, knobs and environment overrides |
| [Command line](cli.md) | `python -m tess_core run` for scripts and CI |
| [Testing your agent](testing.md) | Deterministic tests with a scripted model |

## Reference

| | |
|---|---|
| [API reference](reference/api.md) | Every public name, by entry point |
| [Settings reference](reference/settings.md) | Every configuration knob with its default and range |
| [Public API contract](public-api-contract.md) | What is stable, what is not, and how it is enforced |

## Design notes

Longer write-ups of specific mechanisms, for readers who want the reasoning:

- [MCP stdio containment](design/mcp-stdio-containment.md)
- [Shared MCP broker](design/shared-mcp-broker.md)
- [MCP in child runtimes](design/child-mcp-runtime.md)
- [Monitor delivery](design/resident-monitor-delivery.md)
