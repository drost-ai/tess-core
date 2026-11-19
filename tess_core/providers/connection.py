"""Whether a provider profile can be reached with its own credential.

The check lists models (GET /models): free on OpenAI, OpenAI-compatible
servers and Anthropic, so it never costs a completion. The credential is
resolved here, at the engine's transport boundary; the caller gets a state,
a short code and model ids, never provider text or the credential. A server
without a model list reports manual model entry rather than failure.
Profiles that authenticate through another application's auth file (Codex)
are not probed: that token is the other application's to use.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

from ..config.registry import ProviderRegistry, ProviderRegistryError
from ..runtime.secrets import RuntimeSecretError, is_runtime_secret_ref, resolve_runtime_secret
from .anthropic_async_transport import AnthropicAsyncHttpConnectionOwner

ANTHROPIC_VERSION = "2023-06-01"
# A ChatGPT account's catalogue carries every model's instructions and runs to
# about a megabyte; the limit leaves room for it to grow.
MAX_BODY = 8 * 1024 * 1024
MAX_MODELS = 200


def _result(state: str, code: str, models: list[str] | None = None) -> dict[str, Any]:
    return {"state": state, "code": code, "models": models or []}


async def _secret(value: Any) -> str | None:
    if value is None:
        return None
    if is_runtime_secret_ref(value):
        purpose = value.get("purpose") if value.get("purpose") in ("provider_api_key", "provider_oauth_token") else "provider_api_key"
        return bytes(await resolve_runtime_secret(value, purpose)).decode()
    return str(value)


def _target(profile: dict[str, Any]) -> str:
    if profile["kind"] == "anthropic":
        return (profile.get("baseUrl") or "https://api.anthropic.com").rstrip("/") + "/v1/models"
    return (profile.get("baseUrl") or "https://api.openai.com/v1").rstrip("/") + "/models"


async def check_provider_connection(registry: ProviderRegistry, name: str, *, timeout: float = 10.0,
                                    transport: Any = AnthropicAsyncHttpConnectionOwner) -> dict[str, Any]:
    """``{"state": available|unsupported|failed|not_tested, "code": ..., "models": [...]}``.
    ``transport`` builds the HTTP owner (a test seam; the default is the engine's)."""
    try:
        profile = registry.profiles[name]
        if profile.get("apiMode") == "codex" or profile["auth"]["kind"] == "openai_api_key_or_codex":
            return _result("not_tested", "external_auth_file")
        if profile["kind"] not in ("anthropic", "openai"):
            return _result("not_tested", "unsupported_provider_kind")
        auth = registry.resolve_auth(name)
        headers = {"Accept": "application/json", **{str(k): str(v) for k, v in (auth.get("customHeaders") or {}).items()}}
        if profile["kind"] == "anthropic":
            headers["anthropic-version"] = ANTHROPIC_VERSION
            if auth.get("apiKey") is not None:
                headers["x-api-key"] = await _secret(auth["apiKey"])
            elif auth.get("authToken") is not None:
                headers["Authorization"] = f"Bearer {await _secret(auth['authToken'])}"
        else:
            token = await _secret(auth.get("apiKey") if auth.get("apiKey") is not None else auth.get("openaiToken") or auth.get("authToken"))
            if token:
                headers["Authorization"] = f"Bearer {token}"
        url = _target(profile)
    except (KeyError, TypeError, ProviderRegistryError):
        return _result("failed", "invalid_profile")
    except RuntimeSecretError as error:
        return _result("failed", error.code if error.code.startswith("credential_") else "credential_unavailable")
    except Exception as error:  # noqa: BLE001 (a ChatGPT account that could not be refreshed)
        from .chatgpt_account import ChatGPTAccountError
        if not isinstance(error, ChatGPTAccountError):
            raise
        return _result("failed", "reauthorization_required" if error.terminal else "credential_unavailable")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return _result("failed", "invalid_profile")
    owner = transport()
    try:
        response = await owner.start(url=url, body=b"", headers=headers, timeout_seconds=timeout, method="GET")
        status = int(response.status)
        body = await asyncio.wait_for(response.read_limited(MAX_BODY), timeout)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return _result("failed", "timeout")
    except ValueError:
        # The provider answered, with more than this check reads: not a network failure.
        return _result("failed", "invalid_provider_response")
    except Exception:
        return _result("failed", "connection_unavailable")
    finally:
        owner.close()
    if status in {401, 403}:
        return _result("failed", "authentication_denied")
    if status == 429:
        return _result("failed", "rate_limited")
    if status in {404, 405, 501}:
        return _result("unsupported", "manual_model_required")
    if 300 <= status < 400:
        return _result("failed", "unexpected_redirect")
    if not 200 <= status < 300:
        return _result("failed", "invalid_provider_response")
    try:
        listed = json.loads(body)
        if isinstance(listed.get("models"), list):
            # A ChatGPT account's catalog: slugs the account may pick, in the server's order.
            models = [str(item["slug"]) for item in listed["models"] if isinstance(item, dict)
                      and isinstance(item.get("slug"), str) and item.get("visibility") == "list"][:MAX_MODELS]
        else:
            items = listed.get("data")
            models = [str(item["id"]) for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)][:MAX_MODELS]
    except (ValueError, TypeError, AttributeError, KeyError):
        return _result("failed", "invalid_provider_response")
    return _result("available", "models_listed", models)

