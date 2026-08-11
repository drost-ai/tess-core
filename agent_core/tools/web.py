from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import base64
import html
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .._utils import pretty_json
from ..acquisition.bounded_web_fetch import WebFetchPolicyError, boundedWebFetch
from ..authority.permission_rules import findDispatchDenyRule
from ..payloads.tools import shape_text_tool_result
from ..payloads.utf8_bounds import slice_utf8_prefix
from .cancellation import _create_tool_abort_error, _signal_aborted, _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .schemas import builtin_tool
from .validation import _require_string
from .web_search_api import (
    coerce_result_count,
    configured_allowed_domains,
    hostname_matches_rule,
    join_fallback_reasons,
    optional_string,
    resolve_search_api_provider,
    search_exa,
    resolve_search_provider_preference,
    web_search_settings,
)


class FetchResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None, acquisition: dict[str, Any] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.acquisition = acquisition
        self.ok = 200 <= status < 300

    async def text(self) -> str:
        return self.body


async def fetch(url: str, *, headers: dict[str, str] | None = None, signal: Any = None, authorizeHop: Any = None) -> FetchResponse:
    if _signal_aborted(signal):
        raise _create_tool_abort_error()
    result = await boundedWebFetch(url=url, headers=headers or {}, abortSignal=signal, authorizeHop=authorizeHop)
    if _signal_aborted(signal):
        raise _create_tool_abort_error()
    return FetchResponse(int(result.get("statusCode") or 0), result["body"], acquisition=result)


def _web_fetch_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        url = _require_string(input.get("url"), "url")
        prompt = _require_string(input.get("prompt"), "prompt")
        _throw_if_tool_cancelled(context)
        try:
            fetched = await boundedWebFetch(
                url=url,
                abortSignal=context.get("abortSignal"),
                authorizeHop=lambda hop: _authorize_web_fetch_hop(context, hop),
            )
        except Exception as error:
            if _signal_aborted(context.get("abortSignal")):
                raise _create_tool_abort_error()
            message = slice_utf8_prefix(str(error) or "WebFetch failed", 1_000)
            return {
                "content": pretty_json(
                    {
                        "type": "web_fetch_error",
                        "status": "failed",
                        "reason": "policy" if isinstance(error, WebFetchPolicyError) else "request_failed",
                        "message": message,
                    }
                ),
                "isError": True,
            }
        if fetched["outcome"] in {"http_error", "refused", "timeout"}:
            return {
                "content": pretty_json(
                    {
                        "type": "web_fetch_result",
                        "status": fetched["outcome"],
                        "url": fetched["finalUrl"],
                        "statusCode": fetched.get("statusCode"),
                        "reason": fetched.get("reason"),
                        "timeoutPhase": fetched.get("timeoutPhase"),
                        "redirects": fetched["redirects"],
                    }
                ),
                "isError": True,
            }
        _throw_if_tool_cancelled(context)
        shaped = await shape_text_tool_result(
            config=context["config"], sessionId=context["session"]["id"],
            toolName="WebFetch", family="web", text=_strip_html(fetched["body"]),
            sourceTruncated=fetched["truncated"], artifactTruncated=fetched["truncated"],
            artifactSourceBytes=fetched["decodedBytes"], strategy="head",
        )
        return {
            "content": pretty_json(
                {
                    "type": "web_fetch_result",
                    "status": "partial" if fetched["outcome"] == "partial" else "completed",
                    "url": fetched["finalUrl"],
                    "requested_url": fetched["requestedUrl"],
                    "prompt": slice_utf8_prefix(prompt, 2_000),
                    "content": shaped["text"],
                    "content_manifest": shaped["view"],
                    "acquisition": {
                        "compressedBytes": fetched["compressedBytes"],
                        "decodedBytes": fetched["decodedBytes"],
                        "truncated": fetched["truncated"],
                        "reason": fetched.get("reason"),
                        "timeoutPhase": fetched.get("timeoutPhase"),
                    },
                    "redirects": fetched["redirects"],
                }
            )
        }

    return builtin_tool("WebFetch", handler)


