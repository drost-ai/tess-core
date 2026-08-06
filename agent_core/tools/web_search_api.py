from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.request import Request, urlopen

from ..payloads.bounded_io import readResponseTextBoundedSync
from .cancellation import _create_tool_abort_error, _signal_aborted


EXA_RESPONSE_MAX_BYTES = 2 * 1024 * 1024


async def searchExa(
    query: str,
    *,
    api_key: str,
    count: int = 8,
    freshness: str | None = None,
    category: str | None = None,
    allowedDomains: list[str] | None = None,
    blockedDomains: list[str] | None = None,
    abortSignal: Any = None,
) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    result_limit = max(1, min(10, int(count)))
    payload: dict[str, Any] = {
        "query": query,
        "numResults": result_limit,
        "type": "auto",
        "contents": {"text": {"maxCharacters": 600}},
    }
    if category:
        payload["category"] = category
    include_domains = normalizeSearchDomains(allowedDomains)
    exclude_domains = normalizeSearchDomains(blockedDomains)
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains
    start_published = _exa_start_published_date(freshness)
    if start_published:
        payload["startPublishedDate"] = start_published
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        "https://api.exa.ai/search",
        data=body,
        headers={"x-api-key": api_key, "Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    raw = await asyncio.to_thread(_read_exa_response, req)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    payload_json = json.loads(raw)
    raw_results = payload_json.get("results") if isinstance(payload_json, dict) else []
    out: list[dict[str, str]] = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        url_value = str(item.get("url") or "").strip()
        if not url_value:
            continue
        out.append(
            {
                "source": "exa",
                "url": url_value,
                "title": str(item.get("title") or url_value),
                "snippet": _exa_summary(item),
            }
        )
        if len(out) >= result_limit:
            break
    return out


def _read_exa_response(request: Request) -> str:
    with urlopen(request, timeout=30) as response:
        return readResponseTextBoundedSync(
            response,
            maxBytes=EXA_RESPONSE_MAX_BYTES,
            label="Exa search response",
        )["text"]


def resolveSearchApiProvider(config: dict[str, Any] | None, provider: str | None = None) -> dict[str, str] | None:
    configured = resolveSearchProviderPreference(config, provider)
    if configured == "scraper":
        return None
    api_key = searchApiKey(config)
    if configured == "exa":
        return {"provider": "exa", "api_key": api_key} if api_key else None
    if not configured and api_key:
        return {"provider": "exa", "api_key": api_key}
    return None


def resolveSearchProviderPreference(config: dict[str, Any] | None, provider: str | None = None) -> str:
    configured = normalizeSearchProvider(provider)
    web_settings = webSearchSettings(config)
    if not configured:
        settings_env = settingsEnv(config)
        configured = normalizeSearchProvider(
            os.environ.get("TESS_WEB_SEARCH_PROVIDER")
            or os.environ.get("WEB_SEARCH_PROVIDER")
            or settings_env.get("TESS_WEB_SEARCH_PROVIDER")
            or settings_env.get("WEB_SEARCH_PROVIDER")
            or web_settings.get("provider")
            or configLookup(config, "webSearchProvider", "web_search_provider")
        )
    return configured


def searchApiKey(config: dict[str, Any] | None) -> str:
    web_settings = webSearchSettings(config)
    settings_env = settingsEnv(config)
    env_name = optionalString(web_settings.get("exaApiKeyEnv") or web_settings.get("exa_api_key_env")) or "EXA_API_KEY"
    return os.environ.get(env_name) or os.environ.get("EXA_API_KEY") or settings_env.get(env_name) or settings_env.get("EXA_API_KEY") or ""


def webSearchSettings(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    for key in ("webSearch", "web_search"):
        value = config.get(key)
        if isinstance(value, dict):
            return value
    tools = config.get("tools")
    if isinstance(tools, dict):
        for key in ("webSearch", "web_search"):
            value = tools.get(key)
            if isinstance(value, dict):
                return value
    settings = config.get("settings")
    if isinstance(settings, dict):
        for key in ("webSearch", "web_search"):
            value = settings.get(key)
            if isinstance(value, dict):
                return value
    return {}


def settingsEnv(config: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(config, dict):
        return {}
    value = config.get("settingsEnv") or config.get("settings_env")
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def configuredAllowedDomains(web_settings: dict[str, Any]) -> list[str]:
    raw = web_settings.get("urlAllowlist") or web_settings.get("url_allowlist") or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip().lower() for item in raw if str(item).strip()]


def normalizeSearchDomains(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = normalizeSearchDomain(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def normalizeSearchDomain(value: str) -> str | None:
    trimmed = str(value or "").strip()
    trimmed = re.sub(r"^\*\.", "", trimmed)
    trimmed = re.sub(r"^\.", "", trimmed)
    if not trimmed:
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
        return parsed.hostname or None
    except Exception:
        return trimmed.split("/", 1)[0] or None


def hostnameMatchesRule(hostname: str, domain: str) -> bool:
    host = str(hostname or "").strip().lower()
    rule = str(domain or "").strip().lower()
    if not host or not rule:
        return False
    if rule.startswith("*."):
        return host.endswith(rule[1:])
    return host == rule or host.endswith(f".{rule}")


def configLookup(config: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(config, dict):
        return None
    for key in keys:
        if key in config:
            return config.get(key)
    return None


def normalizeSearchProvider(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned == "exa":
        return "exa"
    if cleaned in {"scraper", "duckduckgo", "bing"}:
        return "scraper"
    return ""


def coerceResultCount(value: Any) -> int:
    try:
        parsed = int(float(value)) if value is not None else 8
    except Exception:
        parsed = 8
    return max(1, min(10, parsed))


def optionalString(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def joinFallbackReasons(*reasons: str | None) -> str | None:
    cleaned = [reason for reason in reasons if isinstance(reason, str) and reason.strip()]
    return ";".join(cleaned) if cleaned else None


def _exa_start_published_date(freshness: str | None) -> str | None:
    days = {"pd": 1, "pw": 7, "pm": 30, "py": 365}.get(str(freshness or "").strip().lower())
    if not days:
        return None
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return start.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _exa_summary(item: dict[str, Any]) -> str:
    highlights = item.get("highlights")
    if isinstance(highlights, list):
        for highlight in highlights:
            text = _sanitize_summary_text(highlight)
            if text:
                return text
    for field in ("summary", "snippet", "text", "description"):
        text = _sanitize_summary_text(item.get(field))
        if text:
            return text
    return ""


def _sanitize_summary_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) > 600:
        return f"{cleaned[:597].rstrip()}..."
    return cleaned
