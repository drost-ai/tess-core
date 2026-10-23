"""Encrypted credential file: temporary directories, synthetic secrets only."""
import base64
import hashlib
import json
import os
import secrets
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_core.runtime import encrypted_credentials as encrypted
from agent_core.runtime.credentials import CredentialStore, CredentialStoreError
from agent_core.runtime.encrypted_credentials import EncryptedFileBackend, UnlockSecret
from agent_core.runtime.secrets import resolve_runtime_secret

PASSPHRASE = "correct horse battery staple"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    # The real parameters (N=2**17) are covered by one test; the rest use a
    # cheap N so the suite stays fast. The format check accepts only SCRYPT.
    monkeypatch.setattr(encrypted, "SCRYPT", {"name": "scrypt", "n": 2 ** 10, "r": 8, "p": 1})


def home(tmp_path):
    directory = tmp_path / "home" / "private-secrets"
    directory.mkdir(parents=True, mode=0o700)
    return directory / "credentials.enc"


def test_primitives_match_published_vectors():
    # RFC 7914 section 12, second vector.
    assert hashlib.scrypt(b"password", salt=b"NaCl", n=1024, r=8, p=16, dklen=64).hex().startswith("fdbabe1c9d3472007856e7190d01e9fe7c6ad7cbc8237830e77376634b373162")
    # AES-256-GCM, NIST/McGrew-Viega test case 14: zero key, zero IV, one zero block.
    sealed = AESGCM(bytes(32)).encrypt(bytes(12), bytes(16), None)
    assert sealed.hex() == "cea7403d4d606b6e074ec5d3baf39d18" + "d0d1c8a799996bf0265b98b5d48ab919"


def test_real_scrypt_parameters_unlock(tmp_path, monkeypatch):
    monkeypatch.setattr(encrypted, "SCRYPT", {"name": "scrypt", "n": 2 ** 17, "r": 8, "p": 1})
    path, installation = home(tmp_path), str(uuid.uuid4())
    EncryptedFileBackend(path, installation).create(UnlockSecret.passphrase(PASSPHRASE))
    assert json.loads(path.read_text())["kdf"]["n"] == 2 ** 17
    EncryptedFileBackend(path, installation).unlock(UnlockSecret.passphrase(PASSPHRASE))


def test_credentials_survive_restart_only_with_the_secret_and_never_in_plaintext(tmp_path):
    path, installation, account = home(tmp_path), str(uuid.uuid4()), str(uuid.uuid4())
    store = CredentialStore.encrypted(installation, str(path))
    store.backend.create(UnlockSecret.passphrase(PASSPHRASE))
    key = "synthetic-" + secrets.token_urlsafe(24)
    bundle = {"access_token": secrets.token_urlsafe(), "refresh_token": secrets.token_urlsafe(),
              "client_id": "synthetic-client", "account_id": "synthetic-account", "expires_at": 2000}
    api_ref = store.write(account, "provider_api_key", {"api_key": key})
    oauth_ref = store.write(account, "provider_oauth_token", bundle)
    raw = path.read_bytes()
    assert key.encode() not in raw and bundle["refresh_token"].encode() not in raw
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    restarted = CredentialStore.encrypted(installation, str(path))  # a new process: locked
    with pytest.raises(CredentialStoreError) as caught:
        restarted.consume(api_ref, account_id=account, purpose="provider_api_key")
    assert caught.value.code == "credential_locked" and restarted.probe() == "locked"
    with pytest.raises(encrypted.KeyringLocked):
        restarted.backend.unlock(UnlockSecret.passphrase("a wrong passphrase!"))
    restarted.backend.unlock(UnlockSecret.passphrase(PASSPHRASE))
    assert restarted.consume(oauth_ref, account_id=account, purpose="provider_oauth_token").payload == bundle
    assert restarted.probe() == "available"
    import asyncio
    assert bytes(asyncio.run(resolve_runtime_secret(api_ref, "provider_api_key"))).decode() == key
    restarted.backend.lock()
    with pytest.raises(CredentialStoreError) as caught:
        asyncio.run(resolve_runtime_secret(api_ref, "provider_api_key"))
    assert caught.value.code == "credential_locked"


