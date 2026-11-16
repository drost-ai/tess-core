# Permissions and security

An agent that can act can do damage. Tess Core's position is that safety belongs in the
runtime, where it is enforced on every tool call, and not in the prompt, where it is a
request.

## The decision, in order

Every tool call is checked like this:

1. A matching **deny rule** refuses it. Nothing overrides a deny.
2. In `bypassPermissions` mode, it is allowed.
3. A matching **ask rule** asks.
4. A matching **allow rule** allows it.
5. Otherwise the **permission mode** decides, according to what kind of action it is.

File tools are additionally checked against the session's **allowed roots**, and a
sub-agent is additionally bound by the **authority its parent pinned**.

## Permission modes

| Mode | Edits files | Shell commands | Use it for |
|---|---|---|---|
| `default` | Asks | Asks, unless clearly read-only | A person at the keyboard |
| `acceptEdits` | Allowed | Asks, unless clearly read-only | Supervised coding |
| `auto`, `dontAsk` | Allowed | Allowed only if the command policy allows them; everything else is refused | Unattended services |
| `bypassPermissions` | Allowed | Allowed | A workspace you are willing to lose, such as a container |

Details worth knowing:

- **Read-only commands never ask.** The shell command policy recognises commands such as
  `ls`, `cat`, `git status` and `git diff` and lets them through.
- **Non-interactive modes refuse instead of asking.** In `auto` and `dontAsk`, MCP tools
  need an explicit allow rule, and worktree creation, stopping processes and recurring
  network checks are refused.
- **A person can widen the mode once.** When a prompt appears, one of the answers switches
  the session to the next mode, so the same question does not come back.

Set the mode per session:

```python
SessionOptions(permission_mode="auto")
```

## Permission rules

Rules live under `permissions` in settings, in three lists:

```yaml
permissions:
  allow:
    - Bash(git status)
    - Bash(npm run test:*)
    - Read(docs/**)
    - mcp__crm__search_accounts
  ask:
    - Bash(git push:*)
  deny:
    - Bash(rm:*)
    - Write(.env)
    - WebFetch
```

- A rule is a tool name, optionally with a pattern in parentheses.
- For `Bash`, the pattern is a command. Ending it with `:*` matches that command and
  anything that begins with it.
- For file tools, the pattern is a path glob, where `**` crosses directories.
- A bare tool name matches every call to that tool.

**Deny rules look inside compound commands.** `Bash(rm:*)` matches `rm -rf x`, but also
`cd /tmp && rm -rf x` and `/bin/rm x`. Allow rules are stricter and match only a single,
plain command.

Pass rules to a session with the `settings` option, or put them in the runtime home's
settings. Allow rules that arrive from a repository's own settings file do not count until
that repository has been approved, so cloning a project cannot grant itself permissions.

## Path confinement

File tools may touch the session's working directory and the directories listed in
`add_dirs`. A path outside those roots:

- is refused outright in `auto`, `dontAsk` and `bypassPermissions`, and for sub-agents;
- prompts for one-time approval in interactive modes.

Symlinks are resolved before the check, so a link inside the workspace cannot be used to
reach outside it.

## Answering prompts

When the runtime needs a decision it calls your listener:

```python
async def ask_question(self, session_id, question, options, detail=None):
    if detail and detail.get("kind") == "permission":
        approved = await show_approval_dialog(session_id, detail["tool"], detail["summary"])
        return options[0] if approved else options[-1]
    return await show_question(session_id, question, options)
```

`detail` describes a permission request: the tool, a summary of the call, the mode and the
reason. Every request and decision is also emitted as `permission.requested` and
`permission.decided` events, with the source of the decision (a rule, the mode, a prompt),
so you have an audit trail without building one.

With no `ask_question`, the session is non-interactive and anything that would ask is
refused.

## Hooks

