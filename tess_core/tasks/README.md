# Task Execution Package

This package owns long-running work:

- `supervisor.py`: daemon-facing task lifecycle, restart recovery, task records,
  output reads, stop, retry, and health checks.
- `runtime.py`: per-session task projection and task event emission.
- `subagents.py`: Agent tool child-session orchestration and background
  subagent finalization helpers.
- `runners/`: subprocess entrypoints for detached main-session and background
  subagent workers.

Import from `tess_core.tasks.*`; the legacy flat module paths were retired
in the v1 refactor.
