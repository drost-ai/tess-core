# Command line

Tess Core includes a small command for running one agent turn without writing any code.
It is meant for scripts, CI jobs and quick checks. For an application, use the
[Python API](embedding.md).

```bash
python -m tess_core run -p "Summarise the changes in this repository" --provider anthropic
```

## `run`

| Option | Meaning |
|---|---|
| `-p`, `--prompt` | The prompt. `-` reads it from standard input. |
| `--cwd` | The directory the agent works in. Default: the current directory. |
| `--runtime-home` | Where state is kept. Default: `$TESS_CORE_HOME`, then `~/.tess-core`. |
| `--session` | Continue a saved session, by id or by a search phrase. |
| `--provider` | A provider or profile name. |
| `--model` | A model name, overriding the profile's default. |
| `--effort` | Reasoning effort. |
| `--permission-mode` | `default`, `acceptEdits`, `auto`, `dontAsk` or `bypassPermissions`. |
| `--tools` | The tools to enable, comma-separated names. |
| `--allowed-tools` | Narrow the enabled tools to these names. |
| `--disallowed-tools` | Remove these tools. |
| `--input-format` | `text` (default) or `json`. |
| `--output-format` | `text` (default), `json` or `stream-json`. |
| `--max-turns` | The number of turns to run. Default: 1. |
| `--no-session-persistence` | Do not save the session. |

### Providers

The command knows two built-in providers, `anthropic` and `openai`, which read
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Profiles you define in
`<runtime-home>/settings/providers.yaml` are available by name.

A Codex sign-in is used only when you name its file, for example
`TESS_OPENAI_CODEX_AUTH_PATH=~/.codex/auth.json`. The command never looks for one, so
being signed in to another tool does not change which provider or credential a run uses.

### Output

`text` prints the final answer.

`json` prints one object when the turn ends:

```json
{
  "success": true,
  "exitCode": 0,
  "sessionId": "…",
  "text": "…",
  "model": "…",
  "stopReason": "end_turn",
  "usage": {"input_tokens": 1820, "output_tokens": 214},
  "toolCount": 3,
  "estimatedCostUsd": 0.0087
}
```

`stream-json` prints one event per line as the turn runs, starting with `system.init` and
ending with `result`, which carries the same object as `json`. The events are those in the
[event catalogue](events.md). Image data is redacted and very long strings are truncated.

```bash
python -m tess_core run -p "Run the tests and report" --provider anthropic \
    --output-format stream-json | jq -r 'select(.event == "tool.started") | .data.name'
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The turn completed |
| 1 | The turn failed or was cancelled, or the provider has no credential |
| 2 | A tool call needed approval. The command cannot ask, so it was refused. |

### Permissions without a person

Nobody is there to answer a prompt, so a call that would ask is refused and the command
exits with 2. Decide the authority up front. There are three levers:

**Which tools exist.** The tool options take tool names and decide what the model can
call at all. They do not take permission rules: `--allowed-tools "Bash(git diff:*)"` is
refused with an explanation. A read-only review needs nothing else:

```bash
python -m tess_core run -p "Review src/ for bugs" --provider anthropic \
    --tools Read,Glob,Grep
```

**Which calls are allowed.** Permission rules live in
`<runtime-home>/settings/config.yaml`:

```yaml
permissions:
  allow:
    - Bash(pytest:*)
    - Bash(git diff:*)
```

```bash
python -m tess_core run -p "Fix the failing test" --provider anthropic \
    --runtime-home ./ci-agent --permission-mode acceptEdits
```

**The mode.** `--permission-mode` sets how everything not covered by a rule is treated.
See [Security](security.md) for the modes and the rule syntax.

### Continuing a conversation

```bash
python -m tess_core run -p "Start a changelog entry" --provider anthropic --output-format json \
    | jq -r .sessionId > session.txt
python -m tess_core run -p "Add the migration notes" --session "$(cat session.txt)"
```

### In CI

Keep a runtime home with its settings in the repository, for example `ci-agent/settings/config.yaml`
holding the allow rules above, and point the command at it. Ignore everything in that
directory except `settings/`:

```yaml
- name: Agent review
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  run: |
    python -m tess_core run \
      -p "Review the diff against origin/main. Reply with a list of problems, or 'OK'." \
      --provider anthropic --tools Read,Glob,Grep,Bash \
      --runtime-home ci-agent --no-session-persistence
```

## What the command does not do

It runs one conversation and exits. Sub-agents, background tasks, the scheduler and
monitors need a running runtime; use [`start_runtime`](embedding.md) for those.
