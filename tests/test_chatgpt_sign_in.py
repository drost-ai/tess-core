"""Sign in with ChatGPT against a fake OpenAI: an RSA key made here signs the
ID tokens, the token endpoint and JWKS are answered in process, and the
callback is delivered to the attempt directly. Nothing reaches OpenAI."""
import asyncio
import base64
import hashlib
import json
import time
import urllib.parse

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from agent_core.providers.chatgpt_account import (  # noqa: E402
    OPENAI, PLAN_SCOPE, SCOPE, ChatGPTAccountError, SignIn, verify_id_token,
)

NOW = time.time()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeOpenAI:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                    "n": b64(numbers.n.to_bytes(256, "big")), "e": b64(numbers.e.to_bytes(3, "big"))}
        self.calls, self.nonce, self.sub, self.scope = [], None, "user-1", SCOPE
        self.claims: dict = {}
        self.signer = self.key

    def sign(self, claims, *, alg="RS256"):
        head = b64(json.dumps({"alg": alg, "kid": "k1", "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        signature = self.signer.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{b64(signature)}"

    async def __call__(self, method, url, form):
        self.calls.append((method, url, dict(form or {})))
        if url == OPENAI.jwks:
            return 200, json.dumps({"keys": [self.jwk]}).encode()
        if url == OPENAI.token and form["grant_type"] == "authorization_code":
            if form["code"] != "good-code":
                return 400, b'{"error": "invalid_grant"}'
            claims = {"iss": OPENAI.issuer, "aud": form["client_id"], "sub": self.sub, "email": "owner@example.com",
                      "exp": NOW + 3600, "iat": NOW, "nonce": self.nonce, **self.claims}
            return 200, json.dumps({"access_token": "at-1", "refresh_token": "rt-1", "id_token": self.sign(claims),
                                    "token_type": "Bearer", "expires_in": 3600, "scope": self.scope,
                                    "earliest_refresh_at": NOW + 1800}).encode()
        raise AssertionError(f"unexpected request {method} {url}")


def query(attempt):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(attempt.url).query))


def sign_in(fake, *, callback=None, **kwargs):
    async def scenario():
        attempt = SignIn(host_id="host-1", app_name="Tess-Agent", opener=fake, clock=lambda: NOW, **kwargs)
        params = query(attempt)
        fake.nonce = params["nonce"]
        attempt.deliver(callback(params) if callback else f"/auth/callback?code=good-code&state={params['state']}&client_id=oaiapp_123")
        return attempt, params, await attempt.finish(timeout=1)
    return asyncio.run(scenario())


def failure(fake, **kwargs):
    with pytest.raises(ChatGPTAccountError) as caught:
        sign_in(fake, **kwargs)
    assert "at-1" not in str(caught.value) and "rt-1" not in str(caught.value)
    return caught.value.code


def test_a_first_sign_in_registers_with_the_real_name_and_keeps_the_issued_client():
    fake = FakeOpenAI()
    attempt, params, grant = sign_in(fake)
    assert urllib.parse.urlsplit(attempt.url)._replace(query="").geturl() == OPENAI.authorize
    assert (params["client_id"], params["agent_name_hint"], params["ext_agent_host_id"]) == ("dynamic_agent_client", "Tess-Agent", "host-1")
    assert params["redirect_uri"] == "http://127.0.0.1:1455/auth/callback" and params["scope"] == SCOPE
    assert (params["response_type"], params["resource"], params["code_challenge_method"]) == ("code", "https://api.openai.com/v1", "S256")
    assert params["code_challenge"] == b64(hashlib.sha256(attempt.verifier.encode()).digest())
    assert "id_token_hint" not in params and "login_hint" not in params
    _, _, exchange = next(call for call in fake.calls if call[1] == OPENAI.token)
    assert exchange == {"grant_type": "authorization_code", "client_id": "oaiapp_123", "code": "good-code",
                        "code_verifier": attempt.verifier, "redirect_uri": params["redirect_uri"], "resource": "https://api.openai.com/v1"}
    assert (grant.client_id, grant.account_id, grant.email, grant.plan_use) == ("oaiapp_123", "user-1", "owner@example.com", True)
    assert grant.expires_at == NOW + 3600 and grant.earliest_refresh_at == NOW + 1800
    assert "at-1" not in repr(grant) and "rt-1" not in repr(grant)
    assert grant.bundle()["refresh_token"] == "rt-1" and grant.bundle()["account_id"] == "user-1"


def test_signing_in_again_reuses_the_client_and_must_return_the_same_account():
    fake = FakeOpenAI()
    attempt, params, grant = sign_in(fake, client_id="oaiapp_123", account_id="user-1", id_token_hint="saved.id.token",
                                     login_hint="owner@example.com",
                                     callback=lambda p: f"/auth/callback?code=good-code&state={p['state']}")
    assert (params["client_id"], params["id_token_hint"], params["login_hint"]) == ("oaiapp_123", "saved.id.token", "owner@example.com")
    assert "agent_name_hint" not in params and grant.client_id == "oaiapp_123"
    other = FakeOpenAI()
    other.sub = "someone-else"
    assert failure(other, client_id="oaiapp_123", account_id="user-1") == "account_mismatch"
    assert failure(FakeOpenAI(), client_id="oaiapp_123", account_id="user-1",
                   callback=lambda p: f"/auth/callback?code=good-code&state={p['state']}&client_id=oaiapp_999") == "client_mismatch"


@pytest.mark.parametrize("callback, code", [
    (lambda p: f"/auth/callback?error=access_denied&state={p['state']}", "access_denied"),
    (lambda p: f"/auth/callback?code=good-code&state={p['state']}", "registration_incomplete"),
    (lambda p: f"/auth/callback?code=good-code&state={p['state']}&client_id=dynamic_agent_client", "registration_incomplete"),
    (lambda p: f"/auth/callback?code=stale-code&state={p['state']}&client_id=oaiapp_123", "code_rejected"),
])
def test_a_callback_that_does_not_complete_a_sign_in_is_refused(callback, code):
    assert failure(FakeOpenAI(), callback=callback) == code


def test_only_the_matching_state_on_the_exact_path_completes_an_attempt_once():
    async def scenario():
        attempt = SignIn(host_id="host-1", app_name="Tess-Agent", opener=FakeOpenAI(), clock=lambda: NOW)
        state = query(attempt)["state"]
        assert attempt.deliver(f"/auth/callbackx?code=good-code&state={state}")[0] == 404
        assert attempt.deliver("/auth/callback?code=good-code&state=forged")[0] == 400
        assert attempt.deliver(f"/auth/callback?code=good-code&state={state}&client_id=oaiapp_123")[0] == 200
        assert attempt.deliver(f"/auth/callback?code=other&state={state}&client_id=oaiapp_123")[0] == 409  # a replay
        with pytest.raises(ChatGPTAccountError) as caught:
            await SignIn(host_id="host-1", app_name="Tess-Agent", opener=FakeOpenAI()).finish(timeout=0.05)
        assert caught.value.code == "timed_out"
    asyncio.run(scenario())


def test_a_grant_without_the_plan_scope_keeps_the_sign_in_with_plan_use_off():
    fake = FakeOpenAI()
    fake.scope = " ".join(part for part in SCOPE.split() if part != PLAN_SCOPE)
    assert sign_in(fake)[2].plan_use is False


@pytest.mark.parametrize("tamper", ["other_key", "audience", "nonce", "issuer", "expired", "alg"])
def test_an_id_token_that_does_not_verify_is_refused(tamper):
    fake = FakeOpenAI()
    if tamper == "other_key":
        fake.signer = fake.other
    elif tamper == "audience":
        fake.claims = {"aud": "oaiapp_someone_else"}
    elif tamper == "nonce":
        fake.claims = {"nonce": "replayed"}
    elif tamper == "issuer":
        fake.claims = {"iss": "https://auth.example.com"}
    elif tamper == "expired":
        fake.claims = {"exp": NOW - 3600}
    if tamper == "alg":
        token = fake.sign({"iss": OPENAI.issuer, "aud": "c", "sub": "s", "exp": NOW + 60, "nonce": "n"}, alg="HS256")
        with pytest.raises(ChatGPTAccountError):
            verify_id_token(token, jwks={"keys": [fake.jwk]}, issuer=OPENAI.issuer, audience="c", nonce="n", now=NOW)
        return
    assert failure(fake) == "id_token_invalid"
