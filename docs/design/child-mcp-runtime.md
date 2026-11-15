# Child MCP runtime

Background one-shot and resident children compose their tools through the same
session-component pipeline as normal sessions. This restores the MCP descriptors
and source policy pinned in the child's effective authority, loads the selected
plugins, connects eligible MCP servers, and filters the resulting tool registry
against the child's pinned `enabledTools`. Catalog refresh uses that same filter:
a server advertising a new tool does not grant the child new authority.

## Delegation policy

Hosts must mark session-bound or otherwise nondelegable MCP descriptors **before
capturing authority and launching children**:

```python
{
    "name": "host-session-service",
    "transport": "stdio",
    "commandOrUrl": "host-service",
    "args": ["--session", parent_session_id],
    "delegation": {"mode": "deny", "scope": "session"},
}
```

The optional `delegation` object accepts only:

- `mode`: required when the object is present; `allow` or `deny`.
- `scope`: optional; `shared` (default) or `session`.

Unknown fields or invalid values are rejected by descriptor normalization. The
metadata remains in the pinned descriptor and is covered by the authority
fingerprint. Changing it after capture invalidates that fingerprint.

Child bootstrap excludes any server with `mode: deny` **or** `scope: session`.
An `allow` policy does not override session scope. This version does not rebind a
parent's session-specific argv, credentials, or role for a child. Exclusion is
generic and independent of server names. The policy affects children only;
normal parent sessions can still connect to their own nondelegable servers.

Descriptors without a delegation object remain eligible under a newly captured
authority, subject to normal source selection and tool authority. Hosts are
responsible for identifying their session-bound descriptors; the engine does not
infer session scope from command-line arguments.

## Legacy launches and rollout

New authority captures include `executableSources.mcpDelegationVersion: 1` before
fingerprinting. A child whose pinned authority lacks this version (or has an
unsupported version) excludes **all** inherited MCP servers rather than silently
activating a potentially parent-bound service. Existing pins are not upgraded in
place. Start a fresh child from a freshly captured parent authority after the
host has supplied the appropriate descriptor metadata.

Deploy the host's session-bound descriptor annotations together with the engine
change before permitting fresh launches. Merely updating the engine does not
identify unannotated host-specific services.

Exclusions produce a `mcp.delegation_excluded` child trace with the server name
and a reason: `legacy_delegation_absent`, `nondelegable_server`, or
`invalid_delegation_policy`. An excluded server was not launched; it is not a
connection failure.

## Resource lifetime

A one-shot worker owns composed resources until its prompt completes. A resident
worker owns them across its initial turn, mailbox waits, and continuation turns;
it does not reconnect servers for every turn. Both paths close their resources
in an outer `finally` block. Normal return, errors, and cooperative cancellation
therefore release owned MCP resources. Bootstrap also closes composed resources
if runtime-binding construction fails. The component closer is idempotent.

The disposable integration tests in `tests/test_subagent_mcp.py` exercise real
stdio discovery and calls, notification-driven catalog refresh, authority
refiltering, nondelegable process exclusion, process reaping on normal close,
persisted worker bootstrap, fingerprint tampering, and cancellation cleanup.
On POSIX, an independent stdio guardian also contains the server's process group
after abrupt worker death, including SIGKILL, without touching parent-owned MCP
servers. See [MCP stdio containment](mcp-stdio-containment.md) for the lifetime-pipe
protocol, bounded cleanup, disposable stop-test evidence, overhead, and limits
(including Windows and descendants deliberately escaping the owned group).
