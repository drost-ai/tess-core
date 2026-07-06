from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cancellation import _create_tool_abort_error, _signal_aborted

FetchCallable = Callable[..., Awaitable[Any]]


async def searchBrave(
    query: str,
    *,
    api_key: str,
    fetch_func: FetchCallable,
    count: int = 8,
    country: str = "US",
    freshness: str | None = None,
    abortSignal: Any = None,
) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    params = {"q": query, "count": str(max(1, min(10, int(count)))), "country": str(country or "US").upper()[:2] or "US"}
    if freshness:
        params["freshness"] = freshness
    url = f"https://api.search.brave.com/res/v1/web/search?{urlencode(params)}"
    response = await fetch_func(url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"}, signal=abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if not response.ok:
        raise RuntimeError(f"Brave Search returned status {response.status}")
    payload = json.loads(await response.text())
    web = payload.get("web") if isinstance(payload, dict) else None
    raw_results = web.get("results") if isinstance(web, dict) else []
    out: list[dict[str, str]] = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        url_value = str(item.get("url") or "").strip()
        if not url_value:
            continue
        out.append(
            {
                "url": url_value,
                "title": str(item.get("title") or url_value),
                "snippet": str(item.get("description") or item.get("snippet") or ""),
            }
        )
    return out


async def searchExa(
    query: str,
    *,
    api_key: str,
    count: int = 8,
    freshness: str | None = None,
    category: str | None = None,
    abortSignal: Any = None,
) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    payload: dict[str, Any] = {
        "query": query,
        "numResults": max(1, min(10, int(count))),
        "type": "auto",
        "contents": {"text": {"maxCharacters": 600}},
    }
    if category:
        payload["category"] = category
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
    with urlopen(req, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
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
                "url": url_value,
                "title": str(item.get("title") or url_value),
                "snippet": _exa_summary(item),
            }
        )
    return out


def resolveSearchApiProvider(config: dict[str, Any] | None, provider: str | None = None) -> dict[str, str] | None:
    configured = normalizeSearchProvider(provider)
    web_settings = webSearchSettings(config)
    if not configured:
        configured = normalizeSearchProvider(
            web_settings.get("provider")
            or configLookup(config, "webSearchProvider", "web_search_provider")
            or os.environ.get("AGENT_CORE_WEB_SEARCH_PROVIDER")
            or os.environ.get("HOST_WEB_SEARCH_PROVIDER")
        )
    if not configured:
        configured = "brave" if searchApiKey("brave", web_settings) else "exa" if searchApiKey("exa", web_settings) else ""
    if configured not in {"brave", "exa"}:
        return None
    api_key = searchApiKey(configured, web_settings)
    if not api_key:
        alternate = "exa" if configured == "brave" else "brave"
        alternate_key = searchApiKey(alternate, web_settings)
        if alternate_key:
            return {"provider": alternate, "api_key": alternate_key}
        return None
    return {"provider": configured, "api_key": api_key}


def searchApiKey(provider: str, web_settings: dict[str, Any]) -> str:
    if provider == "exa":
        env_name = optionalString(web_settings.get("exaApiKeyEnv") or web_settings.get("exa_api_key_env")) or "EXA_API_KEY"
        return os.environ.get(env_name) or os.environ.get("EXA_API_KEY") or ""
    env_name = optionalString(web_settings.get("braveApiKeyEnv") or web_settings.get("brave_api_key_env")) or "BRAVE_API_KEY"
    return os.environ.get(env_name) or os.environ.get("BRAVE_API_KEY") or ""


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


def configuredAllowedDomains(web_settings: dict[str, Any]) -> list[str]:
    raw = web_settings.get("urlAllowlist") or web_settings.get("url_allowlist") or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip().lower() for item in raw if str(item).strip()]


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
    return cleaned if cleaned in {"brave", "exa", "scraper"} else ""


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
