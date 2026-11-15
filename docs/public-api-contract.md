# Public API contract

This page states what Tess Core promises to the applications built on it. The list of
names is in the [API reference](reference/api.md); this page is the policy.

## The four entry points

| Import from | For |
|---|---|
| `tess_core` | Everything an embedding application needs |
| `tess_core.shell_surface` | Applications that build an interactive shell around the runtime |
| `tess_core.platform_support` | Operating-system support: the daemon, sandboxing, diagnostics |
| `tess_core.testing` | Internals exposed for integration tests |

A name exported by one of these is public. Anything imported from a deeper module path is
internal and can change in any release, with the exceptions listed below.

## Supported module paths

A few capabilities are optional or sit beside the main surface, and are imported from
their own modules. These paths are supported:

| Module | Names |
|---|---|
| `tess_core.config` | `ProviderRegistry`, `parse_yaml_subset`, `dump_yaml_subset`, `YamlSubsetError` |
| `tess_core.providers.connection` | `check_provider_connection` |
| `tess_core.providers.chatgpt_account` | `SignIn`, `Grant`, `new_host_id`, `store_grant`, `current_access_token`, `revoke`, `ChatGPTAccountError` (needs the `chatgpt-account` extra) |
| `tess_core.runtime.credentials` | `CredentialStore`, `CredentialStoreError`, `CredentialBackend`, `desktop_backend` |
| `tess_core.runtime.encrypted_credentials` | `UnlockSecret`, `CredentialFileInvalid`, `KeyringLocked` (needs the `encrypted-credentials` extra) |
| `tess_core.runtime.secrets` | `resolve_runtime_secret` |
| `tess_core.tools.web_search_api` | `check_exa_connection` |

## What is stable

- **Names and call signatures** on the public surface. A name is removed or changed
  incompatibly only in a release that says so, after a release in which it was marked
  deprecated.
- **Stored formats.** Session, task, monitor and scheduler records written by one version
  are read by later versions. When a format changes, the runtime migrates it or reads
  both.
- **Event names and their documented fields.** New events and new fields are added; your
  code should ignore what it does not recognise.
- **Settings.** A setting is renamed or removed only with a deprecation period. Because
  unknown settings are fatal at start, a removal can never go unnoticed.

## What is not

- Internal modules, and private names beginning with an underscore.
- `tess_core.testing`. It exists so tests can reach internals, and follows them.
- The exact text of prompts, tool descriptions and error messages.
- Undocumented fields in events and stored records.

## How it is enforced

The contract is a file in the repository, `tests/fixtures/public_api/contract.json`, and a
test compares it with the package on every run:

- `tess_core.__all__` must equal the active names of the root entry point, exactly. A name
  cannot be added or removed without editing the contract in the same change.
- The other entry points must export at least their contracted names.

Adding to the surface is therefore a deliberate, reviewed act, and removing from it cannot
happen by accident.

## Naming

Tess Core began as a port, and part of the surface still uses camelCase names and
dictionary-shaped arguments from that history. These are being moved to idiomatic Python
names. Each rename keeps the old name working for a deprecation period.

## Versions

Tess Core follows semantic versioning. Before 1.0, a minor release may contain breaking
changes; each is listed in the release notes with what to change.

## Reference

The [API reference](reference/api.md) is generated from the contract file and the live
package by `scripts/generate_reference_docs.py`.
