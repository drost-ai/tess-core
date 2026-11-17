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
| `openAiChatReasoningSummary` | For `openai`: ask for reasoning summaries (`auto`, `concise` or `detailed`). |
| `anthropicThinkingDisplay` | For `anthropic`: how thinking is returned (`auto`, `summarized` or `hidden`). |
| `maxContextLength`, `maxOutputTokens` | Limits used for token accounting and compaction. |
| `headers` | Extra non-secret request headers. |
| `capabilities` | What the model accepts, such as image input. |
| `requestStartTimeoutMs`, `streamIdleTimeoutMs` | Transport timeouts. |

Profile names are lowercase and yours to choose.

**Images.** `capabilities.imageInput: true` says the model takes images (`supportsVision:
true` is the same). Then images in a prompt reach it, and so do images a tool returns, such
as a `Read` of a PNG. Without either, the model's name decides: Claude models, and OpenAI's
own GPT-4, GPT-5, GPT-6 and o-series, take images; models on other services are treated as
text-only. Declare it for any other model that can see.

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

## Provider samples

Three profiles that cover the shapes you will meet. Each is a complete `providers.yaml`
that has been run as written; change the model to one your account serves.

### A Codex sign-in

A ChatGPT account signed in through the Codex CLI. The profile declares no key; the
session names the sign-in file.

```yaml
schema_version: 1
providerProfiles:
  codex:
    kind: openai
    apiMode: codex
    defaultModel: gpt-6.1-sol
    reasoningEffort: xhigh
    openAiChatReasoningSummary: auto
    auth:
      kind: openai_api_key_or_codex
```

```sh
# .env
CODEX_AUTH_PATH=~/.codex/auth.json
```

```python
SessionOptions(provider="codex", provider_auth_path=os.environ["CODEX_AUTH_PATH"])
```

The file is read as it is and is not refreshed. If a request is refused as
unauthenticated, run the Codex CLI once to renew it.

### An Anthropic-compatible service

Services that speak the Anthropic Messages protocol use `kind: anthropic` and their own
`baseUrl`. This one is Kimi K3 on a coding plan:

```yaml
schema_version: 1
providerProfiles:
  kimi:
    kind: anthropic
    baseUrl: https://api.kimi.com/coding/v1
    defaultModel: k3
    reasoningEffort: max
    anthropicThinkingDisplay: summarized
    maxContextLength: 1000000
    maxOutputTokens: 256000
    requestStartTimeoutMs: 300000
    streamIdleTimeoutMs: 300000
    auth:
      kind: anthropic_api_key
      envVar: KIMI_API_KEY
```

```sh
# .env
KIMI_API_KEY=...
```

```python
SessionOptions(provider="kimi")
```

### An OpenAI-compatible service

Services that speak Chat Completions use `kind: openai`, `apiMode: chat_completions` and
a bearer key. This one is Z.ai's GLM on a coding plan:

```yaml
schema_version: 1
providerProfiles:
  zai:
    kind: openai
    apiMode: chat_completions
    baseUrl: https://api.z.ai/api/coding/paas/v4
    defaultModel: GLM-5.3
    reasoningEffort: max
    openAiChatReasoningSummary: auto
    maxContextLength: 1000000
    maxOutputTokens: 128000
    requestStartTimeoutMs: 300000
    streamIdleTimeoutMs: 300000
    capabilities:
      imageInput: false
    auth:
      kind: custom_bearer
      envVar: ZAI_API_KEY
```

```sh
# .env
ZAI_API_KEY=...
```

```python
SessionOptions(provider="zai")
```

A model on your own machine is the same shape with a local `baseUrl` and
`auth.kind: none`; see [Models on your own hardware](#models-on-your-own-hardware).

### Seeing the model's reasoning

Reasoning arrives as `message.assistant.thinking.partial` [events](events.md) when the
profile asks for it: `openAiChatReasoningSummary: auto` on an `openai` profile,
`anthropicThinkingDisplay: summarized` on an `anthropic` one. Raise the two timeouts, as
above, for models that think for a long time before their first byte.

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
