from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Iterable, Mapping

from .secrets import (
    RuntimeSecretError,
    authorizeRuntimeSecretWrite,
    isRuntimeSecretRef,
    resolveRuntimeSecretString,
    storeRuntimeSecret,
)


_PROVIDER_SECRET_KEY_PATTERN = re.compile(
    r"(?:authorization|cookie|token|secret|password|api[-_]?key|credential)",
    re.I,
)
_PROVIDER_SECRET_PURPOSES = (
    "provider_api_key",
    "provider_oauth_token",
    "provider_setup_token",
)


def _assert_purpose(ref: Mapping[str, Any], allowed: Iterable[str]) -> None:
    if ref.get("purpose") not in set(allowed):
        raise RuntimeSecretError("secret_store_invalid", "reference purpose or shape is invalid")


def _provider_header_purpose(key: str) -> str:
    return "provider_api_key" if re.search(r"api[-_]?key", key, re.I) else "provider_oauth_token"


async def normalizeRuntimeSecretValue(
    args: Mapping[str, Any] | None = None,
    *,
    value: Any = None,
    purpose: str | None = None,
    idempotencyScope: str | None = None,
    acceptedPurposes: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    values = dict(args or {})
    selected_value = values.get("value", value)
    selected_purpose = values.get("purpose", purpose)
    scope = values.get("idempotencyScope", idempotencyScope)
    accepted = tuple(values.get("acceptedPurposes", acceptedPurposes) or (selected_purpose,))
    if selected_value is None or selected_value == "":
        return None
    if isRuntimeSecretRef(selected_value):
        _assert_purpose(selected_value, accepted)
        return copy.deepcopy(selected_value)
    if not isinstance(selected_value, str) or selected_purpose not in _PROVIDER_SECRET_PURPOSES:
        raise RuntimeSecretError("secret_store_invalid", "provider credential has an invalid value or purpose")
    if not isinstance(scope, str) or not scope:
        raise RuntimeSecretError("secret_store_invalid", "provider credential is missing its idempotency scope")
    digest = hashlib.sha256(selected_value.encode("utf-8")).hexdigest()
    idempotency_key = f"{scope}:{digest}"
    secret = bytearray(selected_value.encode("utf-8"))
    try:
        return await storeRuntimeSecret(
            purpose=selected_purpose,
            secret=secret,
            idempotencyKey=idempotency_key,
            authorization=authorizeRuntimeSecretWrite(
                purpose=selected_purpose,
                idempotencyKey=idempotency_key,
            ),
        )
    finally:
        secret[:] = b"\0" * len(secret)


async def normalizeProviderHeaders(
    headers: Mapping[str, Any] | None,
    idempotencyScope: str,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in (headers or {}).items():
        if not _PROVIDER_SECRET_KEY_PATTERN.search(str(key)):
            normalized[str(key)] = copy.deepcopy(value)
            continue
        selected = await normalizeRuntimeSecretValue(
            value=value,
            purpose=_provider_header_purpose(str(key)),
            idempotencyScope=f"{idempotencyScope}:header:{key}",
            acceptedPurposes=_PROVIDER_SECRET_PURPOSES,
        )
        if selected is not None:
            normalized[str(key)] = selected
    return normalized


async def normalizeCompactionProviderSecrets(
    binding: Mapping[str, Any] | None,
    idempotencyScope: str,
) -> dict[str, Any] | None:
    if binding is None:
        return None
    result = copy.deepcopy(dict(binding))
    result["customHeaders"] = await normalizeProviderHeaders(
        binding.get("customHeaders") if isinstance(binding.get("customHeaders"), Mapping) else {},
        idempotencyScope,
    )
    profile = binding.get("profile") if isinstance(binding.get("profile"), Mapping) else {}
    if profile.get("kind") == "openai":
        result["openaiToken"] = await normalizeRuntimeSecretValue(
            value=binding.get("openaiToken"),
            purpose="provider_oauth_token" if profile.get("apiMode") == "codex" else "provider_api_key",
            acceptedPurposes=("provider_api_key", "provider_oauth_token"),
            idempotencyScope=f"{idempotencyScope}:openai",
        )
        return result
    result["apiKey"] = await normalizeRuntimeSecretValue(
        value=binding.get("apiKey"),
        purpose="provider_api_key",
        idempotencyScope=f"{idempotencyScope}:anthropic-api-key",
    )
    result["authToken"] = await normalizeRuntimeSecretValue(
        value=binding.get("authToken"),
        purpose="provider_oauth_token",
        acceptedPurposes=("provider_oauth_token", "provider_setup_token"),
        idempotencyScope=f"{idempotencyScope}:anthropic-auth-token",
    )
    return result


async def _resolve_provider_value(value: Any, allowed_purposes: Iterable[str]) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if not isRuntimeSecretRef(value):
        raise RuntimeSecretError("secret_store_invalid", "reference purpose or shape is invalid")
    _assert_purpose(value, allowed_purposes)
    return await resolveRuntimeSecretString(value, value["purpose"])


async def resolveCompactionProviderSecrets(binding: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(binding))
    result["openaiToken"] = await _resolve_provider_value(
        binding.get("openaiToken"),
        ("provider_api_key", "provider_oauth_token"),
    )
    result["apiKey"] = await _resolve_provider_value(binding.get("apiKey"), ("provider_api_key",))
    result["authToken"] = await _resolve_provider_value(
        binding.get("authToken"),
        ("provider_oauth_token", "provider_setup_token"),
    )
    result["customHeaders"] = {
        str(key): await _resolve_provider_value(value, _PROVIDER_SECRET_PURPOSES)
        for key, value in (binding.get("customHeaders") or {}).items()
    }
    return result


async def resolveProviderConfigSecrets(config: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(config))
    result["apiKey"] = await _resolve_provider_value(config.get("apiKey"), ("provider_api_key",))
    result["authToken"] = await _resolve_provider_value(
        config.get("authToken"),
        ("provider_oauth_token", "provider_setup_token"),
    )
    result["openaiToken"] = await _resolve_provider_value(
        config.get("openaiToken"),
        ("provider_api_key", "provider_oauth_token"),
    )
    result["customHeaders"] = {
        str(key): await _resolve_provider_value(value, _PROVIDER_SECRET_PURPOSES)
        for key, value in (config.get("customHeaders") or {}).items()
    }
    return result


def assertProviderConfigSecretsResolved(config: Mapping[str, Any]) -> dict[str, Any]:
    values = [
        config.get("apiKey"),
        config.get("authToken"),
        config.get("openaiToken"),
        *(config.get("customHeaders") or {}).values(),
    ]
    if any(value is not None and not isinstance(value, str) for value in values):
        raise RuntimeSecretError(
            "secret_store_invalid",
            "provider credential reached a transport before resolution",
        )
    return dict(config)


async def migrateLegacyStoredAuthSecrets(auth: Mapping[str, Any]) -> dict[str, Any]:
    changed = False
    result = copy.deepcopy(dict(auth))

    async def migrate(field: str, purpose: str, accepted: Iterable[str] | None = None) -> None:
        nonlocal changed
        if field not in result or result[field] is None:
            return
        value = result[field]
        result[field] = await normalizeRuntimeSecretValue(
            value=value,
            purpose=purpose,
            acceptedPurposes=accepted,
            idempotencyScope=f"legacy-provider-auth:{purpose}",
        )
        if isinstance(value, str):
            changed = True

    await migrate("anthropicApiKey", "provider_api_key")
    legacy_api_key_is_oauth = bool(
        auth.get("provider") == "claudeai"
        and isinstance(auth.get("apiKey"), str)
        and re.match(r"^sk-ant-o", auth["apiKey"], re.I)
    )
    await migrate(
        "apiKey",
        "provider_oauth_token" if legacy_api_key_is_oauth else "provider_api_key",
        ("provider_oauth_token", "provider_setup_token") if legacy_api_key_is_oauth else None,
    )
    await migrate(
        "anthropicAuthToken",
        "provider_oauth_token",
        ("provider_oauth_token", "provider_setup_token"),
    )
    await migrate(
        "authToken",
        "provider_oauth_token",
        ("provider_oauth_token", "provider_setup_token"),
    )
    await migrate("anthropicSetupToken", "provider_setup_token")
    await migrate(
        "openaiToken",
        "provider_oauth_token" if auth.get("openaiSource") == "codex-oauth" else "provider_api_key",
        ("provider_api_key", "provider_oauth_token"),
    )
    if isinstance(result.get("customHeaders"), Mapping):
        original = result["customHeaders"]
        result["customHeaders"] = await normalizeProviderHeaders(original, "legacy-provider-auth")
        if any(
            _PROVIDER_SECRET_KEY_PATTERN.search(str(key)) and isinstance(value, str)
            for key, value in original.items()
        ):
            changed = True
    return {"auth": result, "changed": changed}


normalize_runtime_secret_value = normalizeRuntimeSecretValue
normalize_provider_headers = normalizeProviderHeaders
normalize_compaction_provider_secrets = normalizeCompactionProviderSecrets
resolve_compaction_provider_secrets = resolveCompactionProviderSecrets
resolve_provider_config_secrets = resolveProviderConfigSecrets
assert_provider_config_secrets_resolved = assertProviderConfigSecretsResolved
migrate_legacy_stored_auth_secrets = migrateLegacyStoredAuthSecrets
