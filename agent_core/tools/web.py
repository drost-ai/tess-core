from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import base64
import html
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from .._utils import pretty_json
from ..tool_payloads import shapeTextToolResult
from .cancellation import _create_tool_abort_error, _signal_aborted, _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .schemas import _tool_description, _tool_schema
from .validation import _require_string
from .web_search_api import (
    coerceResultCount,
    configuredAllowedDomains,
    hostnameMatchesRule,
    joinFallbackReasons,
    optionalString,
    resolveSearchApiProvider,
    searchExa,
    resolveSearchProviderPreference,
    webSearchSettings,
)


class FetchResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    async def text(self) -> str:
        return self.body


async def fetch(url: str, *, headers: dict[str, str] | None = None, signal: Any = None) -> FetchResponse:
    if _signal_aborted(signal):
        raise _create_tool_abort_error()
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        if _signal_aborted(signal):
            raise _create_tool_abort_error()
        return FetchResponse(response.status, body, dict(response.headers))


def _facade_fetch():
    facade = sys.modules.get("agent_core.tools")
    current = getattr(facade, "fetch", None)
    return current if callable(current) else fetch


def _web_fetch_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        url = _require_string(input.get("url"), "url")
        prompt = _require_string(input.get("prompt"), "prompt")
        _throw_if_tool_cancelled(context)
        response = await _facade_fetch()(url, headers={"user-agent": "tess/0.1"}, signal=context.get("abortSignal"))
        if not response.ok:
            return {"content": f"Fetch failed with status {response.status}", "isError": True}
        _throw_if_tool_cancelled(context)
        raw = await response.text()
        _throw_if_tool_cancelled(context)
        shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="WebFetch", family="web", text=_strip_html(raw), strategy="head")
        return {"content": pretty_json({"url": url, "prompt": prompt, "content": shaped["text"], "content_manifest": shaped["view"]})}

    return ToolDefinition("WebFetch", _tool_description("WebFetch"), _tool_schema("WebFetch"), handler)


def _web_search_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_query = _require_string(input.get("query"), "query")
        query = normalizeWebSearchQuery(raw_query)
        _throw_if_tool_cancelled(context)
        web_settings = webSearchSettings(context.get("config"))
        configured_allowed = configuredAllowedDomains(web_settings)
        allowed = [item for item in input.get("allowed_domains", []) if isinstance(item, str)]
        blocked = [item for item in input.get("blocked_domains", []) if isinstance(item, str)]
        count = coerceResultCount(input.get("count") or input.get("max_results") or input.get("limit"))
        results = []
        response = await searchWebResponse(
            query,
            context.get("abortSignal"),
            config=context.get("config"),
            count=count,
            country=optionalString(input.get("country")) or "US",
            freshness=optionalString(input.get("freshness")),
            category=optionalString(input.get("category")),
            provider=optionalString(input.get("provider")),
            allowed_domains=allowed,
            blocked_domains=blocked,
        )
        for result in response["results"]:
            _throw_if_tool_cancelled(context)
            hostname = _safe_hostname(result["url"])
            if not hostname:
                continue
            if configured_allowed and not any(hostnameMatchesRule(hostname, domain) for domain in configured_allowed):
                continue
            if allowed and not any(hostnameMatchesRule(hostname, domain) for domain in allowed):
                continue
            if any(hostnameMatchesRule(hostname, domain) for domain in blocked):
                continue
            results.append(_compact_web_result(result))
            if len(results) >= count:
                break
        return {
            "content": pretty_json(
                {
                    "provider": response["provider"],
                    "query": query,
                    "raw_query": raw_query,
                    "fallback_reason": response.get("fallback_reason"),
                    "results": results,
                }
            )
        }

    return ToolDefinition("WebSearch", _tool_description("WebSearch"), _tool_schema("WebSearch"), handler)


