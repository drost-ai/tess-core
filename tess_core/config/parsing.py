from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ..storage import file_or_json, load_settings_by_sources
from ..authority.executable_config import parse_mcp_server_config_list
from ..authority.executable_source_policy import load_selected_mcp_servers, parse_setting_sources
from ..runtime.secrets import assert_no_literal_mcp_secrets

def _parse_setting_sources(raw: str | None) -> list[str]:
    return parse_setting_sources(raw)

async def _load_merged_settings(sources: list[str], cwd: str, inline_settings: str | None) -> dict[str, Any]:
    merged = await load_settings_by_sources(sources, cwd)  # type: ignore[arg-type]
    if inline_settings and str(inline_settings).lower().endswith((".yaml", ".yml")):
        from pathlib import Path
        from ._yaml import parse_yaml_subset
        try:
            direct = parse_yaml_subset(Path(inline_settings).expanduser().read_text(encoding="utf-8"), source="explicit engine settings") or {}
        except (ValueError, OSError, UnicodeError):
            raise ValueError("Cannot read or parse explicit engine settings YAML") from None
        if not isinstance(direct, dict):
            raise ValueError("Explicit engine settings must be a mapping")
    else:
        direct = file_or_json(inline_settings) or {}
    return {**merged, **direct, "providerProfiles": {**(merged.get("providerProfiles") or {}), **(direct.get("providerProfiles") or {})}}

async def _resolve_api_key_from_helper(settings: dict[str, Any]) -> str | None:
    helper = settings.get("apiKeyHelper")
    if not helper:
        return None
    parts = str(helper).split()
    if not parts:
        return None
    try:
        result = subprocess.run(parts, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        return result.stdout.strip() or None
    except Exception:
        return None

def _parse_custom_headers(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    trimmed = raw.strip()
    if trimmed.startswith("{"):
        parsed = json.loads(trimmed)
        return {str(k): v for k, v in parsed.items() if isinstance(v, str)}
    headers = {}
    for line in re.split(r"\r?\n|;", trimmed):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers

def _parse_tool_list(cli_value: list[str] | None, settings_value: list[str] | None, fallback: list[str]) -> list[str]:
    source = cli_value if cli_value is not None else settings_value if settings_value is not None else fallback
    if len(source) == 1 and source[0] == "":
        return []
    parsed: list[str] = []
    for entry in source:
        parsed.extend(item.strip() for item in re.split(r"[,\s]+", entry) if item.strip())
    if parsed == ["default"]:
        return fallback
    return _dedupe([_strip_tool_pattern(item) for item in parsed])

def _resolve_tool_lists(options: dict[str, Any], settings: dict[str, Any], defaults: list[str]) -> tuple[list[str], list[str], list[str]]:
    """The session's ``tools``, ``allowedTools`` and ``disallowedTools``: options win over settings, then the defaults."""
    tools = _parse_tool_list(options.get("tools"), settings.get("tools"), defaults)
    allowed = _dedupe(_parse_tool_list(options.get("allowedTools"), settings.get("allowedTools"), tools))
    disallowed = _dedupe(_parse_tool_list(options.get("disallowedTools"), settings.get("disallowedTools"), []))
    if not allowed:
        # An empty allow-list leaves nothing enabled. Say so in the list every
        # filter reads, where empty means none rather than unrestricted.
        tools = []
    return tools, allowed, disallowed

def _strip_tool_pattern(value: str) -> str:
    match = re.match(r"^([A-Za-z]+)(?:\(.+\))?$", value)
    return match.group(1) if match else value

def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))

def _parse_inline_agents(raw: str | None) -> dict[str, Any] | None:
    return json.loads(raw) if raw else None

def _parse_json_schema(raw: str | None) -> Any:
    return json.loads(raw) if raw else None

async def _load_mcp_config(cwd: str, inputs: list[str] | None, sources: list[str], strict: bool = False) -> list[dict[str, Any]]:
    existing = [] if strict else await load_selected_mcp_servers(cwd=cwd, sources=sources)
    inline = []
    for item in inputs or []:
        parsed = file_or_json(item)
        records = parse_mcp_server_config_list(parsed, "explicitMcpConfig") if parsed else []
        assert_no_literal_mcp_secrets(records, "explicit")
        inline.extend(records)
    return [{**item, "commandOrUrl": os.path.expanduser(item.get("commandOrUrl", ""))} for item in [*existing, *inline]]
