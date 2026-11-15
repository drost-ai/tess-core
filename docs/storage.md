# Storage

Tess Core keeps everything in one directory, the **runtime home**. There is no database to
run and no service to connect to. Back up the directory and you have backed up the agent.

## Choosing the runtime home

```python
runtime = await start_runtime("/var/lib/my-agent", ...)
```

The command line uses `--runtime-home`, then the `TESS_CORE_HOME` environment variable,
then `~/.tess-core`.

One process owns a runtime home at a time. A second process that tries to start on the
same home is refused, so two processes can never write the same session. A process hosts
one runtime with background work enabled.

## Layout

```
<runtime-home>/
  settings/              configuration you write (see Configuration)
  sessions/              one conversation per pair of files
    <id>.json              the session head: model, mode, metadata, counters
    <id>.messages.jsonl    the conversation, one message per line
  compactions/           checkpoints written when a conversation is compacted
  tool-artifacts/        large tool results, saved in full
  tasks/                 background tasks and their output
  task-records/          the durable record of each task
  monitors/              monitors and their delivery state
  scheduler/             scheduled jobs
  agents/                sub-agent records
  mcp-tokens/            OAuth tokens for remote MCP servers
  mcp-blobs/             large MCP payloads
  private-secrets/       managed credentials
  trust/                 approvals you have granted to workspaces
  daemon/                the task daemon's socket, state and logs
  locks/                 ownership and file locks
  logs/                  runtime logs
```

Everything is JSON or JSON Lines. You can read a session with `jq`.

## Sessions on disk

A session is split in two so that long conversations stay cheap to save:

- The **head** is small and rewritten when something about the session changes.
- The **body** is append-only. Adding a message appends a line; it does not rewrite the
  conversation.

Writes are atomic: a file is written beside its destination and renamed into place, so a
crash leaves either the old version or the new one, never half of each. Records are
validated when read, and a damaged record is reported instead of being silently repaired
or dropped.

## What survives a restart

| Survives | Does not survive |
|---|---|
| Sessions and their full history | A turn that was in flight (it is recorded as interrupted) |
| Scheduled jobs | Background commands (the daemon stops with the host) |
| Monitors and undelivered results | Open MCP connections (they reconnect) |
| Compaction checkpoints | |
| Tool artifacts | |

After a restart, open a session by id and carry on. See
[Getting started](getting-started.md#6-come-back-later).

## Ending and deleting

- `session.close()` releases the session's resources. The conversation stays on disk and
  can be reopened.
- `session.end()` ends it for good: its sub-agents, tasks and monitors are stopped, and it
  will not be reopened.

Tess Core does not delete conversations by itself. Retention is your decision: remove a
session's two files, and its directories under `compactions/` and `tool-artifacts/`, when
your policy says so.

## Sensitive data

The runtime home holds conversation content and, if you use managed credentials, secrets.

- Directories that hold secrets are created with owner-only permissions.
- Provider keys given through environment variables are never written to disk.
- Images in conversations are stored once and referenced, and are redacted from event
  streams.

Treat the runtime home like a database directory: restrict it to the service account, put
it on encrypted storage, and include it in your backup and retention policies. The
[security guide](security.md) has the full checklist.

## Several agents on one machine

Give each its own runtime home. Homes are independent: separate sessions, settings,
credentials and daemon.

## Per-project state

When a session works in a directory, a small amount of state specific to that project can
live in `.tess/` inside it: project settings and locally granted approvals. Add
`.tess/local/` to your `.gitignore`.
