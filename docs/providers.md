# Providers

A **provider profile** tells Tess Core how to reach a model. You declare profiles once in
a registry file, and each session picks one by name.

## The registry

```yaml
# providers.yaml
schema_version: 1
providerProfiles:

  claude:
    kind: anthropic
    defaultModel: claude-sonnet-5-5
    auth:
      kind: anthropic_api_key
      envVar: ANTHROPIC_API_KEY

  gpt:
    kind: openai
    apiMode: responses
    defaultModel: your-openai-model
    reasoningEffort: high
    auth:
      kind: openai_api_key
      envVar: OPENAI_API_KEY

  deepseek:
    kind: openai
    apiMode: chat_completions
    baseUrl: https://api.deepseek.com
    defaultModel: deepseek-chat
    auth:
      kind: custom_bearer
      envVar: DEEPSEEK_API_KEY

  local:
    kind: openai
    apiMode: chat_completions
    baseUrl: http://127.0.0.1:8080/v1
    defaultModel: your-local-model
    maxContextLength: 32000
    auth:
      kind: none
```

Pin the registry when you start the runtime:

```python
runtime = await start_runtime(home, provider_registry="providers.yaml", ...)
SessionOptions(provider="claude")            # by profile name
SessionOptions(provider="gpt", model="another-model", effort="low")
```

## Profile fields

| Field | Meaning |
|---|---|
| `kind` | `anthropic` or `openai`. Any OpenAI-compatible service uses `openai`. |
| `apiMode` | For `openai`: `responses` or `chat_completions`. |
| `defaultModel` | The model used when a session does not name one. |
| `baseUrl` | The endpoint, for anything other than the provider's own. |
| `auth` | Where the credential comes from. Required. |
| `reasoningEffort` | Default reasoning effort for models that support it. |
| `maxContextLength`, `maxOutputTokens` | Limits used for token accounting and compaction. |
| `headers` | Extra non-secret request headers. |
| `capabilities` | What the model accepts, such as image input. |
| `requestStartTimeoutMs`, `streamIdleTimeoutMs` | Transport timeouts. |

Profile names are lowercase and yours to choose.

## Credentials

`auth` has a `kind` and, for every kind but `none`, an `envVar`:

| `auth.kind` | Sends |
|---|---|
| `anthropic_api_key` | An Anthropic API key |
| `anthropic_auth_token` | An Anthropic bearer token |
| `openai_api_key` | An OpenAI API key |
| `openai_api_key_or_codex` | An OpenAI key or a Codex/ChatGPT token |
| `custom_bearer` | `Authorization: Bearer …` for compatible services |
| `none` | No credential (a local server) |

The registry **never contains a credential**. Literal keys, and `Authorization`, API-key
or cookie headers, are rejected when the file is loaded. Only the named environment
variable is read, and only when a request is made. Errors about the registry or a missing
credential are redacted.

For hosts that manage credentials for their users, `auth` may instead hold a
`credentialRef`: an opaque reference to a secret in the operating system keychain or an
encrypted file. See [Permissions and security](security.md#managed-credentials).

## What a registry guarantees

With a registry pinned, resolution is **strict**: there are no built-in profiles, no
provider guessed from a model name, no credentials discovered in files on disk, and no
fallback endpoint. A session uses exactly what its profile says, or fails with a clear
error. That is the behaviour to want in a service.

Without a registry, the runtime falls back to convenient defaults for interactive use:
built-in `anthropic` and `openai` profiles reading `ANTHROPIC_API_KEY` and
`OPENAI_API_KEY`, plus any profiles in the runtime home's `settings/providers.yaml`.

## Models on your own hardware

Any server that speaks the OpenAI Chat Completions API works: llama.cpp's `llama-server`,
vLLM, Ollama, LM Studio. Set `baseUrl`, `apiMode: chat_completions` and `auth.kind: none`.
Set `maxContextLength` to the context your server was started with, so the runtime
compacts before the window overflows.

## One runtime, many models

Profiles are chosen per session, so one process can serve a fast model for triage and a
strong one for hard work:

```python
def options_for(session_id, reason):
    tier = lookup_tier(session_id)
    return SessionOptions(provider="gpt" if tier == "premium" else "local")
```

A session keeps the profile and model it was created with. Reopening it later does not
silently move it to a different model.

Compaction can use its own model: set `compactionProviderProfile` in settings to a cheaper
profile for summarising long conversations.

## What the runtime handles for you

- **Streaming** with stall detection and recovery from interrupted streams
- **Retries** with bounded backoff, and a watchdog on requests that never settle
- **Prompt caching** where the provider supports it
- **Images** in and, for capable models, tool results that carry images
- **Reasoning effort**, mapped to each provider's own parameter
- **Usage and cost**: token counts on every result, and an estimated cost when pricing is
  known
- **Token accounting** before each request, so an oversized request is caught locally

## Checking a profile

```python
from tess_core import ProviderRegistry
from tess_core.providers.connection import check_provider_connection

registry = ProviderRegistry("providers.yaml")
report = await check_provider_connection(registry, "claude")
```

This lists the provider's models. It never sends a completion, so it costs nothing, and it
returns a state, a code and model ids only.

## Sign in with ChatGPT

With the `chatgpt-account` extra installed, a host can let its user sign in with a ChatGPT
account instead of pasting an API key. `tess_core.providers.chatgpt_account` runs the
browser sign-in on a loopback address, verifies the result, stores the tokens as a managed
credential and refreshes them when needed.