def _effective_agent_authority(context: ToolContext) -> dict[str, Any]:
    metadata = context.get("session", {}).get("metadata") or {}
    authority = metadata.get("effectiveAgentAuthority") if isinstance(metadata, dict) else None
    return authority if isinstance(authority, dict) else {}


def _network_disabled(context: ToolContext) -> bool:
    sandbox = _effective_agent_authority(context).get("sandbox") or {}
    configured_sandbox = context.get("config", {}).get("sandbox") or {}
    return sandbox.get("network", configured_sandbox.get("network", True)) is False


async def _authorize_web_fetch_hop(context: ToolContext, url: str, toolNames: tuple[str, ...] = ("WebFetch",)) -> None:
    if _network_disabled(context):
        raise WebFetchPolicyError(
            f"{toolNames[0]} is blocked because this session's network policy is disabled."
        )
    authority = _effective_agent_authority(context)
    for tool_name in toolNames:
        deny = await findDispatchDenyRule(
            {
                "cwd": context["session"]["cwd"],
                "toolName": tool_name,
                "input": {"url": url},
                "settingsSources": context.get("config", {}).get("settingsSources"),
                "inlineSettings": context.get("config", {}).get("inlineSettings"),
                "rules": authority.get("permissionRules"),
                "projectRoot": authority.get("permissionRuleProjectRoot"),
            }
        )
        if deny:
            raise WebFetchPolicyError(
                f"Redirect hop blocked by permission rule {slice_utf8_prefix(deny['raw'], 512)} "
                f"({deny['scope']} settings)."
            )


def _web_search_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_query = _require_string(input.get("query"), "query")
        query = normalize_web_search_query(raw_query)
        _throw_if_tool_cancelled(context)
        if _network_disabled(context):
            return _web_search_policy_error(
                "WebSearch is blocked because this session's network policy is disabled."
            )
        web_settings = web_search_settings(context.get("config"))
        configured_allowed = configured_allowed_domains(web_settings)
        allowed = [item for item in input.get("allowed_domains", []) if isinstance(item, str)]
        blocked = [item for item in input.get("blocked_domains", []) if isinstance(item, str)]
        count = coerce_result_count(input.get("count") or input.get("max_results") or input.get("limit"))
        results = []
        try:
            response = await search_web_response(
                query,
                context.get("abortSignal"),
                config=context.get("config"),
                count=count,
                country=optional_string(input.get("country")) or "US",
                freshness=optional_string(input.get("freshness")),
                category=optional_string(input.get("category")),
                provider=optional_string(input.get("provider")),
                allowed_domains=allowed,
                blocked_domains=blocked,
                authorizeHop=lambda hop: _authorize_web_fetch_hop(context, hop, toolNames=("WebSearch", "WebFetch")),
            )
        except WebFetchPolicyError as error:
            return _web_search_policy_error(str(error) or "WebSearch was blocked by policy.")
        for result in response["results"]:
            _throw_if_tool_cancelled(context)
            hostname = _safe_hostname(result["url"])
            if not hostname:
                continue
            if configured_allowed and not any(hostname_matches_rule(hostname, domain) for domain in configured_allowed):
                continue
            if allowed and not any(hostname_matches_rule(hostname, domain) for domain in allowed):
                continue
            if any(hostname_matches_rule(hostname, domain) for domain in blocked):
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

    return builtin_tool("WebSearch", handler)


def _web_search_policy_error(message: str) -> ToolResult:
    return {
        "content": pretty_json(
            {
                "type": "web_search_error",
                "status": "failed",
                "reason": "policy",
                "message": slice_utf8_prefix(message, 1_000),
            }
        ),
        "isError": True,
    }


async def search_web(query: str, abortSignal: Any = None) -> list[dict[str, str]]:
    response = await search_web_response(query, abortSignal)
    return response["results"]