async def searchWeb(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    response = await searchWebResponse(query, abortSignal)
    return response["results"]


async def searchWebResponse(
    query: str,
    abortSignal: Any = None,
    *,
    config: dict[str, Any] | None = None,
    count: int = 8,
    country: str = "US",
    freshness: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> dict[str, Any]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    api_provider = resolveSearchApiProvider(config, provider)
    preferred_provider = resolveSearchProviderPreference(config, provider)
    api_fallback_reason = None
    if api_provider:
        try:
            if api_provider["provider"] == "exa":
                exa = await searchExa(
                    query,
                    api_key=api_provider["api_key"],
                    count=count,
                    freshness=freshness,
                    category=category,
                    allowedDomains=allowed_domains,
                    blockedDomains=blocked_domains,
                    abortSignal=abortSignal,
                )
                if exa:
                    return {"provider": "exa", "results": exa, "fallback_reason": None}
                api_fallback_reason = "exa_unavailable" if preferred_provider == "exa" else None
        except Exception as exc:
            api_fallback_reason = "exa_unavailable" if preferred_provider == "exa" else None
    elif preferred_provider == "exa":
        api_fallback_reason = "exa_unavailable"
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    duck = await searchDuckDuckGo(query, abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if duck:
        return {"provider": "scraper", "results": duck, "fallback_reason": api_fallback_reason}
    bing = await searchBing(query, abortSignal)
    if bing:
        return {
            "provider": "scraper",
            "results": bing,
            "fallback_reason": joinFallbackReasons(api_fallback_reason, "duckduckgo_empty"),
        }
    return {
        "provider": "scraper",
        "results": [],
        "fallback_reason": joinFallbackReasons(api_fallback_reason, "scraper_empty"),
    }


async def searchDuckDuckGo(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
    response = await _facade_fetch()(url, headers=_web_search_headers(), signal=abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if not response.ok:
        return []
    raw = await response.text()
    if "anomaly-modal__mask" in raw or "anomaly-modal__modal" in raw:
        return []
    return parseDuckDuckGoResults(raw)


async def searchBing(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    url = f"https://www.bing.com/search?{urlencode({'q': query})}"
    response = await _facade_fetch()(url, headers=_web_search_headers(), signal=abortSignal)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if not response.ok:
        return []
    return parseBingResults(await response.text())


def parseDuckDuckGoResults(raw: str) -> list[dict[str, str]]:
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>')
    out = []
    for match in pattern.finditer(raw):
        url = normalizeDuckDuckGoResultUrl(match.group(1))
        if url:
            out.append({"source": "duckduckgo", "url": url, "title": _strip_html(html.unescape(match.group(2))), "snippet": _strip_html(html.unescape(match.group(3)))})
    return out


def normalizeDuckDuckGoResultUrl(raw: str) -> str | None:
    decoded = html.unescape(raw).strip()
    if not decoded:
        return None
    absolute = f"https:{decoded}" if decoded.startswith("//") else f"https://duckduckgo.com{decoded}" if decoded.startswith("/") else decoded
    try:
        parsed = urlparse(absolute)
        if parsed.hostname == "duckduckgo.com" and parsed.path == "/l/":
            redirected = parse_qs(parsed.query).get("uddg", [None])[0]
            if redirected:
                return redirected
        return urlunparse(parsed)
    except Exception:
        return None


def parseBingResults(raw: str) -> list[dict[str, str]]:
    sections = re.split(r'<li class="b_algo"[^>]*>', raw, flags=re.I)[1:]
    out = []
    for section in sections:
        title_match = re.search(r'<h2[^>]*>[\s\S]*?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?</h2>', section, re.I)
        if not title_match:
            continue
        url = normalizeBingResultUrl(title_match.group(1))
        if not url:
            continue
        snippet_match = re.search(r"<p[^>]*>([\s\S]*?)</p>", section, re.I)
        out.append({"source": "bing", "url": url, "title": _strip_html(html.unescape(title_match.group(2))), "snippet": _strip_html(html.unescape(snippet_match.group(1) if snippet_match else ""))})
    return out


def normalizeBingResultUrl(raw: str) -> str | None:
    decoded = html.unescape(raw).strip()
    if not decoded:
        return None
    try:
        parsed = urlparse(decoded)
        if parsed.hostname == "www.bing.com" and parsed.path == "/ck/a":
            encoded = parse_qs(parsed.query).get("u", [None])[0]
            if encoded:
                target = decodeBingRedirectTarget(encoded)
                if target:
                    return target
        return urlunparse(parsed)
    except Exception:
        return None


def decodeBingRedirectTarget(value: str) -> str | None:
    normalized = value.strip()
    if normalized.startswith("a1"):
        encoded = normalized[2:]
        try:
            padding = "=" * ((4 - len(encoded) % 4) % 4)
            return base64.b64decode(f"{encoded}{padding}").decode("utf-8")
        except Exception:
            return None
    return None


def normalizeWebSearchQuery(query: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\\([\"'])", r"\1", query)).strip()


def _compact_web_result(result: dict[str, str]) -> dict[str, str]:
    payload = {
        "url": result.get("url", ""),
        "title": _truncate_text(result.get("title", ""), 200),
        "snippet": _truncate_text(result.get("snippet", ""), 600),
    }
    if result.get("source"):
        payload["source"] = result.get("source", "")
    return payload


def _truncate_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\u2026"


def _strip_html(raw: str) -> str:
    raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _safe_hostname(value: str) -> str | None:
    try:
        normalized = f"https:{value}" if value.startswith("//") else value
        return urlparse(normalized).hostname
    except Exception:
        return None


def _web_search_headers() -> dict[str, str]:
    return {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36", "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "accept-language": "en-US,en;q=0.9"}


def _header_value(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    if hasattr(headers, "get"):
        return headers.get(name) or headers.get(name.lower()) or headers.get(name.title())
    return None
