"""ChatGPT account bundles: kept under one reference, refreshed in place
when due (once, however many callers or processes ask), classified when
refresh fails, and revoked on sign-out. A temporary encrypted store and a
fake token endpoint; nothing reaches OpenAI."""
import asyncio
import json
import threading
import uuid

import pytest

from tess_core.providers import chatgpt_account
from tess_core.providers.chatgpt_account import OPENAI, ChatGPTAccountError, Grant, current_access_token, revoke, store_grant
from tess_core.runtime import encrypted_credentials as encrypted
from tess_core.runtime.credentials import CredentialStore, CredentialStoreError
from tess_core.runtime.encrypted_credentials import UnlockSecret
from tess_core.runtime.secrets import resolve_runtime_secret
from tess_core.storage.paths import scoped_runtime_home

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(encrypted, "SCRYPT", {"name": "scrypt", "n": 2 ** 10, "r": 8, "p": 1})


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class TokenEndpoint:
    """Rotates the refresh token on every refresh, as OpenAI does."""

    def __init__(self, *, answers=None, delay=0.0):
        self.answers = list(answers or [])
        self.refreshes, self.revocations, self.delay, self.issued = [], [], delay, 1
        self.lock = threading.Lock()

    async def __call__(self, method, url, form):
        if url == OPENAI.revoke:
            self.revocations.append(dict(form))
            answer = self.answers.pop(0) if self.answers else 200
            if isinstance(answer, Exception):
                raise answer
            return answer, b""
        assert url == OPENAI.token and form["grant_type"] == "refresh_token"
        self.refreshes.append(dict(form))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with self.lock:
            self.issued += 1
            number = self.issued
        return 200, json.dumps({"access_token": f"at-{number}", "refresh_token": f"rt-{number}", "expires_in": 3600,
                                "scope": "openid chatgpt.tokens.use.direct"}).encode()


@pytest.fixture
def account(tmp_path):
    store = CredentialStore.encrypted(str(uuid.uuid4()), str(tmp_path / "credentials.enc"))
    store.backend.create(UnlockSecret.passphrase("a synthetic passphrase"))
    grant = Grant(client_id="oaiapp_123", account_id="user-1", email="owner@example.com", scope=("chatgpt.tokens.use.direct",),
                  plan_use=True, access_token="at-1", refresh_token="rt-1", id_token="id.token.saved", expires_at=NOW + 3600)
    ref = asyncio.run(store_grant(store, grant))
    return store, ref, tmp_path / "locks"


def bundle(store, ref):
    return store.consume(ref, account_id=ref["account_id"], purpose="provider_oauth_token").payload


def test_a_fresh_token_is_used_and_a_due_one_is_rotated_in_place(account):
    store, ref, locks = account
    clock, endpoint = Clock(), TokenEndpoint()
    use = lambda: asyncio.run(current_access_token(ref, opener=endpoint, clock=clock, lock_dir=str(locks)))  # noqa: E731
    assert use() == "at-1" and endpoint.refreshes == []
    clock.now = NOW + 3600 - 200  # inside the refresh margin
    assert use() == "at-2"
    assert endpoint.refreshes == [{"grant_type": "refresh_token", "client_id": "oaiapp_123", "refresh_token": "rt-1",
                                   "resource": "https://api.openai.com/v1"}]
    stored = bundle(store, ref)  # the same reference, a rotated bundle
    assert (stored["refresh_token"], stored["id_token"], stored["account_id"]) == ("rt-2", "id.token.saved", "user-1")
    assert use() == "at-2" and len(endpoint.refreshes) == 1
    restarted = CredentialStore.encrypted(ref["installation_id"], str(locks.parent / "credentials.enc"))
    restarted.backend.unlock(UnlockSecret.passphrase("a synthetic passphrase"))
    assert bundle(restarted, ref)["refresh_token"] == "rt-2"  # survives a restart


