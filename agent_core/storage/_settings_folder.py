from __future__ import annotations

"""Loader for the <runtime-home>/settings/ folder.

The settings folder is the cross-runtime replacement for the monolithic
user-scope settings.json:

    settings/.env            credentials only (KEY=VALUE), feeds the env block
    settings/providers.yaml  provider profiles + provider/model selection
    settings/config.yaml     runtime configuration knobs

All three are optional; the folder augments and overrides the legacy
settings.json record per top-level key (env and providerProfiles merge
per-entry). YAML files use the strict subset documented in
agent_core/config/YAML_SUBSET.md. Malformed content raises immediately —
settings errors must fail loud at startup, never silently degrade.
"""

import os
import re
from typing import Any

from .paths import getStateRoot

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def settingsFolderPath() -> str:
    return os.path.join(getStateRoot(), "settings")


def loadSettingsFolder() -> dict[str, Any] | None:
    """Read the settings folder, or return None when it does not exist."""

    folder = settingsFolderPath()
    if not os.path.isdir(folder):
        return None
    record: dict[str, Any] = {}
    for name in ("config.yaml", "providers.yaml"):
        overlay = _read_yaml_mapping(os.path.join(folder, name))
        if overlay:
            record = mergeSettingsRecords(record, overlay)
    env = _read_env_file(os.path.join(folder, ".env"))
    if env:
        record["env"] = {**(record.get("env") or {}), **env}
    return record


def mergeSettingsRecords(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge two settings records; overlay wins per top-level key.

    ``env`` and ``providerProfiles`` merge per entry instead of being
    replaced wholesale, matching how settings sources already combine in
    config resolution.
    """

    merged = {**base, **overlay}
    for deep_key in ("env", "providerProfiles"):
        base_value = base.get(deep_key)
        overlay_value = overlay.get(deep_key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            merged[deep_key] = {**base_value, **overlay_value}
    return merged


def _read_yaml_mapping(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    # Imported lazily: agent_core.config's package init pulls in config
    # resolution, which imports agent_core.storage — a module-level import
    # here would create a load-order cycle.
    from ..config._yaml import parse_yaml_subset

    parsed = parse_yaml_subset(text, source=path)
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: top-level value must be a mapping.")
    return parsed


def _read_env_file(path: str) -> dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().split("\n")
    except FileNotFoundError:
        return {}
    env: dict[str, str] = {}
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY_RE.match(key):
            raise ValueError(f"{path}:{number}: expected KEY=VALUE.")
        env[key] = _unquote_env_value(value.strip())
    return env


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
