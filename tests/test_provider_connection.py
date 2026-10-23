"""Provider connection check: synthetic registry and transport, no sockets."""
import asyncio
import json
import uuid

import pytest

from agent_core.config import ProviderRegistry
from agent_core.providers.connection import check_provider_connection
from agent_core.runtime.credentials import CredentialStore


class Backend:
    def __init__(self):
        self.records = {}

    def get_password(self, service, username):
        return self.records.get((service, username))

    def set_password(self, service, username, password):
        self.records[service, username] = password

    def delete_password(self, service, username):
        self.records.pop((service, username), None)


class Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def read_limited(self, limit):
        if len(self._body) > limit:
            raise ValueError("too large")
        return self._body


def transport(status=200, payload=b'{"data": [{"id": "model-a"}, {"id": "model-b"}, {"nope": 1}]}', error=None, seen=None):
    class Owner:
        async def start(self, *, url, body, headers, timeout_seconds, method):
            if seen is not None:
                seen.update(url=url, headers=headers, method=method, body=body)
            if error:
                raise error
            return Response(status, payload)

        def close(self):
            pass
    return Owner


def registry(tmp_path, monkeypatch, auth, **extra):
    profile = {"kind": "openai", "defaultModel": "m", "auth": auth, **extra}
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"profiles": {"p": profile}}))
    return ProviderRegistry(path)


def test_lists_models_with_a_managed_key_resolved_inside_the_engine(tmp_path, monkeypatch):
    store = CredentialStore(str(uuid.uuid4()), "macos_keychain", Backend())
    ref = store.write(str(uuid.uuid4()), "provider_api_key", {"api_key": "synthetic-key"})
    monkeypatch.setattr(CredentialStore, "desktop", classmethod(lambda cls, installation, name: store))
    seen = {}
    result = asyncio.run(check_provider_connection(
        registry(tmp_path, monkeypatch, {"kind": "openai_api_key", "credentialRef": ref}, baseUrl="https://compatible.invalid/v1"),
        "p", transport=transport(seen=seen)))
    assert result == {"state": "available", "code": "models_listed", "models": ["model-a", "model-b"]}
    assert (seen["url"], seen["method"], seen["body"]) == ("https://compatible.invalid/v1/models", "GET", b"")
    assert seen["headers"]["Authorization"] == "Bearer synthetic-key"
    assert "synthetic-key" not in json.dumps(result)


def test_anthropic_uses_its_own_header_and_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_ANTHROPIC", "synthetic-anthropic")
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"profiles": {"p": {"kind": "anthropic", "defaultModel": "m",
                                                   "auth": {"kind": "anthropic_api_key", "envVar": "SYNTHETIC_ANTHROPIC"}}}}))
    seen = {}
    result = asyncio.run(check_provider_connection(ProviderRegistry(path), "p", transport=transport(seen=seen)))
    assert result["state"] == "available"
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert (seen["headers"]["x-api-key"], seen["headers"]["anthropic-version"]) == ("synthetic-anthropic", "2023-06-01")


@pytest.mark.parametrize("status,body,expected", [
    (401, b"{}", ("failed", "authentication_denied")),
    (429, b"{}", ("failed", "rate_limited")),
    (404, b"", ("unsupported", "manual_model_required")),
    (302, b"", ("failed", "unexpected_redirect")),
    (500, b"{}", ("failed", "invalid_provider_response")),
    (200, b"not json", ("failed", "invalid_provider_response")),
    (200, b"x" * (600 * 1024), ("failed", "connection_unavailable")),
])
def test_outcomes_are_codes_not_provider_text(tmp_path, monkeypatch, status, body, expected):
    result = asyncio.run(check_provider_connection(
        registry(tmp_path, monkeypatch, {"kind": "none"}, baseUrl="http://127.0.0.1:9/v1"), "p",
        transport=transport(status=status, payload=body)))
    assert (result["state"], result["code"]) == expected and result["models"] == []


def test_unreachable_timeouts_and_borrowed_auth_files(tmp_path, monkeypatch):
    plain = registry(tmp_path, monkeypatch, {"kind": "none"}, baseUrl="http://127.0.0.1:9/v1")
    assert asyncio.run(check_provider_connection(plain, "p", transport=transport(error=ConnectionRefusedError())))["code"] == "connection_unavailable"
    assert asyncio.run(check_provider_connection(plain, "p", transport=transport(error=asyncio.TimeoutError())))["code"] == "timeout"
    codex = registry(tmp_path, monkeypatch, {"kind": "openai_api_key_or_codex"}, apiMode="codex")
    assert asyncio.run(check_provider_connection(codex, "p", transport=transport(error=AssertionError("never called"))))["state"] == "not_tested"
    missing = registry(tmp_path, monkeypatch, {"kind": "openai_api_key", "envVar": "SYNTHETIC_UNSET_KEY"})
    monkeypatch.delenv("SYNTHETIC_UNSET_KEY", raising=False)
    assert asyncio.run(check_provider_connection(missing, "p", transport=transport()))["code"] == "invalid_profile"
