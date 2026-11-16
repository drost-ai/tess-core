# Configuration

Tess Core is configured in three places, from most to least specific:

1. **Code**: `SessionOptions` and the arguments to `start_runtime`. What your application
   decides.
2. **Environment variables**: `TESS_CORE_<SECTION>_<NAME>`. What an operator overrides at
   deploy time.
3. **The settings folder**: `<runtime-home>/settings/`. What is true for this
   installation.

Everything has a default. An empty settings folder is a valid configuration.

## The settings folder

```
<runtime-home>/settings/
  providers.yaml           provider profiles, default provider and model
  config.yaml              runtime settings
  config/                  optional: config.yaml split into one file per section
    compaction.yaml
    subagents.yaml
  mcp/                     one JSON file per MCP server
    crm.json
  .env                     credentials only, KEY=VALUE per line
  system_prompt.md         replaces the base system prompt
  system_prompt_append.md  appended to whichever base prompt is used
```

Every file is optional.

### Mistakes are fatal, by design

A malformed file, an unknown setting, a value out of range, or the same setting defined in
two files stops the runtime at start, with an error that names the file. A setting is
never silently ignored. In production you want to learn about a typing mistake at deploy
time, not three weeks later.

### `config.yaml`

Runtime settings live under `knobs:`, grouped by section:

```yaml
knobs:
  subagents:
    max_live: 4
    max_depth: 1
  sessions:
    idle_close_ms: 600000
  agent:
    no_progress_limit: 8
```

Each setting has a type, a default and an allowed range. The complete list is the
[settings reference](reference/settings.md).

To split the file, put fragments in `settings/config/`. They are merged with
`config.yaml`. Each setting must be defined in exactly one file.

### Environment overrides

Any setting can be overridden for one process:

```bash
TESS_CORE_SUBAGENTS_MAX_LIVE=2 python my_service.py
```

The name is `TESS_CORE_` followed by the section and setting in upper case, joined by an
underscore. Environment variables win over the settings folder.

### `providers.yaml`

Provider profiles. See [Providers](providers.md).

An application that manages its own profiles can keep the registry anywhere and pass its
path to `start_runtime(provider_registry=...)`. When it does, only that registry is used.

### `.env`

Credentials for the settings folder, one `KEY=VALUE` per line. It is for secrets only and
should be readable by the service account alone. Most deployments leave it out and supply
secrets through the process environment or managed credentials.

### System prompt

The base prompt is chosen in this order:

1. `SessionOptions.system_prompt`, when your application sets one
2. `settings/system_prompt.md`
3. Your application's default prompt, when it provides one
4. The built-in default

`system_prompt_append.md` is then added after whichever base was chosen. A session records its composed prompt when it is created, so
editing the files affects new sessions.

### The YAML subset

Settings use a deliberately small subset of YAML: mappings, lists, strings, numbers,
booleans and null. No anchors, tags or multi-document files. It is parsed by Tess Core
itself, which is part of how the runtime keeps zero dependencies. Anything outside the
subset is an error.

## Permissions

The permission mode is set in code (`SessionOptions.permission_mode`). Rules live in
settings: at the top level of `config.yaml`, or in a file passed as
`SessionOptions.settings`.

```yaml
permissions:
  allow:
    - Read
    - Bash(git status:*)
  ask:
    - Bash(git push:*)
  deny:
    - Read(**/.env)
    - Grep(**/.env)
```

A deny rule covers the tool it names. `Read(**/.env)` alone leaves `Grep` able to print the
file, and a shell command such as `cat .env` is a `Bash` call. If a secrets file must stay
out of an agent's reach, keep it outside the session's working directory.

See [Security](security.md) for what each rule means and the order they apply in.

## Settings worth knowing

| Setting | Default | Why you would change it |
|---|---|---|
| `sessions.idle_close_ms` | 15 minutes | How long an idle session keeps its resources |
| `subagents.max_live` | 8 | Concurrency against cost and rate limits |
| `subagents.max_depth` | 2 | Whether sub-agents may start their own |
| `agent.no_progress_limit` | 12 | How patient the loop guard is |
| `comms.reply_timeout_ms` | 5 minutes | How long an agent waits for an answer from another |
| `scheduler.max_late_ms` | unset | Skip scheduled jobs that are too late to matter |
| `mcp.startup_timeout_ms` | see reference | Slow MCP servers |

## For services

- Pass an explicit `provider_registry` and set `strict_mcp_config=True` in
  `SessionOptions`. The service then uses exactly the providers and MCP servers its code
  names, whatever is on the machine.
- Keep secrets out of files. Use the process environment or managed credentials.
- Keep the settings folder in version control without `.env`.
- Start the service in a staging environment after changing settings. Because mistakes are
  fatal, a clean start is a real check.
