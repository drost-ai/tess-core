# MCP

The [Model Context Protocol](https://modelcontextprotocol.io) lets an agent use tools and
data served by other programs. Tess Core is an MCP client: you describe a server, and its
tools and resources appear in the session.

## Describing a server

A server descriptor is a small record:

```python
crm = {
    "name": "crm",
    "transport": "stdio",
    "commandOrUrl": "/usr/local/bin/crm-mcp",
    "args": ["--read-only"],
    "env": {"CRM_REGION": "eu"},
    "inheritEnv": False,                 # start with only the env above
    "cwd": "/srv/crm",
    "startupTimeoutMs": 10000,
    "requestTimeoutMs": 60000,
}

SessionOptions(provider="claude", mcp_servers=(crm,))
```

| Field | Meaning |
|---|---|
| `name` | Your name for the server. Tools appear as `mcp__<name>__<tool>`. |
| `transport` | `stdio`, `http` or `sse` |
| `commandOrUrl` | The executable for `stdio`, the URL otherwise |
| `args`, `env`, `cwd` | For `stdio` servers |
| `inheritEnv` | `False` replaces the parent environment with `env`. Default: inherit. |
| `headers` | Non-secret headers for `http` and `sse` |
| `startupTimeoutMs`, `requestTimeoutMs` | Bounds on the handshake and on each call |

## Where servers come from

1. **Per session**: `SessionOptions.mcp_servers`, as above.
2. **The runtime home**: one JSON file per server in `settings/mcp/<name>.json`, loaded for
   every session.
3. **Shared**: servers named in `start_runtime(shared_mcp_servers=...)` run once and are
   shared by all sessions, instead of once per session.

Set `strict_mcp_config=True` in `SessionOptions` to use only the servers you pass and
ignore everything configured elsewhere. A service should do this.

## Using MCP tools

Once connected, a server's tools are ordinary tools. The model calls
`mcp__crm__search_accounts` like any other. Two built-in tools help it look around:

- `Mcp` reports connected servers and what they offer.
- `ListMcpResources` and `ReadMcpResource` browse resources.

Your host code can call a session's MCP tools directly, through the connection the session
already has:

```python
result = await session.mcp_call("crm", "search_accounts", {"query": "Acme"})
```

## Permissions

MCP tools run code outside the built-in toolset, so they are treated with care:

- In interactive modes, an MCP tool call asks unless a rule allows it.
- In `auto` and `dontAsk`, an MCP tool is **refused unless an allow rule names it**.

```yaml
permissions:
  allow:
    - mcp__crm__search_accounts
    - mcp__crm__get_account
  deny:
    - mcp__crm__delete_account
```

## Containment

A stdio MCP server is a child process, and child processes are where agent systems leak.
Tess Core is strict about them:

- **Bounded startup.** A server that does not complete its handshake in time is stopped
  and reported, with the real cause when there is one (a framing error, a bad encoding).
- **Bounded messages.** Oversized frames, invalid UTF-8 and malformed JSON-RPC end the
  connection instead of consuming memory.
- **No orphans.** Each server runs under a small guardian process. When the session
  closes, the runtime stops, or the host itself dies, the server and everything it
  started are terminated.
- **Isolated environment.** With `inheritEnv: False`, the server sees only the variables
  you list, so it cannot read your provider keys.
- **Health is observed.** A server that exits is noticed and reported; reconnection backs
  off instead of spinning.

The [containment design note](design/mcp-stdio-containment.md) explains the mechanism.

## Remote servers and OAuth

`http` and `sse` servers are supported, including servers that require OAuth. The runtime
runs the authorization flow, stores tokens in the runtime home, and refreshes them. A
server waiting for authorization reports that state instead of failing.

## Shared servers

A server that is expensive to start, or that should hold one connection to a backend, can
be shared:

```python
runtime = await start_runtime(home, shared_mcp_servers=("search-index",), ...)
```

Sessions then reach it through a broker in the runtime instead of each starting a copy.
See the [shared broker design note](design/shared-mcp-broker.md).

## Sub-agents and MCP

A sub-agent gets its own connections to its parent's MCP servers, subject to the same
permission rules. Servers that should not be handed to children can be excluded. See the
[child runtime design note](design/child-mcp-runtime.md).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `startup_timeout` | The server is slow to start or never answered `initialize`. Raise `startupTimeoutMs` or check the command. |
| `invalid_framing`, `invalid_encoding`, `frame_limit` | The server wrote something that is not JSON-RPC to stdout. Logs belong on stderr. |
| Tools missing in `auto` mode | No allow rule names them. |
| Server cannot find its config | `inheritEnv: False` removed a variable it needs. Add it to `env`, including `PATH`. |
