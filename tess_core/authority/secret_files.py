"""Files that hold credentials by convention.

Reading one is not an ordinary read: it needs approval (or an allow rule) in
every mode but ``bypassPermissions``, whichever tool is used, and a shell
command that names one is never treated as a harmless read-only command.
"""
from __future__ import annotations

import os
import re

_SECRET_DIRECTORIES = {".ssh", ".aws", ".gnupg", ".codex", ".kube", ".docker"}
_SECRET_NAMES = {".netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
_ENV_FILE = re.compile(r"^\.env(?:\.[A-Za-z0-9_.-]+)?$|^[A-Za-z0-9_-]+\.env$")
_ENV_TEMPLATES = (".example", ".sample", ".template", ".dist", ".defaults")


def looks_like_secret_file(path: str) -> bool:
    """Whether a path names a credentials file by its name or its directory."""
    if not isinstance(path, str) or not path:
        return False
    parts = [part for part in path.replace("\\", "/").split("/") if part and part != "."]
    if not parts:
        return False
    name = parts[-1]
    lowered = name.lower()
    if any(part in _SECRET_DIRECTORIES for part in parts[:-1]):
        return True
    if lowered in _SECRET_NAMES or lowered.endswith(_SECRET_SUFFIXES):
        return True
    if _ENV_FILE.match(name) and not lowered.endswith(_ENV_TEMPLATES):
        return True
    return False


def names_a_secret_file(arguments: list[str]) -> bool:
    """Whether any shell argument is, or expands under the home directory to, a credentials file."""
    return any(looks_like_secret_file(os.path.expanduser(argument)) for argument in arguments if not argument.startswith("-"))
