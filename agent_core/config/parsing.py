from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ..storage import file_or_json, load_settings_by_sources
from ..authority.executable_config import parseMcpServerConfigList
from ..authority.executable_source_policy import loadSelectedMcpServers, parseSettingSources
from ..runtime.secrets import assert_no_literal_mcp_secrets

def _parse_setting_sources(raw: str | None) -> list[str]:
    return parseSettingSources(raw)

async def _load_merged_settings(sources: list[str], cwd: str, inline_settings: str | None) -> dict[str, Any]:
    merged = await load_settings_by_sources(sources, cwd)  # type: ignore[arg-type]
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
    existing = [] if strict else await loadSelectedMcpServers(cwd=cwd, sources=sources)
    inline = []
    for item in inputs or []:
        parsed = file_or_json(item)
        records = parseMcpServerConfigList(parsed, "explicitMcpConfig") if parsed else []
        assert_no_literal_mcp_secrets(records, "explicit")
        inline.extend(records)
    return [{**item, "commandOrUrl": os.path.expanduser(item.get("commandOrUrl", ""))} for item in [*existing, *inline]]
