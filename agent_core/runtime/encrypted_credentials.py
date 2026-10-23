"""Encrypted-at-rest credential file for hosts without a usable desktop store.

For headless Linux, WSL and locked keyrings; selected explicitly, never as a
fallback. One versioned file holds every credential of one installation:
AES-256-GCM over the whole map, keyed by either the owner's passphrase
(scrypt, N=2**17, r=8, p=1) or a 32-byte service key that the service
manager provides (systemd LoadCredential, or a key file outside the runtime
home), expanded with HKDF-SHA256. The format, parameters and installation
id are authenticated as associated data, so a file cannot be moved to
another installation or downgraded unnoticed.

The store is locked until unlocked with the right secret; a wrong secret, a
tampered file and a corrupt file are all refused without detail. Every write
re-reads the file under an exclusive lock (so two writers never lose each
other's updates), encrypts with a fresh nonce and replaces the file
atomically: temporary file, fsync, rename, directory fsync. There is no
recovery without the passphrase or key, and nothing here keeps a copy of
either on disk. Requires the `encrypted-credentials` extra (cryptography).
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

FORMAT = "tess-agent.credentials"
VERSION = 1
SCRYPT = {"name": "scrypt", "n": 2 ** 17, "r": 8, "p": 1}
MAX_FILE_BYTES = 2 * 1024 * 1024


class KeyringLocked(Exception):
    """Locked, or unlocked with the wrong secret. Named like keyring's error
    so the store maps it to ``credential_locked``."""


class CredentialFileInvalid(ValueError):
    """Not a readable credential file of this version and installation."""


class UnlockSecret:
    """A passphrase or a 32-byte service key; repr never shows either."""

    def __init__(self, kind: str, value: bytes) -> None:
        if kind not in {"passphrase", "service_key"}:
            raise ValueError("unknown secret kind")
        if kind == "passphrase" and len(value) < 12:
            raise ValueError("a passphrase needs at least 12 characters")
        if kind == "service_key" and len(value) != 32:
            raise ValueError("a service key is exactly 32 bytes")
        self.kind = kind
        self._value = bytearray(value)

    @classmethod
    def passphrase(cls, text: str) -> UnlockSecret:
        return cls("passphrase", text.encode())

    @classmethod
    def service_key_file(cls, path: str | os.PathLike[str], *, runtime_home: str | os.PathLike[str]) -> UnlockSecret:
        """Read a service key the service manager or owner placed outside the
        runtime home: owned by this user, not a link, not group/world readable,
        32 raw bytes or their base64."""
        key_path = Path(os.path.abspath(path))
        home = Path(os.path.realpath(runtime_home))
        if Path(os.path.realpath(key_path.parent)).is_relative_to(home):
            raise PermissionError("the service key must not live inside the runtime home it protects")
        fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("the service key file must be a private regular file owned by this user")
            raw = os.read(fd, 128)
        finally:
            os.close(fd)
        raw = raw.strip()
        if len(raw) != 32:
            try:
                raw = base64.b64decode(raw, validate=True)
            except ValueError:
                raise ValueError("a service key is 32 raw bytes or their base64") from None
        return cls("service_key", bytes(raw))

    def derive(self, kdf: dict[str, Any], installation_id: str) -> bytearray:
        salt = base64.b64decode(kdf["salt"])
        if self.kind == "passphrase":
            if kdf.get("name") != "scrypt":
                raise KeyringLocked()
            return bytearray(hashlib.scrypt(bytes(self._value), salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"],
                                            maxmem=256 * 1024 * 1024, dklen=32))
        if kdf.get("name") != "hkdf-sha256":
            raise KeyringLocked()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        return bytearray(HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                              info=f"{FORMAT}/{installation_id}".encode()).derive(bytes(self._value)))

    def new_kdf(self) -> dict[str, Any]:
        salt = base64.b64encode(secrets.token_bytes(16)).decode()
        return {**SCRYPT, "salt": salt} if self.kind == "passphrase" else {"name": "hkdf-sha256", "salt": salt}

    def wipe(self) -> None:
        for index in range(len(self._value)):
            self._value[index] = 0

    def __repr__(self) -> str:
        return f"UnlockSecret({self.kind})"


class EncryptedFileBackend:
    """The CredentialBackend protocol over one encrypted file."""

    def __init__(self, path: str | os.PathLike[str], installation_id: str) -> None:
        self.path = Path(os.path.abspath(path))
        self.installation_id = installation_id
        self._key: bytearray | None = None
        self._kdf: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @property
    def locked(self) -> bool:
        return self._key is None

    # -- lifecycle ------------------------------------------------------

    def create(self, secret: UnlockSecret) -> None:
        """A new empty store, unlocked; refuses to replace an existing file."""
        with self._exclusive():
            if self.path.exists():
                raise FileExistsError("a credential file already exists here")
            kdf = secret.new_kdf()
            key = secret.derive(kdf, self.installation_id)
            self._write({}, key, kdf)
            self._key, self._kdf = key, kdf

    def unlock(self, secret: UnlockSecret) -> None:
        with self._exclusive():
            envelope = self._read_envelope()
            key = secret.derive(envelope["kdf"], self.installation_id)
            self._decrypt(envelope, key)  # proves the secret
            self.lock()
            self._key, self._kdf = key, envelope["kdf"]

    def lock(self) -> None:
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
        self._key = None

    def rekey(self, secret: UnlockSecret) -> None:
        """Re-encrypt every credential under a new passphrase or key."""
        with self._exclusive():
            entries = self._load()
            kdf = secret.new_kdf()
            key = secret.derive(kdf, self.installation_id)
            self._write(entries, key, kdf)
            self.lock()
            self._key, self._kdf = key, kdf

    # -- CredentialBackend ----------------------------------------------

    def get_password(self, service: str, username: str) -> str | None:
        with self._exclusive():
            return self._load().get(service, {}).get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._exclusive():
            entries = self._load()
            entries.setdefault(service, {})[username] = password
            self._write(entries, self._key, self._kdf)

    def delete_password(self, service: str, username: str) -> None:
        with self._exclusive():
            entries = self._load()
            if username in entries.get(service, {}):
                del entries[service][username]
                if not entries[service]:
                    del entries[service]
                self._write(entries, self._key, self._kdf)

    # -- file -----------------------------------------------------------

    def _exclusive(self):
        backend = self

        class _Held:
            def __enter__(self):
                backend._lock.acquire()
                try:
                    backend._check_directory()
                    self.fd = os.open(f"{backend.path}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    fcntl.flock(self.fd, fcntl.LOCK_EX)
                except BaseException:
                    backend._lock.release()
                    raise
                return self

            def __exit__(self, *_):
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                    os.close(self.fd)
                finally:
                    backend._lock.release()
        return _Held()

    def _check_directory(self) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise PermissionError("the credential directory must be owned by this user and not writable by others")

    def _load(self) -> dict[str, dict[str, str]]:
        if self._key is None:
            raise KeyringLocked()
        return self._decrypt(self._read_envelope(), self._key)

    def _read_envelope(self) -> dict[str, Any]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise CredentialFileInvalid("no credential file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError("the credential file must be private to this user")
            if info.st_size > MAX_FILE_BYTES:
                raise CredentialFileInvalid("credential file too large")
            raw = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        try:
            envelope = json.loads(raw)
            kdf = envelope["kdf"]
            valid = (envelope.get("format") == FORMAT and envelope.get("version") == VERSION
                     and envelope.get("cipher") == "AES-256-GCM" and envelope.get("installation_id") == self.installation_id
                     and isinstance(kdf, dict) and isinstance(kdf.get("salt"), str)
                     and (kdf.get("name") == "hkdf-sha256" or {k: kdf.get(k) for k in SCRYPT} == SCRYPT))
            if not valid:
                raise ValueError()
            return envelope
        except (ValueError, KeyError, TypeError):
            raise CredentialFileInvalid("unsupported or corrupt credential file") from None

    def _associated(self, kdf: dict[str, Any]) -> bytes:
        return json.dumps({"format": FORMAT, "version": VERSION, "cipher": "AES-256-GCM",
                           "installation_id": self.installation_id, "kdf": kdf}, sort_keys=True).encode()

    def _decrypt(self, envelope: dict[str, Any], key: bytearray) -> dict[str, dict[str, str]]:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            plain = AESGCM(bytes(key)).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]),
                                               self._associated(envelope["kdf"]))
        except (InvalidTag, ValueError, KeyError, TypeError):
            raise KeyringLocked() from None  # wrong secret and tampering look the same
        try:
            entries = json.loads(plain)["entries"]
            if not isinstance(entries, dict):
                raise ValueError()
            return entries
        except (ValueError, KeyError, TypeError):
            raise CredentialFileInvalid("unsupported or corrupt credential file") from None

    def _write(self, entries: dict[str, dict[str, str]], key: bytearray | None, kdf: dict[str, Any] | None) -> None:
        if key is None or kdf is None:
            raise KeyringLocked()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = secrets.token_bytes(12)
        plain = json.dumps({"entries": entries}, sort_keys=True).encode()
        ciphertext = AESGCM(bytes(key)).encrypt(nonce, plain, self._associated(kdf))
        envelope = {"format": FORMAT, "version": VERSION, "cipher": "AES-256-GCM", "installation_id": self.installation_id,
                    "kdf": kdf, "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}
        data = json.dumps(envelope, sort_keys=True).encode()
        if len(data) > MAX_FILE_BYTES:
            raise CredentialFileInvalid("credential file too large")
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
