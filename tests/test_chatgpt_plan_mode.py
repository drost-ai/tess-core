"""A ChatGPT account as a provider profile: the registry admits it only on
the public Responses API, requests drop what plan use refuses, a refused
token is refreshed once, and the connection check reads the account's own
model catalog. Synthetic refs, fakes for every transport."""
import asyncio
import json
import uuid

import pytest

from tess_core.agent import providers as agent_providers
from tess_core.config.registry import ProviderRegistry, ProviderRegistryError
from tess_core.providers import chatgpt_account, connection
from tess_core.providers.errors import ProviderRequestError
from tess_core.providers.openai.params import build_responses_create_params


def oauth_ref(purpose="provider_oauth_token"):
    return {"kind": "runtime_secret", "version": 1, "id": str(uuid.uuid4()), "purpose": purpose,
            "backend": "encrypted_file", "installation_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4())}


def registry(**overrides):
    record = {"kind": "openai", "apiMode": "responses", "defaultModel": "gpt-synthetic",
              "auth": {"kind": "chatgpt", "credentialRef": oauth_ref()}, **overrides}
    return ProviderRegistry.from_document({"profiles": {"chatgpt": record}})


def test_a_chatgpt_account_profile_is_the_public_responses_api_with_a_managed_bundle():
    ref = oauth_ref()
    resolved = registry(auth={"kind": "chatgpt", "credentialRef": ref}).resolve_auth("chatgpt")
    assert resolved == {"openaiToken": ref, "customHeaders": {}}
    assert registry(baseUrl="https://api.openai.com/v1/").profiles["chatgpt"]["auth"]["kind"] == "chatgpt"
    refused = [
        {"auth": {"kind": "chatgpt", "credentialRef": oauth_ref("provider_api_key")}},
        {"auth": {"kind": "chatgpt", "envVar": "OPENAI_API_KEY"}},
        {"apiMode": "codex"},
        {"apiMode": "chat_completions"},
        {"baseUrl": "https://chatgpt.com/backend-api/codex"},
        {"capabilities": {"openaiResponsesWebSocket": True}},
        {"capabilities": {"openaiResponsesIncremental": True}},
    ]
    for overrides in refused:
        with pytest.raises(ProviderRegistryError):
            registry(**overrides)
    with pytest.raises(ProviderRegistryError):  # an API-key profile cannot hold an OAuth bundle either
        ProviderRegistry.from_document({"profiles": {"p": {"kind": "openai", "defaultModel": "m",
                                                           "auth": {"kind": "openai_api_key", "credentialRef": oauth_ref()}}}})


def test_plan_requests_drop_what_plan_use_refuses():
    request = {"model": "gpt-synthetic", "system": "Be Tess.", "messages": [{"role": "user", "content": "hi"}], "maxTokens": 4000}
    options = {"temperature": 0.2, "top_p": 0.9, "metadata": {"a": "b"}, "user": "u", "max_output_tokens": 99,
               "previous_response_id": "resp_1", "extra_body": {"background": True, "metadata": {}}, "prompt_cache_key": "k"}
    plan = build_responses_create_params({"request": request, "chatgptPlan": True, "requestOptions": options})
    assert (plan["store"], plan["stream"], plan["instructions"]) == (False, True, "Be Tess.")
    for key in ("temperature", "top_p", "metadata", "user", "max_output_tokens", "previous_response_id", "extra_body"):
        assert key not in plan
    assert plan["prompt_cache_key"] == "k" and plan["input"][0]["role"] == "user"
    ordinary = build_responses_create_params({"request": request, "requestOptions": {"temperature": 0.2}})
    assert "max_output_tokens" in ordinary and ordinary["temperature"] == 0.2


def test_a_refused_token_is_refreshed_once_and_the_request_retried(monkeypatch):
    ref = oauth_ref()
    tokens = iter(["at-old", "at-new"])
    sent, forced = [], []

    async def resolve(config):
        return {**config, "openaiToken": next(tokens)}

    async def send_openai_request(**kwargs):
        sent.append(kwargs["token"])
        if kwargs["token"] == "at-old":
            raise ProviderRequestError({"message": "unauthorized", "status": 401, "hadAssistantOutput": False})
        return {"content": [], "stopReason": "end_turn"}

    async def force(account, *, force, stale):
        forced.append((account["id"], force, stale))
        return "at-new"
    monkeypatch.setattr(agent_providers, "resolve_provider_config_secrets", resolve)
    monkeypatch.setattr(agent_providers, "send_openai_request", send_openai_request)
    monkeypatch.setattr(chatgpt_account, "current_access_token", force)
    config = {"provider": "openai", "openaiToken": ref, "providerProfile": {"auth": {"kind": "chatgpt"}}, "sessionPersistence": False}
    provider = agent_providers._create_provider(config, {}, {"metadata": {}})
    assert asyncio.run(provider["send"]({"model": "m"}))["stopReason"] == "end_turn"
    assert sent == ["at-old", "at-new"] and forced == [(ref["id"], True, "at-old")]

    tokens = iter(["at-old", "at-old"])
    api_key = {"provider": "openai", "openaiToken": "sk-synthetic", "providerProfile": {"auth": {"kind": "openai_api_key"}}, "sessionPersistence": False}
    with pytest.raises(ProviderRequestError):  # an API key is not refreshed: the refusal stands
        asyncio.run(agent_providers._create_provider(api_key, {}, {"metadata": {}})["send"]({"model": "m"}))


class Owner:
    def __init__(self, status, body):
        self.status, self.body, self.seen = status, body, {}

    def __call__(self):
        return self

    async def start(self, *, url, body, headers, timeout_seconds, method):
        self.seen = {"url": url, "authorization": headers.get("Authorization"), "method": method}
        return self

    async def read_limited(self, limit):
        return self.body

    def close(self):
        pass


def test_the_connection_check_lists_the_accounts_own_catalog(monkeypatch):
    async def token(ref, **kwargs):
        return "at-live"
    monkeypatch.setattr(chatgpt_account, "current_access_token", token)
    catalog = {"models": [{"slug": "gpt-a", "display_name": "A", "visibility": "list"},
                          {"slug": "gpt-hidden", "display_name": "H", "visibility": "hide"},
                          {"slug": "gpt-b", "display_name": "B", "visibility": "list"}]}
    owner = Owner(200, json.dumps(catalog).encode())
    result = asyncio.run(connection.check_provider_connection(registry(), "chatgpt", transport=owner))
    assert result == {"state": "available", "code": "models_listed", "models": ["gpt-a", "gpt-b"]}
    assert owner.seen == {"url": "https://api.openai.com/v1/models", "authorization": "Bearer at-live", "method": "GET"}

    async def expired(ref, **kwargs):
        raise chatgpt_account.ChatGPTAccountError("reauthorization_required", terminal=True)
    monkeypatch.setattr(chatgpt_account, "current_access_token", expired)
    assert asyncio.run(connection.check_provider_connection(registry(), "chatgpt", transport=owner))["code"] == "reauthorization_required"