def test_an_unregistered_installation_is_locked_not_read(tmp_path):
    ref = {"kind": "runtime_secret", "version": 1, "id": str(uuid.uuid4()), "purpose": "provider_api_key",
           "backend": "encrypted_file", "installation_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4())}
    import asyncio
    with pytest.raises(CredentialStoreError) as caught:
        asyncio.run(resolve_runtime_secret(ref, "provider_api_key"))
    assert caught.value.code == "credential_locked"


@pytest.mark.parametrize("tamper", ["ciphertext", "installation", "kdf", "version", "garbage"])
def test_tampered_moved_or_corrupt_files_are_refused(tmp_path, tamper):
    path, installation = home(tmp_path), str(uuid.uuid4())
    backend = EncryptedFileBackend(path, installation)
    backend.create(UnlockSecret.passphrase(PASSPHRASE))
    backend.set_password("service", "name", "synthetic-value")
    envelope = json.loads(path.read_text())
    if tamper == "ciphertext":
        blob = bytearray(base64.b64decode(envelope["ciphertext"]))
        blob[0] ^= 1
        envelope["ciphertext"] = base64.b64encode(bytes(blob)).decode()
    elif tamper == "installation":
        envelope["installation_id"] = str(uuid.uuid4())
    elif tamper == "kdf":
        envelope["kdf"]["n"] = 2 ** 4
    elif tamper == "version":
        envelope["version"] = 2
    path.write_text("{not json" if tamper == "garbage" else json.dumps(envelope))
    with pytest.raises((encrypted.KeyringLocked, encrypted.CredentialFileInvalid)):
        EncryptedFileBackend(path, installation).unlock(UnlockSecret.passphrase(PASSPHRASE))


def test_unsafe_files_and_directories_are_refused(tmp_path):
    path, installation = home(tmp_path), str(uuid.uuid4())
    EncryptedFileBackend(path, installation).create(UnlockSecret.passphrase(PASSPHRASE))
    os.chmod(path, 0o644)
    with pytest.raises(PermissionError):
        EncryptedFileBackend(path, installation).unlock(UnlockSecret.passphrase(PASSPHRASE))
    os.chmod(path, 0o600)
    link = path.with_name("link.enc")
    link.symlink_to(path)
    with pytest.raises(OSError):
        EncryptedFileBackend(link, installation).unlock(UnlockSecret.passphrase(PASSPHRASE))
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o777)
    os.chmod(open_dir, 0o777)
    with pytest.raises(PermissionError):
        EncryptedFileBackend(open_dir / "c.enc", installation).create(UnlockSecret.passphrase(PASSPHRASE))
    with pytest.raises(FileExistsError):
        EncryptedFileBackend(path, installation).create(UnlockSecret.passphrase(PASSPHRASE))


def test_two_writers_never_lose_each_others_updates(tmp_path):
    path, installation = home(tmp_path), str(uuid.uuid4())
    first = EncryptedFileBackend(path, installation)
    first.create(UnlockSecret.passphrase(PASSPHRASE))
    second = EncryptedFileBackend(path, installation)
    second.unlock(UnlockSecret.passphrase(PASSPHRASE))
    for index in range(5):
        first.set_password("s", f"first-{index}", "v")
        second.set_password("s", f"second-{index}", "v")
    names = {name for name in ("first", "second") for name in [f"{name}-{index}" for index in range(5)]}
    assert all(first.get_password("s", name) == "v" for name in names)


def test_a_failed_replacement_keeps_the_previous_file(tmp_path, monkeypatch):
    path, installation, account = home(tmp_path), str(uuid.uuid4()), str(uuid.uuid4())
    store = CredentialStore.encrypted(installation, str(path))
    store.backend.create(UnlockSecret.passphrase(PASSPHRASE))
    old = {"access_token": "a1", "refresh_token": "r1", "client_id": "c", "account_id": "x", "expires_at": 1}
    ref = store.write(account, "provider_oauth_token", old)
    before = path.read_bytes()

    def crash(*_args):
        raise OSError("synthetic crash before rename")
    with monkeypatch.context() as patched:
        patched.setattr(encrypted.os, "replace", crash)
        with pytest.raises(CredentialStoreError):
            store.write(account, "provider_oauth_token", {**old, "refresh_token": "r2"})
    assert path.read_bytes() == before
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert store.consume(ref, account_id=account, purpose="provider_oauth_token").payload == old


def test_rekey_and_service_keys(tmp_path):
    path, installation = home(tmp_path), str(uuid.uuid4())
    backend = EncryptedFileBackend(path, installation)
    backend.create(UnlockSecret.passphrase(PASSPHRASE))
    backend.set_password("s", "n", "kept")
    runtime_home = path.parent.parent
    key_dir = tmp_path / "service-credentials"
    key_dir.mkdir(mode=0o700)
    key_file = key_dir / "tess-agent.key"
    key_file.write_bytes(base64.b64encode(secrets.token_bytes(32)))
    os.chmod(key_file, 0o400)
    service = UnlockSecret.service_key_file(key_file, runtime_home=runtime_home)
    assert "tess" not in repr(service) and repr(service) == "UnlockSecret(service_key)"
    backend.rekey(service)
    with pytest.raises(encrypted.KeyringLocked):
        EncryptedFileBackend(path, installation).unlock(UnlockSecret.passphrase(PASSPHRASE))
    fresh = EncryptedFileBackend(path, installation)
    fresh.unlock(UnlockSecret.service_key_file(key_file, runtime_home=runtime_home))
    assert fresh.get_password("s", "n") == "kept"
    inside = runtime_home / "tess-agent.key"
    inside.write_bytes(secrets.token_bytes(32))
    os.chmod(inside, 0o600)
    with pytest.raises(PermissionError):
        UnlockSecret.service_key_file(inside, runtime_home=runtime_home)
    os.chmod(key_file, 0o644)
    with pytest.raises(PermissionError):
        UnlockSecret.service_key_file(key_file, runtime_home=runtime_home)
    with pytest.raises(ValueError):
        UnlockSecret.passphrase("too short")
