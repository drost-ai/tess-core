# Canonical Key Registry

The single reference for every boundary field that has (or had) more than one
spelling, the canonical spelling, and where the legacy alias is absorbed.
Package 22 (typed contract boundaries) froze these shapes; **removing legacy
key emission is explicitly out of scope** — it is a behavior change that needs
its own parity-checked package, because consumers (including the TypeScript
side) may read the legacy spelling.

## Routed through canonical accessors

These fields had inline `or`-chain coalescing reads; all readers now go
through one accessor, so a future de-duplication has a single switch point.

| Logical field | Canonical | Legacy alias(es) | Accessor | Producers still emitting the alias |
|---|---|---|---|---|
| Task launch result id | `taskId` | `task_id` | `contracts.records.launch_task_id` | every supervisor launcher emits both (tasks/supervisor.py) |
| Task launch result output file | `outputFile` | `output_file` | `contracts.records.launch_output_file` | every non-health-check launcher emits both |
| Task meta id | `taskId` | `task_id`, `id` | `contracts.records.task_meta_id` | launchers emit `taskId`+`id`; the subagent finalizer emits only `task_id` (tasks/subagent_parts/finalizers.py) |
| Event kind (typed view) | `kind` | `type`, `event` | `contracts.events.event_kind` | createRuntimeEvent emits all three |
| Event name (raw view) | `event` | `type`, `kind`, `legacyEvent` | `contracts.events.event_name` | createRuntimeEvent emits all |
| Event body (typed view) | `payload` | `data`, `legacyData` | `contracts.events.event_payload` | createRuntimeEvent emits all |
| Event body (raw view) | `data` | `payload` | `contracts.events.event_body` | createRuntimeEvent emits both |
| Event timestamp | `createdAt` | `timestamp` | `contracts.events.event_time` | createRuntimeEvent emits both |
| Event session id | `sessionId` | body `session_id`/`sessionId` | `contracts.events.event_session_id` | createRuntimeEvent hoists from the body |

## Documented, deliberately not routed

These dual-spelling reads are TypeScript-parity surface and stay as they are;
they are listed so they remain a known, shrinking set rather than a habit.

| Location | Pattern | Why it stays |
|---|---|---|
| `tasks/supervisor.py` launcher args (`sessionId`/`session_id`, `agentId`/`agent_id`, `parentSessionId`/`parent_session_id`, `parentTaskId`/`parent_task_id`, `intervalMs`/`interval_ms`, …) | input alias acceptance | public input contract: callers may pass either spelling; not record drift |
| `runtime_event_parts/shared.py` `_field(data, "snake", "camel")` helpers | typed-payload projection | this layer *is* the absorption point that builds the camelCase typed payload from either spelling (TypeScript-shaped) |
| `runtime_state_parts/reducer.py` session.activated handler (`event.get("sessionId") or summary id or _field(data, …)`) | payload-specific fallback chain | precedence crosses three sources (envelope, summary, body); not a pure alias read |
| `tasks/subagent_parts/foreground.py` result `toolCount`/`tool_count`, `stopReason`/`stop_reason` | foreign-result tolerance | accepts results from both the embedded runtime and runtime callbacks |
| `daemon_runtime/client.py`, `tasks/supervisor_parts/projection.py` (`sessionId`/`session_id` on generic event values) | wire tolerance | daemon socket payloads are produced by both runtimes |

## Single-spelling boundary fields

Everything else in `contracts/` has exactly one spelling on the wire; the
TypedDicts in this package are the authoritative list. New fields must be
authored camelCase (matching the TypeScript contract) and added to the
relevant TypedDict in the same change.

## Type-check lane scope

`python3 -m mypy --config-file pyproject.toml` is **blocking** over
`agent_core/contracts/**` (enforced by `tests/test_contracts_typecheck.py`).
Runtime modules that import the contracts stay outside the strict gate by
design — the port's internals are deliberately TypeScript-shaped
`dict[str, Any]` code, and converting them is a non-goal of package 22.
