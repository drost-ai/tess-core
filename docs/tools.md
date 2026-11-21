# Tools

Tools are how an agent acts. Tess Core ships 27, and every one passes through the
[permission system](security.md) before it runs.

## Built-in tools

### Files

| Tool | Does |
|---|---|
| `Read` | Read a file, or a line range of a large one. Reads images and notebooks too. |
| `Write` | Write a whole file, creating parent directories. |
| `Edit` | Replace an exact string in a file, once or everywhere. |
| `Glob` | List files matching a pattern. |
| `Grep` | Search file contents with regular expressions, with context lines and several output modes. |
| `NotebookEdit` | Edit Jupyter notebook cells by id. |

File tools are confined to the session's workspace and any directories you added. See
[path confinement](security.md#path-confinement).

`Glob` and `Grep` use [ripgrep](https://github.com/BurntSushi/ripgrep) when `rg` is on the
`PATH`, because it is fast. Without it they use a search built into Tess Core that takes
the same arguments and prints the same results. The built-in search is slower on large
trees, does not read `.gitignore` files, and takes Python regular expressions.

### Shell

| Tool | Does |
|---|---|
| `Bash` | Run a shell command in the workspace, with a timeout. `run_in_background` hands it to the task daemon. |

Read-only commands such as `ls` or `git status` are recognised and run without a prompt.
Anything else is decided by the permission mode and rules.

### Web

| Tool | Does |
|---|---|
| `WebFetch` | Fetch a URL and return a text snapshot. Private addresses are refused unless allow-listed. |
| `WebSearch` | Search the web and return ranked results, with domain filters and freshness. |

### Agents

| Tool | Does |
|---|---|
| `Agent` | Start a sub-agent on a focused task. Returns at once; the child works in the background. |
| `SendMessage` | Message a related agent: the parent from a child, or a child from its parent. |

See [Sub-agents](sub-agents.md).

### Background work

| Tool | Does |
|---|---|
| `TaskOutput` | Read the output of a background task. |
| `TaskStop` | Stop a background task. |
| `TaskMonitorStart`, `TaskMonitorStop` | Watch a background task and be told when it changes. |
| `TaskHealthCheckStart`, `TaskHealthCheckStop` | Run a periodic check of a URL or command. |

See [Background tasks](background-tasks.md).

### MCP

| Tool | Does |
|---|---|
| `Mcp` | Inspect connected MCP servers and what they offer. |
| `ListMcpResources`, `ReadMcpResource` | Browse and read resources published by MCP servers. |

Tools published by MCP servers appear alongside these as `mcp__<server>__<tool>`. See
[MCP](mcp.md).

### Workspace and session

| Tool | Does |
|---|---|
| `EnterWorktree`, `ExitWorktree` | Work in an isolated git worktree and return. |
| `TodoWrite` | Keep the session's to-do list. |
| `SetStatus` | Update a short live status line for the user. |
| `AskUserQuestion` | Ask the user one or more multiple-choice questions. |
| `Config` | Read the active configuration. |

### Scheduling

When the runtime's scheduler is running, sessions also get `ScheduleCreate`,
`ScheduleList`, `ScheduleUpdate` and `ScheduleCancel`. See [Scheduler](scheduler.md).

## Choosing tools

Narrow a session to the tools it needs, for one turn or for the whole session:

```python
await session.run_turn("Review this diff.", tools=["Read", "Grep", "Glob"])   # one turn

SessionOptions(extra={"tools": ["Read", "Grep", "Glob"]})                     # the session
```

For a session there are three lists, each of tool names:

| Key | Effect |
|---|---|
| `tools` | The only tools the session has. Built-in, host, MCP and plugin tools not named here are left out. |
| `allowedTools` | Narrows the session's tools to these names. |
| `disallowedTools` | Removes these names and keeps everything else. |

An empty `tools` or `allowedTools` list means no tools at all. `session.tool_names` reports
what the session ends up with. See [Embedding](embedding.md#choosing-a-sessions-tools) for
running a session on your own tools only.

On the command line, `--tools`, `--allowed-tools` and `--disallowed-tools` do the same.
Permission rules can deny a tool outright for a whole session.

## Parallel and exclusive tools

The model often asks for several tools at once. Tools marked parallel (`Read`, `Grep`,
`Glob`, `WebFetch`, `WebSearch` and other read-only ones) run concurrently. Tools that
change something (`Write`, `Edit`, `Bash`, `Agent`) run one at a time, in order.

## Large results

A tool that returns megabytes would flood the conversation. A result of up to 30,000
characters is returned whole. A larger one is saved to a file in the runtime home, and the
model receives a notice with the file's path and the first 2 KB:

```
<persisted-output>
Output too large (67.3KB). Full output saved to: <path>

Preview (first 2.0KB):
...
</persisted-output>
```

It reads the rest with `Read`, which returns 2,000 lines at a time and pages with `offset`
and `limit`. The allowance is the same for every model, and is
[configurable](configuration.md) (`payloads.inline_output_chars`).

A tool of your own gets the same allowance. Set `maxResultChars` on its `ToolDefinition` to
give it another, or to `0` if the tool bounds its own results, as `Read` does by paging.

## Writing your own

```python
from tess_core import ToolDefinition

async def create_ticket(arguments, context):
    ticket = await tracker.create(title=arguments["title"], body=arguments.get("body", ""))
    return {"content": f"Created {ticket.key}: {ticket.url}"}

tickets = ToolDefinition(
    name="CreateTicket",
    description=(
        "Create a ticket in the issue tracker. Use when the user asks to file, "
        "log or track something. Returns the new ticket's key and URL."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "One-line summary"},
            "body": {"type": "string", "description": "Details, in Markdown"},
        },
        "required": ["title"],
    },
    handler=create_ticket,
)

SessionOptions(provider="claude", host_tools=(tickets,))
```

**The contract**

- `handler(arguments, context)` is async. `arguments` has been checked against
  `inputSchema`. `context` carries the session and its configuration.
- Return `{"content": "<text for the model>"}`. Add `"isError": True` when the call
  failed, so the model knows to recover instead of trusting the text.
- Leave `concurrency` unset for anything with side effects. Set `"parallel"` only for
  tools that are safe to run several at once.

**Writing a good tool**

- The description is the model's only documentation. Say what the tool does, when to use
  it, and what it returns.
- Describe every parameter. Prefer a few required fields to many optional ones.
- Return text a person could read. Include the identifiers the model will need next.
- Fail with a message that says what to do differently.

## Tools from MCP servers and plugins

An MCP server is the right home for tools that live in another process or another
language, or that several agents share. Configure the server and its tools appear in the
session. See [MCP](mcp.md).
