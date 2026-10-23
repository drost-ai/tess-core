"""Explicit, fail-closed provider profiles for embedders and memory clients.

No settings discovery, auth files, credential helpers or provider network I/O.
"""
from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._yaml import parse_yaml_subset
from .providers import resolve_provider_profile


class ProviderRegistryError(ValueError):
    """A redacted registry/profile/credential configuration error."""


class ProviderRegistry:
    """Load a YAML providerProfiles (or profiles) mapping from an explicit path.

    Credentials come from explicit env references or managed credentialRef. Returned
    dictionaries are independent copies. Credential values must not be logged.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ProviderRegistryError("Cannot read explicit provider registry; check path and permissions") from None
        try:
            if self.path.suffix.lower() == ".json":
                import json
                def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                    result: dict[str, Any] = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate registry key")
                        result[key] = value
                    return result
                document = json.loads(text, object_pairs_hook=unique_pairs)
            else:
                document = parse_yaml_subset(text, source="provider registry")
        except ValueError as exc:
            line = getattr(exc, "line", None)
            suffix = f" at line {line}" if line else ""
            raise ProviderRegistryError(f"Invalid provider registry YAML/JSON{suffix}") from None
        self._load_document(document)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ProviderRegistry:
        """Validate an explicit in-memory registry with the SAME schema authority.

        No file writes/discovery/auth/backend/provider I/O; used by staged
        management edits before safe runtime-owned publication.
        """
        instance = cls.__new__(cls)
        instance.path = None
        instance._load_document(copy.deepcopy(document))
        return instance

    def _load_document(self, document: Mapping[str, Any]) -> None:
        if not isinstance(document, dict):
            raise ProviderRegistryError("Provider registry must contain a profiles mapping")
        version = document.get("schema_version", 1)
        if type(version) is not int or version != 1:
            raise ProviderRegistryError("Unsupported provider registry schema_version; expected 1")
        records = document.get("providerProfiles", document.get("profiles"))
        if not isinstance(records, dict) or not records:
            raise ProviderRegistryError("Provider registry requires a nonempty providerProfiles mapping")
        self._profiles: dict[str, dict[str, Any]] = {}
        for name, record in records.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise ProviderRegistryError("Provider profile names must be ASCII identifiers")
            if not isinstance(record, dict) or not isinstance(record.get("kind"), str) or record["kind"] not in {"anthropic", "openai"}:
                raise ProviderRegistryError(f"Profile '{name}' requires kind anthropic or openai")
            self._validate_no_embedded_credentials(record, name)
            auth = record.get("auth")
            if not isinstance(auth, dict):
                raise ProviderRegistryError(f"Profile '{name}' requires explicit auth configuration")
            if set(auth) - {"kind", "envVar", "credentialRef"}:
                raise ProviderRegistryError(f"Profile '{name}' auth accepts only kind, envVar and credentialRef")
            if "credentialRef" in auth:
                from ..runtime.credentials import CredentialStoreError, _uuid
                from ..runtime.secrets import is_runtime_secret_ref
                ref = auth["credentialRef"]
                try:
                    # A ChatGPT account is an OAuth bundle; every other managed credential is an API key.
                    purpose = 'provider_oauth_token' if auth.get('kind') == 'chatgpt' else 'provider_api_key'
                    if (auth.get('kind') not in {'openai_api_key', 'anthropic_api_key', 'chatgpt'}
                            or not is_runtime_secret_ref(ref) or ref.get('purpose') != purpose
                            or ref.get('backend') not in {'macos_keychain', 'linux_secret_service', 'encrypted_file'}
                            or set(ref) != {'kind', 'version', 'id', 'purpose', 'backend', 'installation_id', 'account_id'}
                            or auth.get('envVar') is not None):
                        raise ValueError()
                    _uuid(ref['installation_id'])
                    _uuid(ref['account_id'])
                except (ValueError, KeyError, TypeError, CredentialStoreError):
                    raise ProviderRegistryError('Invalid managed credential reference') from None
            supported = {"anthropic_api_key", "anthropic_auth_token", "custom_bearer", "none"} if record["kind"] == "anthropic" else {"openai_api_key", "openai_api_key_or_codex", "custom_bearer", "none", "chatgpt"}
            if not isinstance(auth.get("kind"), str) or auth["kind"] not in supported:
                raise ProviderRegistryError(f"Profile '{name}' has unsupported auth kind")
            if auth.get("envVar") is not None and (not isinstance(auth["envVar"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth["envVar"])):
                raise ProviderRegistryError(f"Profile '{name}' has invalid auth.envVar")
            if auth["kind"] not in {"none", "openai_api_key_or_codex"} and "credentialRef" not in auth and (not isinstance(auth.get("envVar"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth["envVar"])):
                raise ProviderRegistryError(f"Profile '{name}' requires auth.envVar")
            if auth["kind"] == "chatgpt":
                self._validate_chatgpt_plan(record, name)
            if not isinstance(record.get("defaultModel"), str) or not record["defaultModel"].strip():
                raise ProviderRegistryError(f"Profile '{name}' requires defaultModel")
            if not isinstance(record.get("headers", {}), dict):
                raise ProviderRegistryError(f"Profile '{name}' headers must be a mapping")
            self._profiles[name] = copy.deepcopy(record)
            # Validate profile normalization without reflecting raw values in errors.
            self.resolve_profile(name)

    @staticmethod
    def _validate_chatgpt_plan(record: dict[str, Any], name: str) -> None:
        """ChatGPT plan use is the public Responses API with full history over
        SSE: no other endpoint, API mode, WebSocket or incremental chaining."""
        capabilities = record.get("capabilities") if isinstance(record.get("capabilities"), dict) else {}
        if ("credentialRef" not in record["auth"] or record.get("apiMode") not in (None, "responses")
                or (record.get("baseUrl") or "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1"
                or any(capabilities.get(key) for key in ("openaiResponsesWebSocket", "openaiResponsesIncremental", "openaiTurnState"))):
            raise ProviderRegistryError(f"Profile '{name}' uses a ChatGPT account: the public Responses API with a managed account only")

    @staticmethod
    def _validate_no_embedded_credentials(record: dict[str, Any], name: str) -> None:
        credential_keys = {"apikey", "authtoken", "openaitoken", "accesstoken", "refreshtoken", "password", "secret", "anthropicapikey", "anthropicauthtoken"}
        sensitive_headers = {"authorization", "proxyauthorization", "xapikey", "apikey", "cookie", "setcookie"}
        def normalized(key: Any) -> str:
            return re.sub(r"[^a-z0-9]", "", str(key).lower())
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if normalized(key) in credential_keys:
                        raise ProviderRegistryError(f"Profile '{name}' cannot embed credentials; use auth.envVar")
                    if normalized(key) == "headers" and isinstance(child, dict):
                        if any(normalized(header) in sensitive_headers for header in child):
                            raise ProviderRegistryError(f"Profile '{name}' cannot embed credential headers; use auth.envVar")
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(record)

    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        """Raw profile copies, without built-ins or resolved credentials."""
        return copy.deepcopy(self._profiles)

    def _record(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise ProviderRegistryError("An explicit provider profile name is required")
        if name not in self._profiles:
            # Do not echo arbitrary caller input (it may accidentally be a secret).
            raise ProviderRegistryError("Unknown provider profile in explicit registry; check configured role profile")
        return self._profiles[name]

    def resolve_profile(self, name: str, *, model: str | None = None) -> dict[str, Any]:
        record = self._record(name)
        try:
            return resolve_provider_profile(name, copy.deepcopy(record), {"model": model})
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderRegistryError(f"Invalid configuration for provider profile '{name}'") from None

    def resolve_auth(self, name: str, *, environment: Mapping[str, str] | None = None, codex_auth_path: str | Path | None = None) -> dict[str, Any]:
        """Return transport config fields, reading only the declared envVar.

        Pass an explicit environment (including {}) for deterministic resolution.
        Anthropic API keys and bearer tokens retain their distinct transports.
        Codex auth is an explicitly supplied token, never ambient auth-file lookup.
        """
        record = self._record(name)
        auth = record["auth"]
        headers = copy.deepcopy(record.get("headers") or {})
        if codex_auth_path is not None:
            if record["kind"] != "openai":
                raise ProviderRegistryError("Explicit Codex auth file requires an OpenAI profile")
            return {"openaiToken": _read_codex_auth(codex_auth_path), "customHeaders": headers}
        if auth["kind"] == "none":
            return {"customHeaders": headers}
        env = os.environ if environment is None else environment
        if 'credentialRef' in auth:
            if auth['kind'] == 'chatgpt':
                return {'openaiToken': copy.deepcopy(auth['credentialRef']), 'customHeaders': headers}
            if auth['kind'] not in {'openai_api_key', 'anthropic_api_key'}:
                raise ProviderRegistryError('Managed credentials require API-key authentication')
            return {'apiKey': copy.deepcopy(auth['credentialRef']), 'customHeaders': headers}
        token = env.get(auth.get("envVar")) if auth.get("envVar") else None
        if not isinstance(token, str) or not token.strip():
            raise ProviderRegistryError(f"Missing credential for profile '{name}'; set its declared auth.envVar")
        if record["kind"] == "openai":
            return {"openaiToken": token, "customHeaders": headers}
        key = "authToken" if auth["kind"] in {"anthropic_auth_token", "custom_bearer"} else "apiKey"
        return {key: token, "customHeaders": headers}


async def resolve_codex_auth(path: str | Path) -> str:
    """Read only an explicit Codex auth JSON file; no home discovery or refresh.

    Returns OPENAI_API_KEY or tokens.access_token, preserving the existing
    file token precedence. Fails closed without printing path or file contents.
    """
    import asyncio
    return await asyncio.to_thread(_read_codex_auth, path)


def _read_codex_auth(path: str | Path) -> str:
    import json
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        token = payload.get("OPENAI_API_KEY") or (payload.get("tokens") or {}).get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raise ProviderRegistryError("Cannot resolve credential from explicit Codex auth file")


async def resolve_provider_secret(
    value: Any, *, purpose: str = "provider_api_key", idempotency_scope: str = "provider-auth"
) -> dict[str, Any] | None:
    """Normalize a supplied credential or secret reference in the scoped runtime.

    Does not discover credentials. OAuth/setup-token references retain their
    purpose and account pinning; returned references are not plaintext tokens.
    """
    from ..runtime.secret_provider import normalize_runtime_secret_value

    accepted = ("provider_oauth_token", "provider_setup_token") if purpose == "provider_oauth_token" else (purpose,)
    return await normalize_runtime_secret_value(
        value=value, purpose=purpose, idempotencyScope=idempotency_scope,
        acceptedPurposes=accepted,
    )
