# Host-owned shared MCP broker

`tess_core.mcp.shared_broker_http.SharedMcpBroker` owns a loopback-only,
authenticated HTTP MCP endpoint and a pool of upstream connections. Session
clients use the existing HTTP MCP transport; ordinary standalone stdio clients
are unchanged. The host starts/closes the broker, not a session or child.

## Admission contract

An upstream must explicitly contain both:

```json
{"sharing":{"mode":"host"},"delegation":{"mode":"allow","scope":"shared"}}
```

Absent sharing, denied delegation, and session/personal scope fail closed.
The engine has no memory-server name hardcoded. Product configuration decides
which services to opt in. Sharing is an assertion that the upstream's state and
security principal are appropriate to share: the broker does not make an
inherently session-private server safely multi-tenant.

```python
from tess_core.mcp.shared_broker import BrokerGrant
from tess_core.mcp.shared_broker_http import (
    SharedMcpBroker, delegate_shared_mcp_descriptor,
    revoke_shared_mcp_descriptor,
)

broker = SharedMcpBroker()
await broker.start()
parent = await broker.acquire_descriptor(
    upstream_config, cwd=upstream_cwd,
    source_fingerprint=pinned_upstream_fingerprint,
    grant=BrokerGrant(parent_session_id, tools=None, resources=None),
)
child = await delegate_shared_mcp_descriptor(
    parent, BrokerGrant(child_session_id, tools=None, resources=frozenset()),
    enabled_tools=frozenset(compiled_child_runtime_tool_names),
)
# On terminal child lifecycle (not merely a reconnect):
await revoke_shared_mcp_descriptor(child)
# On host shutdown:
await broker.close()
```

A grant's empty set denies; `None` explicitly authorizes the whole upstream
catalog. Direct admission is a trusted host API and must use compiled authority.
Remote delegation is authenticated by the parent's bearer and can only narrow
its immutable tool/resource grant. Runtime tool names are mapped through the
broker's upstream catalog and the engine's canonical naming function, never
reverse-parsed from sanitized names. Resources have independent exact-URI
permissions. Arbitrary protocol methods, subscriptions, prompts, sampling,
server callbacks, and child notifications are not forwarded.

## Persistence and isolation

Returned descriptors use canonical `commandOrUrl`, `transport: "http"`,
`sharedBroker: {"version": 1}`, and an Authorization header containing an opaque
existing runtime-secret reference. Bearers are independently random 256-bit
capabilities, stored using the existing private secret store; no plaintext
bearer is persisted in a session descriptor. Changing the bearer never changes
the upstream pool key. The key includes the entire unresolved upstream
descriptor, cwd, and pinned upstream source fingerprint, **not** a child's
authority fingerprint. Upstream env/header references resolve only immediately
before connection. HTTP/SSE upstreams reuse the existing OAuth implementation
and token store; interactive authorization needs an injected host client factory.

Tokens authorize calls server-side, including requests manually crafted without
using tool discovery. Revocation synchronously removes an entire descendant
capability subtree before waiting for cancellation. Secret-store records can
outlive tokens, but revoked or previous-host tokens cannot authenticate. The
broker endpoint is ephemeral and host-owned; a stopped host requires new
admission, never fallback to spawning a private copy of its upstream.

This is a capability boundary between cooperating runtime processes, **not an
OS sandbox against arbitrary code running as the same Unix user**. Like existing
runtime secrets, credential references should not be exposed to untrusted code.
No unauthenticated global socket, browser origin, or admission endpoint exists.

## Cancellation and verification

Every forwarded request has its own upstream request ID and task. Disconnecting
or cancelling one child cancels only its request. Stdio cancellation wakes its
executor queue waiter before removing its pending route and sends best-effort
request-specific `notifications/cancelled`; it never closes the shared service.
Child cleanup revokes that capability; only host shutdown closes upstreams.

`tests/test_shared_mcp_broker.py` uses a disposable real stdio server and 32 real
subprocess HTTP clients, waits until all 32 have initialized, releases them
concurrently, and verifies exactly one backend PID was spawned. It also covers
forged tokens, direct denied calls, filtered discovery, resource authorization,
nested attenuation, source isolation, personal exclusion, secret references,
remote cancellation, repeated real stdio cancellation with a four-worker
executor, and cancellation-resistant atomic subtree revocation.

## Forced exits and retention limitations

A killed child cannot run its cleanup. TCP EOF cancels that connection's
in-flight operation, but **does not revoke its capability**: the same bearer can
open multiple HTTP connections. Without a host terminal-task observer invoking
revocation, that child's token remains valid until host shutdown. This module
currently has no time-based expiry/reaper and no lease-count admission limit.
The trusted host API `await broker.revoke_child(child_id)` removes every
matching grant and its descendants atomically before draining requests. It is
not a remote RPC; children cannot revoke sibling identities. Hosts should call
it on durable terminal child transitions and reuse a root grant
when reopening the same authorized parent session instead of minting endlessly.

The existing private runtime secret store has no deletion API here. Issued
secret-reference records accumulate even after revocation; revocation invalidates
the server-side token rather than deleting its stored bytes. Inert records need
a separately designed secret-store retention policy. This implementation does
not claim automatic SIGKILL credential revocation or bounded on-disk retention.
