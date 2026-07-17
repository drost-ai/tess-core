from __future__ import annotations

from .shared import *
from ..provider_timeouts import resolveProviderTimeoutPolicy
from ..runtime_secret_provider import resolveProviderConfigSecrets

def _create_provider(config: dict[str, Any], runtime: dict[str, Any], session: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session, dict) and isinstance(session.get("metadata"), dict) else {}
    timeout_policy = resolveProviderTimeoutPolicy({"config": config, "runtimeContract": metadata.get("subagentRuntimeContract")})
    if config.get("provider") == "openai":
        if not config.get("openaiToken"):
            return {"send": lambda request: _missing_openai_credential_response(config, request)}
        async def send_openai(request: dict[str, Any]) -> dict[str, Any]:
            resolved = await resolveProviderConfigSecrets(config)
            return await sendOpenAiRequest(
                token=resolved["openaiToken"],
                baseUrl=resolved.get("openaiBaseUrl"),
                headers=resolved.get("customHeaders"),
                mode=resolved.get("openaiApiMode"),
                config=resolved,
                request=request,
                requestOptions=(resolved.get("providerProfile") or {}).get("requestOptions") or {},
                runtime=runtime,
                clientFactory=runtime.get("openaiClientFactory") or resolved.get("openaiClientFactory"),
                **timeout_policy,
            )

        return {"send": send_openai}
    if not config.get("apiKey") and not config.get("authToken"):
        return {"send": lambda request: _missing_anthropic_credential_response(config, request)}
    async def send_anthropic(request: dict[str, Any]) -> dict[str, Any]:
        resolved = await resolveProviderConfigSecrets(config)
        return await sendAnthropicMessagesRequest(
            config=resolved,
            request=request,
            runtime=runtime,
            **timeout_policy,
        )

    return {"send": send_anthropic}

def _missing_openai_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required bearer token env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'. Set {env_var}, provide a Codex auth file, or run `tess auth login --openai` for official OpenAI access.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }

def _missing_anthropic_credential_response(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    env_var = (((config.get("providerProfile") or {}).get("auth") or {}).get("envVar")) or "the required Anthropic credential env var"
    return {
        "content": [
            {
                "type": "text",
                "text": f"No credential is configured for provider profile '{config.get('providerProfileName')}'. Set {env_var} or run `tess auth login` to enable model-backed responses.",
            }
        ],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "model": request["model"],
        "stopReason": "end_turn",
    }
