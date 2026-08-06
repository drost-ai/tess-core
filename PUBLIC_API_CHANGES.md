# Public API Changes -- v1 Refactor

The v1 refactor consolidated 131 top-level modules in `agent_core/` into
subsystem packages. This document records the one intentional break, as the
project's refactor governance requires a written decision before public
imports change.

## What changed

`agent_core/__init__.py` builds its public surface by star-importing its
submodules. A side effect of that layout is that every star-imported module's
*own name* became an attribute of `agent_core`, and therefore part of the
frozen export snapshot -- `agent_core.subagent_groups`, `agent_core.runtime_paths`,
and so on. These were never designed as API; they were an artefact of the
import style. The same mechanism also published stdlib module names such as
`json`, `copy`, and `os`, which remain in place and are untouched here.

Those module objects have moved into subsystem packages. Paths with a known
consumer are preserved exactly, via a `sys.modules` alias that registers the
*same module object* -- so `import`, `from`-import, `dir()`, and monkeypatching
all behave as they did. Paths with no observed consumer were retired.

## Retired module paths (54)

`agent_core.<name>` no longer resolves for the following. Each one's code still
exists, unchanged, at the new path shown in "Where things moved" below.

- `agent_authority`
- `agent_runtime_bindings`
- `collaboration_updates`
- `debug_bundle`
- `executable_closure`
- `executable_config`
- `executable_source_policy`
- `execution_descriptors`
- `fetch_proxy`
- `hooks`
- `lifecycle_barrier`
- `mcp_origin_policy`
- `monitor_bootstrap`
- `owned_process_launch`
- `permission_rules`
- `persisted_record_scanner`
- `persisted_record_validation`
- `plugin_contracts`
- `plugins`
- `provider_errors`
- `provider_timeouts`
- `provider_token_budget`
- `remote`
- `repository_trust`
- `runtime_bootstrap`
- `runtime_events`
- `runtime_generation`
- `runtime_identifiers`
- `runtime_paths`
- `runtime_secret_provider`
- `runtime_secrets`
- `runtime_session_components`
- `runtime_state`
- `runtime_support`
- `session_fork`
- `session_message_history`
- `session_runtime`
- `shell_command_policy`
- `subagent_capacity`
- `subagent_continuation`
- `subagent_continuation_recovery`
- `subagent_contract`
- `subagent_depth_policy`
- `subagent_groups`
- `subagent_limits`
- `subagent_reconciliation`
- `subagent_reconciliation_renderer`
- `subagent_reconciliation_store`
- `subagent_result`
- `task_communication`
- `task_ownership`
- `tool_payloads`
- `transcript`
- `workspace`

## Preserved compatibility paths (5)

These keep working unchanged, because a consumer in this repository still
imports or patches them by their historical path:

- `agent_core.monitors` -> `agent_core.tasks.monitors`
- `agent_core.process_identity` -> `agent_core.process.identity`
- `agent_core.provider_watchdog` -> `agent_core.providers.watchdog`
- `agent_core.subagent_coordination` -> `agent_core.subagents.coordination`
- `agent_core.task_process_launch` -> `agent_core.process.task_launch`

## Where things moved

| Subsystem | Contents |
| --- | --- |
| `agent_core/acquisition/` | outbound fetch and search acquisition |
| `agent_core/agent/` | the agent loop (was `agent.py` + `agent_loop/`) |
| `agent_core/authority/` | trust, permissions, hooks, executable policy, plugins |
| `agent_core/collaboration/` | collaboration, remote sessions, bridges |
| `agent_core/config/` | config resolution (was `config.py` + `config_store/`) |
| `agent_core/context/` | compaction, continuation, token accounting, memory |
| `agent_core/daemon/` | daemon server, client, protocol (was `daemon_runtime/`) |
| `agent_core/mcp/` | MCP runtime and transports (was `mcp_runtime/`) |
| `agent_core/payloads/` | model-visible payload shaping and bounds |
| `agent_core/process/` | process identity, launchers, background runners |
| `agent_core/providers/` | provider adapters plus the provider boundary |
| `agent_core/runtime/` | bootstrap, events, state, paths, identity, secrets |
| `agent_core/session/` | session persistence, forking, transcript, history |
| `agent_core/subagents/` | subagent contracts, limits, reconciliation |
| `agent_core/tasks/` | supervisor, monitors, task runtime |
| `agent_core/tools/` | tool handlers and registry |

`platform_support/` deliberately keeps its name: `agent_core.platform` would be
shadowed by the stdlib `platform` module, which `tools/__init__` star-exports.

## Migration

Replace a retired path with its new location, or import the name from the
top-level package, which still re-exports everything it did before:

```python
from agent_core import persistSession          # unchanged
from agent_core.session.persistence import persistSession   # explicit
```

The top-level `agent_core` *name* surface is otherwise unchanged: this refactor
removed 54 module-name attributes and added none.