def test_many_callers_in_two_processes_refresh_once(account):
    _, ref, locks = account
    clock, endpoint = Clock(), TokenEndpoint(delay=0.05)
    clock.now = NOW + 3600 - 10

    async def many():
        return await asyncio.gather(*(current_access_token(ref, opener=endpoint, clock=clock, lock_dir=str(locks)) for _ in range(5)))
    results: list = []
    # Two event loops in two threads stand in for two processes: only the file lock is shared.
    threads = [threading.Thread(target=lambda: results.extend(asyncio.run(many()))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(endpoint.refreshes) == 1 and set(results) == {"at-2"}


def test_forcing_after_a_refusal_refreshes_unless_someone_already_did(account):
    _, ref, locks = account
    clock, endpoint = Clock(), TokenEndpoint()
    force = lambda stale: asyncio.run(current_access_token(ref, force=True, stale=stale, opener=endpoint, clock=clock, lock_dir=str(locks)))  # noqa: E731
    assert force("at-1") == "at-2" and force("at-1") == "at-2"  # the second caller's token was already replaced
    assert len(endpoint.refreshes) == 1


def test_a_refresh_failure_is_terminal_or_transient_and_never_erases_the_account(account):
    store, ref, locks = account
    clock = Clock()
    clock.now = NOW + 3600 - 200
    transient = TokenEndpoint(answers=[(503, b'{"error": "temporarily_unavailable"}')])
    assert asyncio.run(current_access_token(ref, opener=transient, clock=clock, lock_dir=str(locks))) == "at-1"  # still valid
    clock.now = NOW + 3600 + 5
    offline = TokenEndpoint(answers=[OSError("no route")])
    with pytest.raises(ChatGPTAccountError) as caught:
        asyncio.run(current_access_token(ref, opener=offline, clock=clock, lock_dir=str(locks)))
    assert (caught.value.code, caught.value.terminal) == ("token_endpoint_unreachable", False)
    for error in ("invalid_grant", "refresh_token_reused", "refresh_token_expired"):
        reused = TokenEndpoint(answers=[(400, json.dumps({"error": error}).encode())])
        with pytest.raises(ChatGPTAccountError) as caught:
            asyncio.run(current_access_token(ref, opener=reused, clock=clock, lock_dir=str(locks)))
        assert (caught.value.code, caught.value.terminal) == ("reauthorization_required", True)
    assert bundle(store, ref)["refresh_token"] == "rt-1"  # kept: signing in again replaces it


def test_the_transport_resolver_refreshes_a_due_bundle(account, monkeypatch):
    _, ref, locks = account
    endpoint = TokenEndpoint()
    monkeypatch.setattr(chatgpt_account, "urllib_opener", endpoint)
    monkeypatch.setattr(chatgpt_account.time, "time", lambda: NOW + 3600 - 1)
    with scoped_runtime_home(str(locks.parent / "home")):
        assert bytes(asyncio.run(resolve_runtime_secret(ref, "provider_oauth_token"))).decode() == "at-2"


@pytest.mark.parametrize("answers, confirmed, calls", [
    ([200], True, 1),
    ([503, OSError("reset"), 200], True, 3),
    ([OSError("down"), OSError("down"), OSError("down")], False, 3),
    ([400], False, 1),
])
def test_signing_out_revokes_with_bounded_retries_and_forgets_the_bundle(account, answers, confirmed, calls):
    store, ref, _ = account
    endpoint = TokenEndpoint(answers=answers)

    async def no_wait(_seconds):
        return None
    assert asyncio.run(revoke(ref, opener=endpoint, sleep=no_wait)) is confirmed
    assert len(endpoint.revocations) == calls
    assert endpoint.revocations[0] == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "oaiapp_123"}
    with pytest.raises(CredentialStoreError):
        bundle(store, ref)


def test_only_an_oauth_bundle_is_rewritten_in_place(account):
    store, _, _ = account
    key = store.write(str(uuid.uuid4()), "provider_api_key", {"api_key": "sk-synthetic"})
    with pytest.raises(CredentialStoreError):
        store.replace(key, account_id=key["account_id"], purpose="provider_api_key", value={"api_key": "sk-other"})
