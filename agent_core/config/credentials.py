"""Provider credential storage and resolution helpers."""
from __future__ import annotations

import os
import re
from typing import Any

def _looks_like_claude_ai_token(value: Any) -> bool:
    return bool(isinstance(value, str) and re.match(r"^sk-ant-o", value, re.I))

def _same_secret(left: Any, right: Any) -> bool:
    if left is right or left == right:
        return True
    return bool(
        isinstance(left, dict)
        and isinstance(right, dict)
        and left.get("id") == right.get("id")
        and left.get("purpose") == right.get("purpose")
    )


def _get_stored_anthropic_auth_token(auth: dict[str, Any] | None) -> Any:
    if not auth:
        return None
    api_key = auth.get("apiKey")
    return (
        auth.get("anthropicAuthToken")
        or auth.get("anthropicSetupToken")
        or auth.get("authToken")
        or (
            api_key
            if auth.get("provider") == "claudeai"
            and (not isinstance(api_key, str) or _looks_like_claude_ai_token(api_key))
            else None
        )
    )

def _get_stored_anthropic_api_key(auth: dict[str, Any] | None) -> Any:
    if not auth:
        return None
    token = _get_stored_anthropic_auth_token(auth)
    api_key = auth.get("apiKey")
    return auth.get("anthropicApiKey") or (api_key if api_key and not _same_secret(api_key, token) else None)

def _resolve_provider_credential_env_value(env_var: str | None, settings_env: dict[str, str]) -> str | None:
    if not env_var:
        return None
    return os.environ.get(env_var) or settings_env.get(env_var)

def _resolve_openai_credential(args: dict[str, Any]) -> Any:
    profile = args["profile"]
    if profile.get("kind") != "openai":
        return None
    env_var = profile.get("auth", {}).get("envVar") or "OPENAI_API_KEY"
    env_token = _resolve_provider_credential_env_value(env_var, args["settingsEnv"])
    kind = profile.get("auth", {}).get("kind")
    if kind == "custom_bearer":
        return env_token
    if kind == "openai_api_key":
        return env_token or (args.get("storedToken") or args.get("envDefaultToken") if env_var == "OPENAI_API_KEY" else None)
    if kind == "openai_api_key_or_codex":
        return args.get("codexToken") or env_token or (args.get("storedToken") or args.get("envDefaultToken") if env_var == "OPENAI_API_KEY" else None)
    return env_token or (args.get("storedToken") or args.get("envDefaultToken") if env_var == "OPENAI_API_KEY" else None) or args.get("codexToken")

def _resolve_anthropic_credentials(args: dict[str, Any]) -> dict[str, Any]:
    profile = args["profile"]
    if profile.get("kind") != "anthropic":
        return {}
    env_var = profile.get("auth", {}).get("envVar")
    if profile.get("auth", {}).get("kind") == "anthropic_auth_token":
        token = _resolve_provider_credential_env_value(env_var, args["settingsEnv"]) or args.get("explicitAuthToken") or (args.get("storedAuthToken") if env_var in ("ANTHROPIC_AUTH_TOKEN", None) else None)
        return {"authToken": token}
    api_key = _resolve_provider_credential_env_value(env_var, args["settingsEnv"]) or args.get("explicitApiKey") or (args.get("storedApiKey") if env_var in ("ANTHROPIC_API_KEY", None) else None)
    return {"apiKey": api_key}
