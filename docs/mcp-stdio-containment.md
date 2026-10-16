# MCP stdio process containment

## Why a guardian is needed

The supervisor already performs identity-verified process-group termination of
owned workers. Its session descendant traversal covers registered child agents
and tasks, not arbitrary OS subprocesses. Stdio MCP uses a new session/process
group, so killing a worker's group alone cannot reach its MCP servers.

On POSIX, `StdioMcpClient` therefore launches `stdio_guardian.py` as the dedicated
session/group leader. The guardian launches the configured server in that same
group with stdin/stdout/stderr inherited unchanged. No JSON-RPC proxy is involved.

## Ownership and termination protocol

- The client owns a non-inheritable lifetime-pipe write descriptor. Only the
  guardian inherits the read descriptor via `pass_fds`. The actual server gets
  neither descriptor (`close_fds=True`). Other exec'ed workers cannot retain the
  write end accidentally.
- Before launching the server and every 50ms while it runs, the guardian checks
  this pipe. EOF means the owner closed the transport or died, including SIGKILL.
  This does not depend on looking up a possibly reused parent PID.
- Owner loss, server exit, or TERM/INT/HUP starts group cleanup: SIGTERM, the
  configured `mcp.close_grace_ms` grace, then SIGKILL to the entire group including
  the guardian. Cleanup never waits indefinitely for server cooperation.
- The guardian retains group leadership until escalation. Even if the server
  exits early, the group ID remains anchored to the guardian's live PID.
- A separate one-shot status pipe reports the server's actual exit code promptly.
  This preserves exit diagnostics and fails pending RPCs without waiting through
  the cleanup grace. The existing wait thread consumes it; no extra client
  thread is required. Both pipes are released on normal close and spawn failure.
- Ordinary POSIX transport close closes the lifetime pipe and waits/reaps the
  guardian within a bounded budget. **Only the guardian signals its group**;
  the client never probes or signals a numeric PGID that another thread may
  already have reaped. A missed deadline reports a cleanup error, not an unsafe
  PID-based fallback. Other clients' guardians have different groups and owner
  pipes; closing a child client does not close the parent's MCP servers.

The overhead is **one small Python guardian process and two client pipe
descriptors per POSIX stdio server**. The guardian uses only the standard
library, runs through a file path with isolated Python mode, and does not import
the agent runtime. Normal server environment, working directory, argv and stdio
are preserved. Windows retains its pre-existing direct subprocess behavior; this
change does not establish Windows abrupt-owner-death containment.

## Scope and limits

This is lifecycle containment, **not a security sandbox**. The guarantee covers
the MCP server and descendants remaining in its owned group. A server that
intentionally calls `setsid()`/changes groups can escape. Killing the guardian
itself outside the group cleanup protocol can also defeat owner monitoring.
A raw `fork()` without exec in the client can retain descriptors; the runtime's
subprocess launches close descriptors or exec with CLOEXEC.

Scheduling and OS reaping add latency beyond the poll/grace intervals. Orphaned
zombies may await the OS reaper; they are not executing servers. No unbounded
application-level wait is used in guardian cleanup.

## Verification

`tests/test_mcp_stdio_containment.py` launches only disposable processes. It
initializes a real child stdio MCP server and an unrelated parent-owned server,
then kills the disposable worker group with both SIGTERM and SIGKILL. The child
server ignores TERM and stdin EOF and owns a grandchild. The test verifies the
isolated guardian/server/grandchild stop within the deadline while the parent's
server remains alive and answers another RPC. Finally blocks stop only the
recorded test groups. It also tests spawn-failure descriptor cleanup.

Existing transport tests verify stubborn-descendant cleanup, request deadlines,
bounded stderr, and preservation of real server exit codes (including 23/24).

Validation after implementation:

```text
python -m pytest tests/*mcp*.py -q --tb=short
101 passed, 40 subtests passed in 70.22s
```

No live runtime restart, live server mutation, or git operation was used.
