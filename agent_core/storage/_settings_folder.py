from __future__ import annotations

"""Loader for the <runtime-home>/settings/ folder.

The settings folder is the cross-runtime replacement for the monolithic
user-scope settings.json:

    settings/.env                     credentials only (KEY=VALUE), feeds the env block
    settings/providers.yaml           provider profiles + provider/model selection
    settings/config.yaml              runtime configuration knobs
    settings/config/*.yaml            optional per-section fragments of config.yaml
    settings/system_prompt.md         replaces the base system prompt
    settings/system_prompt_append.md  appended after whichever base prompt wins

All are optional; the folder augments and overrides the legacy
settings.json record per top-level key (env and providerProfiles merge
per-entry). YAML files use the strict subset documented in
agent_core/config/YAML_SUBSET.md. Malformed content raises immediately —
settings errors must fail loud at startup, never silently degrade.

Fragment semantics (full contract in agent_core/config/SETTINGS_FOLDER.md):
config.yaml is the base, then settings/config/*.yaml merge in
lexicographic filename order (non-recursive; dotfiles and non-.yaml
ignored; empty files skipped). Mappings deep-merge, but the same setting
defined in two files is a FATAL startup error naming both files — each
setting lives in exactly one file. providers.yaml still wins for its
keys, and env vars remain the only override layer above the folder.
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
    record = _load_config_with_fragments(folder)
    providers = _read_yaml_mapping(os.path.join(folder, "providers.yaml"))
    if providers:
        record = mergeSettingsRecords(record, providers)
    env = _read_env_file(os.path.join(folder, ".env"))
    if env:
        record["env"] = {**(record.get("env") or {}), **env}
    for filename, key in (
        ("system_prompt.md", "systemPromptFile"),
        ("system_prompt_append.md", "systemPromptAppendFile"),
    ):
        prompt = _read_prompt_file(os.path.join(folder, filename))
        if prompt:
            record[key] = prompt
    return record


def _read_prompt_file(path: str) -> str | None:
    """A prompt file's rstripped text; None when absent or whitespace-only."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    return text.rstrip() or None


def _load_config_with_fragments(folder: str) -> dict[str, Any]:
    """config.yaml plus settings/config/*.yaml fragments, strictly merged."""

    base_name = "config.yaml"
    record: dict[str, Any] = {}
    origins: dict[str, str] = {}
    base = _read_yaml_mapping(os.path.join(folder, base_name))
    if base:
        record = base
        _record_leaf_origins(origins, base, base_name)
    fragments_dir = os.path.join(folder, "config")
    if not os.path.isdir(fragments_dir):
        return record
    for name in sorted(os.listdir(fragments_dir)):
        if name.startswith(".") or not name.endswith(".yaml"):
            continue
        overlay = _read_yaml_mapping(os.path.join(fragments_dir, name))
        if not overlay:
            continue
        fragment_name = f"config/{name}"
        _merge_fragment(record, overlay, origins, fragment_name)
        _record_leaf_origins(origins, overlay, fragment_name)
    return record


def _merge_fragment(
    base: dict[str, Any],
    overlay: dict[str, Any],
    origins: dict[str, str],
    fragment_name: str,
    prefix: str = "",
) -> None:
    """Deep-merge one fragment into the combined record.

    Each setting may live in exactly one file: any leaf (or
    section-vs-value mismatch) already present is a fatal conflict naming
    both files — last-wins conf.d semantics were explicitly rejected.
    """

    for key, value in overlay.items():
        path = f"{prefix}{key}"
        if key not in base:
            base[key] = value
            continue
        existing = base[key]
        if isinstance(existing, dict) and isinstance(value, dict):
            _merge_fragment(existing, value, origins, fragment_name, prefix=f"{path}.")
            continue
        owner = origins.get(path) or next(
            (origins[known] for known in origins if known.startswith(f"{path}.")), "config.yaml"
        )
        raise ValueError(
            f"{path} is set in both {owner} and {fragment_name} — each setting may live in exactly one file; remove one."
        )


def _record_leaf_origins(origins: dict[str, str], mapping: dict[str, Any], source: str, prefix: str = "") -> None:
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            _record_leaf_origins(origins, value, source, prefix=f"{path}.")
        else:
            origins.setdefault(path, source)


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
