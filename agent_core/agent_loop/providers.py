from __future__ import annotations

from .shared import *

def _create_provider(config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    if config.get("provider") == "openai":
        if not config.get("openaiToken"):
            return {"send": lambda request: _missing_openai_credential_response(config, request)}
        return {
            "send": lambda request: sendOpenAiRequest(
                token=config["openaiToken"],
                baseUrl=config.get("openaiBaseUrl"),
                headers=config.get("customHeaders"),
                mode=config.get("openaiApiMode"),
                request=request,
                runtime=runtime,
                clientFactory=runtime.get("openaiClientFactory") or config.get("openaiClientFactory"),
            )
        }
    if not config.get("apiKey") and not config.get("authToken"):
        return {"send": lambda request: _missing_anthropic_credential_response(config, request)}
    return {"send": lambda request: sendAnthropicMessagesRequest(config=config, request=request, runtime=runtime)}

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
