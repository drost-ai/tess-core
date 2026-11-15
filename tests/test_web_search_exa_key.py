"""Exa with a managed key: the reference is resolved at the transport, and a
failure becomes a static reason. Temporary encrypted store, synthetic keys,
a patched urlopen; nothing reaches Exa or a search engine."""
import asyncio
import io
import json
import uuid
from urllib.error import HTTPError, URLError

import pytest

from tess_core.runtime import encrypted_credentials as encrypted
from tess_core.runtime.credentials import CredentialStore
from tess_core.runtime.encrypted_credentials import UnlockSecret
from tess_core.tools import web_search_api
from tess_core.tools.web import FetchResponse, search_web_response

KEY = "exa-synthetic-" + uuid.uuid4().hex


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(encrypted, "SCRYPT", {"name": "scrypt", "n": 2 ** 10, "r": 8, "p": 1})
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TESS_WEB_SEARCH_PROVIDER", raising=False)


@pytest.fixture
def ref(tmp_path):
    store = CredentialStore.encrypted(str(uuid.uuid4()), str(tmp_path / "credentials.enc"))
    store.backend.create(UnlockSecret.passphrase("a synthetic passphrase"))
    made = store.write(str(uuid.uuid4()), "provider_api_key", {"api_key": KEY})
    made["_store"] = store
    return made


def config(ref):
    return {"webSearch": {"provider": "exa", "exaApiKeyRef": {k: v for k, v in ref.items() if k != "_store"}}}


async def no_scraper(url, **_):
    return FetchResponse(200, "")


def answer(monkeypatch, outcome, seen):
    def urlopen(request, timeout=0):
        seen.append({"key": request.get_header("X-api-key"), "body": json.loads(request.data)})
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(json.dumps(outcome).encode())
    monkeypatch.setattr(web_search_api, "urlopen", urlopen)


def test_a_managed_key_is_read_at_the_transport_and_never_returned(ref, monkeypatch):
    seen = []
    answer(monkeypatch, {"results": [{"url": "https://example.com/a", "title": "A", "text": "about a"}]}, seen)
    result = asyncio.run(search_web_response("anything", config=config(ref), fetcher=no_scraper))
    assert (result["provider"], result["fallback_reason"], seen[0]["key"]) == ("exa", None, KEY)
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("failure, code", [
    (HTTPError("https://api.exa.ai/search", 401, "Unauthorized", {}, None), "exa_unauthorized"),
    (HTTPError("https://api.exa.ai/search", 402, "Payment Required", {}, None), "exa_quota"),
    (HTTPError("https://api.exa.ai/search", 429, "Too Many Requests", {}, None), "exa_rate_limited"),
    (URLError(TimeoutError("timed out")), "exa_timeout"),
    (HTTPError("https://api.exa.ai/search", 503, "Unavailable", {}, None), "exa_unavailable"),
])
def test_an_exa_failure_is_a_static_reason_and_the_search_says_so(ref, monkeypatch, failure, code):
    answer(monkeypatch, failure, [])
    result = asyncio.run(search_web_response("anything", config=config(ref), fetcher=no_scraper))
    assert result["provider"] == "scraper" and result["fallback_reason"].split(";")[0] == code
    assert KEY not in json.dumps(result)
    assert asyncio.run(web_search_api.check_exa_connection(config(ref))) == {"state": "unavailable", "code": code}


def test_a_missing_or_locked_key_is_said_plainly(ref, monkeypatch):
    answer(monkeypatch, AssertionError("Exa must not be called"), [])
    missing = asyncio.run(search_web_response("anything", config={"webSearch": {"provider": "exa"}}, fetcher=no_scraper))
    assert missing["fallback_reason"].startswith("exa_key_missing")
    ref["_store"].backend.lock()
    locked = asyncio.run(search_web_response("anything", config=config(ref), fetcher=no_scraper))
    assert locked["fallback_reason"].startswith("exa_key_unavailable")
    assert asyncio.run(web_search_api.check_exa_connection({"webSearch": {}})) == {"state": "unavailable", "code": "exa_key_missing"}


def test_the_connection_check_spends_one_single_result_search(ref, monkeypatch):
    seen = []
    answer(monkeypatch, {"results": []}, seen)
    assert asyncio.run(web_search_api.check_exa_connection(config(ref))) == {"state": "available", "code": "exa_answered"}
    assert len(seen) == 1 and seen[0]["body"]["numResults"] == 1 and seen[0]["key"] == KEY
