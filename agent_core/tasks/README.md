# Task Execution Package

This package owns long-running work:

- `supervisor.py`: daemon-facing task lifecycle, restart recovery, task records,
  output reads, stop, retry, and health checks.
- `runtime.py`: per-session task projection and task event emission.
- `subagents.py`: Agent tool child-session orchestration and background
  subagent finalization helpers.
- `runners/`: subprocess entrypoints for detached main-session and background
  subagent workers.

Top-level modules such as `agent_core.task_supervisor` remain compatibility
shims. New code should import from `agent_core.tasks.*` when it is not relying
on the legacy public module path.