Hooks run a command of yours at fixed points: `PreToolUse`, `PostToolUse`, `Stop` and
`SessionEnd`. A `PreToolUse` hook marked `blocking` can veto a tool call, which is how you
plug in a policy engine or a scanner. Hooks have a timeout, and hooks that come from a
repository must be approved before they run.

## Sub-agent authority

A sub-agent receives its authority when it is launched, and it is fixed. A child cannot
change its permission mode, cannot gain rules its parent did not have, and cannot be
granted new directories afterwards. Nesting depth and the number of live children are
bounded. See [Sub-agents](sub-agents.md).

## OS sandbox for shell commands

Shell commands can additionally run inside an operating-system sandbox: Seatbelt on macOS,
Bubblewrap on Linux. The sandbox limits writes to the allowed roots and can cut off the
network. Configure it under `sandbox` in settings (`mode`, `writableRoots`,
`readableRoots`, `allowNetwork`).

Treat this as defence in depth. It narrows what a mistaken command can do; it is not a
boundary designed to hold hostile code. For untrusted workloads, run the whole runtime in a
container or virtual machine.

## Credentials the agent cannot reach

Two protections apply without configuration:

- **Provider keys are removed from the shell.** A command the agent runs does not see
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or any variable a provider profile names in
  `auth.envVar`, so `env` or `echo $OPENAI_API_KEY` reveals nothing. This holds in every
  mode, including `bypassPermissions`, and for background commands.
- **Credentials files need approval.** `.env` and `.env.*` files (templates such as
  `.env.example` excepted), private keys, `.netrc`, and anything under `~/.ssh`, `~/.aws`
  or `~/.codex` are not ordinary reads. `Read` and `Grep` ask before opening one, a shell
  command that names one is not treated as read-only, and in `auto` and `dontAsk` they are
  refused unless an allow rule names the file.

`bypassPermissions` turns the second protection off, as it does every prompt: in that mode
the agent can read any file your user account can. Keep secrets out of the working
directory of a session that runs in it.

## Credentials

- **Provider credentials** are never stored in configuration. A profile names an
  environment variable or a managed reference. See [Providers](providers.md).
- **Registry files are checked on load.** Literal keys and secret-bearing headers are
  rejected.
- **Errors are redacted.** Registry, profile and missing-credential errors do not echo
  values.
- **Child processes get what they need.** An MCP server can be started with
  `inheritEnv: false` and an explicit environment, so it never sees your keys.

### Managed credentials

For applications that hold credentials on behalf of their users, `CredentialStore` keeps
secrets in the macOS Keychain, the Linux Secret Service (`desktop-credentials` extra) or an
AES-256-GCM encrypted file (`encrypted-credentials` extra). Code handles opaque references;
the secret is resolved only at the moment a request is sent. Nothing is selected
automatically and there is no plaintext fallback.

## MCP servers

MCP servers are other people's code running with your agent's reach. Tess Core bounds
their startup time and message size, ties their lifetime to the session so no process is
left behind, lets you isolate their environment, and treats their tools as untrusted by
default in unattended modes. See [MCP](mcp.md).

## Web access

`WebFetch` refuses private and loopback addresses unless you allow-list them, which stops
an agent being steered into your internal network.

## What Tess Core does not do

Being clear about limits is part of being secure.

- **It is not a multi-tenant isolation boundary.** Everything in one runtime runs as one
  operating-system user. Isolate tenants with separate processes, users or containers.
- **It does not judge content.** Prompt injection in a web page or a file can still ask
  the model to misuse the permissions the session has. Grant the least you can.
- **`bypassPermissions` means what it says.**
- **It holds no certifications.** It gives you the controls and the audit events; your
  deployment supplies the compliance.

## A checklist for production

- Pin a provider registry.
- Use `auto` with an explicit command policy, not `bypassPermissions`.
- Write deny rules for what must never happen, even if you believe nothing would try.
- Give each session the narrowest workspace that works.
- Start MCP servers with `inheritEnv: false`.
- Record `permission.*` and `tool.*` events.
- Put the runtime home on storage you back up and restrict to the service user.
