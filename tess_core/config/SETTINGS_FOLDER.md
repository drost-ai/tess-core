# The settings/ folder

Cross-runtime contract for `<runtime-home>/settings/` — the successor of the
monolithic user-scope `settings.json`. Any runtime implementation (Python,
TypeScript, Rust) must load it identically.

## Layout

```
settings/
  .env                     credentials only (KEY=VALUE lines), feeds the env block
  providers.yaml           provider profiles + provider/model selection
  config.yaml              runtime configuration (knobs: section + config keys)
  config/                  optional fragments of config.yaml, one file per section
    daemon.yaml
    compaction.yaml
    ...
  system_prompt.md         replaces the base system prompt
  system_prompt_append.md  appended after whichever base prompt wins
```

Everything is optional. YAML files use the strict subset specified in
`YAML_SUBSET.md`. Malformed content is a fatal startup error — settings
never silently degrade.

## Fragment merge rules (settings/config/)

1. **Load order**: `config.yaml` is the base, then `config/*.yaml` in
   lexicographic filename order. Non-recursive; dotfiles and files without a
   `.yaml` suffix are ignored; empty or comment-only files are skipped.
2. **Merge**: mappings deep-merge. A fragment may hold any section — the
   directory is a user-organization feature, not a schema.
3. **Conflicts are fatal**: the same setting (any non-mapping leaf, or a
   section-vs-value mismatch) defined in two files — fragment vs fragment or
   `config.yaml` vs fragment — aborts startup with an error naming both
   files. Each setting lives in exactly one file. Last-wins conf.d
   semantics were considered and rejected.
4. **Each fragment must parse to a mapping** (same rule as `config.yaml`).

## Precedence (unchanged by fragments)

- `providers.yaml` wins over `config.yaml`+fragments for its top-level keys
  (`env` and `providerProfiles` merge per entry).
- The folder augments and overrides legacy `settings.json` per top-level key.
- Process environment variables (`TESS_CORE_<DOTTED>`) remain the only
  override layer above the folder.

## System prompt

The system prompt is two concepts, each with one settings-layer file:

- **Replace**: `system_prompt.md` becomes the base prompt. Full precedence:
  `options.systemPrompt` (embedder force) > `settings/system_prompt.md` >
  `options.defaultSystemPrompt` (product layer) > the built-in default
  (`buildDefaultSystemPrompt`, `config/prompts.py`).
- **Append**: `system_prompt_append.md` is appended after whichever base
  wins, separated by a blank line (`options.appendSystemPrompt` wins over
  the file). Composition happens in `buildEffectiveSystemPrompt`
  (`config/prompts.py`); sessions stamp the composed prompt at creation.

Any text is valid; trailing whitespace is stripped, and an empty or
whitespace-only file is ignored. `systemPrompt` / `appendSystemPrompt` /
`defaultSystemPrompt` keys inside settings records (`config.yaml` or legacy
`settings.json`) are **not** consulted — the files are the settings-layer
mechanism.

## Reference

`config.yaml.example` (generated from the knob registry, lockstep-tested) is
the single complete reference of every knob; fragments never add settings
that the example does not document.

## Model limit rules (providers.yaml)

`providers.yaml` may carry a top-level `modelLimitRules` list to extend or
override the builtin model→token-limit inference table:

```yaml
modelLimitRules:
  - match: my-local-model        # case-insensitive substring of model OR profile name
    maxContextLength: 48000
    maxOutputTokens: 8000        # either field may be omitted
```

Precedence per field: explicit profile fields (`maxContextLength`,
`maxOutputTokens`) > `modelLimitRules` (first match wins) > builtin
inference table > `provider.*` knob defaults. Malformed rules are a fatal
startup error.

## Knobs

Every process-wide tunable is a **knob**: declared once in
`_knob_registry.py` with a dotted name, default, type, bounds, and
description. Override precedence: default < `knobs:` in
`config.yaml`/fragments < `TESS_CORE_<DOTTED>` env var (dots become
underscores, upper-cased — `daemon.rpc_timeout_ms` →
`TESS_CORE_DAEMON_RPC_TIMEOUT_MS`). Unknown knob names in settings are a
fatal startup error.

What may become a knob at all is governed by `KNOB_CLASSIFICATION.md`:
security bounds, protocol constants, and schema versions never; per-provider
values belong in `providers.yaml`; per-session behavior in config keys.
Where a knob backs a per-profile field (timeouts, image caps, compaction
threshold), the profile always wins — the knob is only the default layer.

Deprecated env aliases (`TESS_COMPACTION_*_TIMEOUT_MS`,
`TESS_SUBAGENT_MAX_CONCURRENCY`, `TESS_CORE_SUBAGENT_MAX_CONCURRENCY`)
still work but print a one-time warning naming the canonical form.
`TESS_SUBAGENT_CONCURRENCY_GROUP_LIMITS` /
`TESS_CORE_SUBAGENT_CONCURRENCY_GROUP_LIMITS` (a `group=limit` comma list)
remains env-only — structured values do not fit the knob grammar.
