"""Explicit desktop backend for existing runtime-secret/provider references.

No backend autodiscovery, fallback, auth-file import, CLI subprocess, environment
credential injection, or OS operation occurs at import. Desktop operations are
injected in tests. Owner-user processes are trusted; Python keyring does not
promise isolation against arbitrary code running as that OS user.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .secrets import RuntimeSecretError, is_runtime_secret_ref


class CredentialStoreError(RuntimeSecretError):
    def __init__(self, code: str = 'credential_unavailable'):
        # Only static codes/messages, never backend exceptions or secret values.
        if code not in {'credential_unavailable', 'credential_locked', 'credential_invalid',
                        'credential_missing', 'credential_in_use', 'credential_permission_denied'}:
            code = 'credential_unavailable'
        self.code = code
        RuntimeError.__init__(self, 'Credential operation unavailable; check the selected backend privately.')


def _backend_error(error: Exception) -> CredentialStoreError:
    if isinstance(error, PermissionError):
        return CredentialStoreError('credential_permission_denied')
    if type(error).__name__ == 'KeyringLocked':
        return CredentialStoreError('credential_locked')
    if type(error).__name__ == 'CredentialFileInvalid':
        return CredentialStoreError('credential_invalid')
    return CredentialStoreError()


BACKENDS = frozenset({'macos_keychain', 'linux_secret_service', 'encrypted_file'})
# Unlocked encrypted-file stores by installation: the engine's secret boundary
# resolves managed refs through these, never by reading the file itself.
_ENCRYPTED: dict[str, 'CredentialStore'] = {}


class CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def desktop_backend(name: str) -> CredentialBackend:
    """Use maintained keyring concrete backend classes, never get_keyring().

    Dependency is optional and explicitly selected. Never select chainer, null,
    plaintext plugins, macOS `security` argv, or another backend on failure.
    """
    try:
        if name == 'macos_keychain':
            from keyring.backends.macOS import Keyring
        elif name == 'linux_secret_service':
            from keyring.backends.SecretService import Keyring
        else:
            raise CredentialStoreError('credential_invalid')
        return Keyring()
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError() from None


def _uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise CredentialStoreError('credential_invalid') from None


@dataclass(frozen=True)
class CredentialValue:
    payload: Mapping[str, Any] = field(repr=False)


class CredentialStore:
    """Immutable per-operation writes; old ref survives failed replacements.

    RuntimeSecretRef purpose/id semantics are reused. Extra explicit backend,
    installation/account pins distinguish managed refs from legacy file refs.
    No raw-value retrieval is intended for browser/host diagnostics.
    """
    def __init__(self, installation_id: str, backend_name: str, backend: CredentialBackend):
        self.installation_id = _uuid(installation_id)
        if backend_name not in BACKENDS:
            raise CredentialStoreError('credential_invalid')
        self.backend_name, self._backend = backend_name, backend

    @classmethod
    def desktop(cls, installation_id: str, backend_name: str) -> CredentialStore:
        return cls(installation_id, backend_name, desktop_backend(backend_name))

    @classmethod
    def encrypted(cls, installation_id: str, path: str) -> CredentialStore:
        """The installation's encrypted-file store, locked until its backend
        is created or unlocked; registered so managed refs resolve through it."""
        from .encrypted_credentials import EncryptedFileBackend
        store = cls(installation_id, 'encrypted_file', EncryptedFileBackend(path, _uuid(installation_id)))
        _ENCRYPTED[store.installation_id] = store
        return store

    @classmethod
    def open(cls, installation_id: str, backend_name: str) -> CredentialStore:
        """The store a managed ref names. An encrypted file nobody unlocked in
        this process is locked, not read."""
        if backend_name == 'encrypted_file':
            store = _ENCRYPTED.get(_uuid(installation_id))
            if store is None:
                raise CredentialStoreError('credential_locked')
            return store
        return cls.desktop(installation_id, backend_name)

    @property
    def backend(self) -> CredentialBackend:
        return self._backend

    def _key(self, ref: Mapping[str, Any], *, account_id: str, purpose: str) -> tuple[str, str]:
        if (not is_runtime_secret_ref(dict(ref)) or set(ref) != {
                'kind', 'version', 'id', 'purpose', 'backend', 'installation_id', 'account_id'}
                or ref['backend'] != self.backend_name or ref['installation_id'] != self.installation_id
                or ref['account_id'] != _uuid(account_id) or ref['purpose'] != purpose):
            raise CredentialStoreError('credential_invalid')
        _uuid(ref['id'])
        return f'tess-agent/{self.installation_id}/{account_id}/{purpose}', ref['id']

    @staticmethod
    def _serialize(value: Mapping[str, Any], purpose: str) -> str:
        try:
            if not isinstance(value, Mapping):
                raise ValueError()
            value = dict(value)
            if purpose == 'provider_api_key':
                if set(value) != {'api_key'} or not isinstance(value['api_key'], str) or not value['api_key'].strip():
                    raise ValueError()
            elif purpose == 'channel_token':  # a host's messaging-platform token (a bot token)
                if set(value) != {'token'} or not isinstance(value['token'], str) or not value['token'].strip():
                    raise ValueError()
            elif purpose == 'provider_oauth_token':
                if (set(value) - {'access_token', 'refresh_token', 'expires_at', 'client_id', 'account_id', 'id_token'}
                        or any(not isinstance(value.get(key), str) or not value[key]
                               for key in ('access_token', 'refresh_token', 'client_id', 'account_id'))
                        or type(value.get('expires_at')) not in (int, float)):
                    raise ValueError()
            else:
                raise ValueError()
            text = json.dumps(value, sort_keys=True, allow_nan=False)
            if len(text.encode()) > 64 * 1024:
                raise ValueError()
            return text
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CredentialStoreError('credential_invalid') from None

    def write(self, account_id: str, purpose: str, value: Mapping[str, Any]) -> dict[str, Any]:
        account_id = _uuid(account_id)
        text = self._serialize(value, purpose)
        ref = {'kind': 'runtime_secret', 'version': 1, 'id': str(uuid.uuid4()), 'purpose': purpose,
               'backend': self.backend_name, 'installation_id': self.installation_id, 'account_id': account_id}
        service, name = self._key(ref, account_id=account_id, purpose=purpose)
        try:
            self._backend.set_password(service, name, text)
        except PermissionError:
            raise CredentialStoreError('credential_permission_denied') from None
        except Exception as error:
            raise _backend_error(error) from None
        return ref

    def consume(self, ref: Mapping[str, Any], *, account_id: str, purpose: str) -> CredentialValue:
        service, name = self._key(ref, account_id=account_id, purpose=purpose)
        try:
            text = self._backend.get_password(service, name)
            if text is None:
                raise CredentialStoreError('credential_missing')
            if not isinstance(text, str) or len(text.encode()) > 64 * 1024:
                raise CredentialStoreError('credential_invalid')
            value = json.loads(text)
            self._serialize(value, purpose)
            return CredentialValue(value)
        except CredentialStoreError:
            raise
        except PermissionError:
            raise CredentialStoreError('credential_permission_denied') from None
        except Exception as error:
            raise _backend_error(error) from None

    def delete(self, ref: Mapping[str, Any], *, account_id: str, purpose: str,
               referenced_ids: frozenset[str]) -> None:
        service, name = self._key(ref, account_id=account_id, purpose=purpose)
        if name in referenced_ids:
            raise CredentialStoreError('credential_in_use')
        try:
            self._backend.delete_password(service, name)
        except PermissionError:
            raise CredentialStoreError('credential_permission_denied') from None
        except Exception as error:
            raise _backend_error(error) from None

    def probe(self) -> str:
        """Whether this backend can hold a credential for this installation now:
        ``available``, ``locked``, ``permission_denied`` or ``unavailable``.

        A random probe value is written under this installation's namespace,
        read back and removed; nothing else in the store is read or listed.
        """
        service, name = f'tess-agent/{self.installation_id}/probe', str(uuid.uuid4())
        value = str(uuid.uuid4())
        try:
            self._backend.set_password(service, name, value)
            try:
                if self._backend.get_password(service, name) != value:
                    return 'unavailable'
            finally:
                self._backend.delete_password(service, name)
            return 'available'
        except Exception as error:
            return {'credential_locked': 'locked', 'credential_permission_denied': 'permission_denied'}.get(
                _backend_error(error).code, 'unavailable')

    async def bounded(self, operation: str, *args, **kwargs):
        """Bound caller wait. OS thread may finish late; immutable new refs only.

        Do not blindly replay timed-out writes or claim OS cancellation; caller
        must surface unknown outcome/reconcile orphan operations before retry.
        """
        if operation not in {'write', 'consume', 'delete', 'probe'}:
            raise CredentialStoreError('credential_invalid')
        try:
            return await asyncio.wait_for(asyncio.to_thread(getattr(self, operation), *args, **kwargs), 5.0)
        except asyncio.TimeoutError:
            raise CredentialStoreError() from None
