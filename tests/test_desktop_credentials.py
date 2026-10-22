"""Synthetic backend only: no OS backend is selected or contacted by these tests."""
import asyncio
import json
import secrets
import uuid

import pytest

from agent_core.runtime.credentials import CredentialStore, CredentialStoreError, desktop_backend
from agent_core.runtime.secrets import resolve_runtime_secret
from agent_core.config import ProviderRegistry


class FakeBackend:
    def __init__(self):
        self.records = {}
        self.fail = False
    def get_password(self, service, username):
        if self.fail:
            raise PermissionError('never include backend detail')
        return self.records.get((service, username))
    def set_password(self, service, username, password):
        if self.fail:
            raise PermissionError('never include backend detail')
        self.records[service, username] = password
    def delete_password(self, service, username):
        if self.fail:
            raise PermissionError('never include backend detail')
        self.records.pop((service, username), None)


def setup():
    backend = FakeBackend()
    installation, account = str(uuid.uuid4()), str(uuid.uuid4())
    return CredentialStore(installation, 'macos_keychain', backend), backend, installation, account


def test_immutable_replace_restart_and_in_use_delete():
    store, backend, installation, account = setup()
    key = secrets.token_urlsafe(24)
    ref = store.write(account, 'provider_api_key', {'api_key': key})
    restarted = CredentialStore(installation, 'macos_keychain', backend)
    assert restarted.consume(ref, account_id=account, purpose='provider_api_key').payload['api_key'] == key
    assert key not in repr(restarted.consume(ref, account_id=account, purpose='provider_api_key')) + repr(ref)
    backend.fail = True
    with pytest.raises(CredentialStoreError):
        store.write(account, 'provider_api_key', {'api_key': secrets.token_urlsafe(24)})
    backend.fail = False
    assert store.consume(ref, account_id=account, purpose='provider_api_key').payload['api_key'] == key
    with pytest.raises(CredentialStoreError) as exc:
        store.delete(ref, account_id=account, purpose='provider_api_key', referenced_ids=frozenset({ref['id']}))
    assert exc.value.code == 'credential_in_use'
    store.delete(ref, account_id=account, purpose='provider_api_key', referenced_ids=frozenset())
    with pytest.raises(CredentialStoreError):
        store.consume(ref, account_id=account, purpose='provider_api_key')


@pytest.mark.parametrize('field', ['installation_id', 'account_id', 'backend', 'purpose', 'id'])
def test_reference_cannot_cross_binding(field):
    store, backend, installation, account = setup()
    ref = store.write(account, 'provider_api_key', {'api_key': secrets.token_urlsafe(24)})
    ref[field] = str(uuid.uuid4())
    with pytest.raises(CredentialStoreError):
        store.consume(ref, account_id=account, purpose='provider_api_key')


def test_structured_oauth_bundle_atomic_replacement():
    store, backend, installation, account = setup()
    value = {'access_token': secrets.token_urlsafe(), 'refresh_token': secrets.token_urlsafe(),
             'client_id': 'synthetic-client', 'account_id': 'synthetic-account', 'expires_at': 2000}
    ref = store.write(account, 'provider_oauth_token', value)
    assert store.consume(ref, account_id=account, purpose='provider_oauth_token').payload == value
    assert value['access_token'] not in repr(ref)


def test_managed_reference_consumed_by_existing_engine_secret_boundary(monkeypatch, tmp_path):
    store, backend, installation, account = setup()
    key = secrets.token_urlsafe(24)
    ref = store.write(account, 'provider_api_key', {'api_key': key})
    monkeypatch.setattr(CredentialStore, 'desktop', classmethod(lambda cls, instance, name: store))
    result = asyncio.run(resolve_runtime_secret(ref, 'provider_api_key'))
    assert bytes(result).decode() == key
    assert list(tmp_path.iterdir()) == []


def test_registry_carries_ref_not_value_and_preserves_legacy_auth(tmp_path):
    store, backend, installation, account = setup()
    key = secrets.token_urlsafe(24)
    ref = store.write(account, 'provider_api_key', {'api_key': key})
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'profiles': {
        'managed': {'kind': 'openai', 'defaultModel': 'synthetic', 'auth': {'kind': 'openai_api_key', 'credentialRef': ref}},
        'legacy': {'kind': 'anthropic', 'defaultModel': 'synthetic', 'auth': {'kind': 'anthropic_auth_token', 'envVar': 'SYNTHETIC_TOKEN'}}}}))
    registry = ProviderRegistry(path)
    assert registry.resolve_auth('managed')['apiKey'] == ref
    assert key not in repr(registry.profiles)
    assert registry.resolve_auth('legacy', environment={'SYNTHETIC_TOKEN': key})['authToken'] == key


@pytest.mark.parametrize('backend', ['null', 'plaintext', 'auto', '', 'chainer'])
def test_unsupported_backend_never_falls_back(backend):
    with pytest.raises(CredentialStoreError):
        desktop_backend(backend)