async def search_web_response(
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
    fetcher: Any = None,
    authorizeHop: Any = None,
) -> dict[str, Any]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    api_provider = resolve_search_api_provider(config, provider)
    preferred_provider = resolve_search_provider_preference(config, provider)
    api_fallback_reason = None
    if api_provider:
        try:
            if api_provider["provider"] == "exa":
                exa = await search_exa(
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
    duck = await search_duck_duck_go(query, abortSignal, fetcher=fetcher, authorizeHop=authorizeHop)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if duck:
        return {"provider": "scraper", "results": duck, "fallback_reason": api_fallback_reason}
    bing = await search_bing(query, abortSignal, fetcher=fetcher, authorizeHop=authorizeHop)
    if bing:
        return {
            "provider": "scraper",
            "results": bing,
            "fallback_reason": join_fallback_reasons(api_fallback_reason, "duckduckgo_empty"),
        }
    return {
        "provider": "scraper",
        "results": [],
        "fallback_reason": join_fallback_reasons(api_fallback_reason, "scraper_empty"),
    }


async def search_duck_duck_go(query: str, abortSignal: Any = None, *, fetcher: Any = None, authorizeHop: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    url = f"https://html.duckduckgo.com/html/?{urlencode({'q': query})}"
    response = await (fetcher or fetch)(url, headers=_web_search_headers(), signal=abortSignal, authorizeHop=authorizeHop)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if not response.ok:
        return []
    raw = await response.text()
    if "anomaly-modal__mask" in raw or "anomaly-modal__modal" in raw:
        return []
    return parse_duck_duck_go_results(raw)


async def search_bing(query: str, abortSignal: Any = None, *, fetcher: Any = None, authorizeHop: Any = None) -> list[dict[str, str]]:
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    url = f"https://www.bing.com/search?{urlencode({'q': query})}"
    response = await (fetcher or fetch)(url, headers=_web_search_headers(), signal=abortSignal, authorizeHop=authorizeHop)
    if _signal_aborted(abortSignal):
        raise _create_tool_abort_error()
    if not response.ok:
        return []
    return parse_bing_results(await response.text())


def parse_duck_duck_go_results(raw: str) -> list[dict[str, str]]:
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>')
    out = []
    for match in pattern.finditer(raw):
        url = normalize_duck_duck_go_result_url(match.group(1))
        if url:
            out.append({"source": "duckduckgo", "url": url, "title": _strip_html(html.unescape(match.group(2))), "snippet": _strip_html(html.unescape(match.group(3)))})
    return out


def normalize_duck_duck_go_result_url(raw: str) -> str | None:
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


def parse_bing_results(raw: str) -> list[dict[str, str]]:
    sections = re.split(r'<li class="b_algo"[^>]*>', raw, flags=re.I)[1:]
    out = []
    for section in sections:
        title_match = re.search(r'<h2[^>]*>[\s\S]*?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>[\s\S]*?</h2>', section, re.I)
        if not title_match:
            continue
        url = normalize_bing_result_url(title_match.group(1))
        if not url:
            continue
        snippet_match = re.search(r"<p[^>]*>([\s\S]*?)</p>", section, re.I)
        out.append({"source": "bing", "url": url, "title": _strip_html(html.unescape(title_match.group(2))), "snippet": _strip_html(html.unescape(snippet_match.group(1) if snippet_match else ""))})
    return out


def normalize_bing_result_url(raw: str) -> str | None:
    decoded = html.unescape(raw).strip()
    if not decoded:
        return None
    try:
        parsed = urlparse(decoded)
        if parsed.hostname == "www.bing.com" and parsed.path == "/ck/a":
            encoded = parse_qs(parsed.query).get("u", [None])[0]
            if encoded:
                target = decode_bing_redirect_target(encoded)
                if target:
                    return target
        return urlunparse(parsed)
    except Exception:
        return None


def decode_bing_redirect_target(value: str) -> str | None:
    normalized = value.strip()
    if normalized.startswith("a1"):
        encoded = normalized[2:]
        try:
            padding = "=" * ((4 - len(encoded) % 4) % 4)
            return base64.b64decode(f"{encoded}{padding}").decode("utf-8")
        except Exception:
            return None
    return None


def normalize_web_search_query(query: str) -> str:
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
