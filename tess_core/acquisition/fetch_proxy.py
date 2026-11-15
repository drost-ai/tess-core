from __future__ import annotations

from urllib.parse import urlparse


def resolve_environment_proxy_for_url(url: str, env: dict[str, str] | None = None) -> str | None:
    env = env or {}
    scheme = urlparse(url).scheme.lower()
    if scheme == "https":
        return env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("HTTP_PROXY") or env.get("http_proxy")
    if scheme == "http":
        return env.get("HTTP_PROXY") or env.get("http_proxy")
    return None


