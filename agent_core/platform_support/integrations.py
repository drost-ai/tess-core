from __future__ import annotations

from typing import Any

from .._utils import now_iso

def applyPlatformIntegrationMetadata(
    session: dict[str, Any],
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    metadata = session.setdefault("metadata", {})
    timestamp = args.get("updatedAt") or now_iso()
    tmux = args.get("tmux") or args.get("tmuxResult")
    if isinstance(tmux, dict):
        event = tmux.get("event") if isinstance(tmux.get("event"), dict) else {}
        data = event.get("data") if isinstance(event.get("data"), dict) else tmux.get("data") if isinstance(tmux.get("data"), dict) else {}
        if data.get("tmux_session_name"):
            metadata["tmuxSessionName"] = data.get("tmux_session_name")
        if data.get("tmux_window_name"):
            metadata["tmuxWindowName"] = data.get("tmux_window_name")
        if data.get("mode"):
            metadata["tmuxMode"] = data.get("mode")
        if event.get("event"):
            metadata["tmuxLastEvent"] = event.get("event")
        if tmux.get("handled") is not None:
            metadata["tmuxHandoffHandled"] = bool(tmux.get("handled"))
        metadata["tmuxUpdatedAt"] = timestamp
    ide = args.get("ide") or args.get("ideResult")
    if isinstance(ide, dict):
        target = ide.get("target") if isinstance(ide.get("target"), dict) else {}
        metadata["ideConnectionStatus"] = ide.get("status")
        if target.get("displayName"):
            metadata["ideName"] = target.get("displayName")
        if target.get("id"):
            metadata["ideTargetId"] = target.get("id")
        if target.get("command"):
            metadata["ideCommand"] = target.get("command")
        if target.get("source"):
            metadata["ideSource"] = target.get("source")
        if ide.get("reason"):
            metadata["ideConnectionReason"] = ide.get("reason")
        metadata["ideUpdatedAt"] = timestamp
    chrome = args.get("chrome") or args.get("chromeResult")
    if isinstance(chrome, dict):
        status = chrome.get("status") if isinstance(chrome.get("status"), dict) else chrome
        metadata["chromeIntegrationEnabled"] = bool(chrome.get("enabled") or status.get("enabled") or chrome.get("server"))
        metadata["chromeIntegrationMode"] = "mcp" if chrome.get("server") else metadata.get("chromeIntegrationMode") or ("native" if status.get("nativeHostInstalled") else "status")
        metadata["chromePlatform"] = status.get("platform")
        metadata["chromeBrowserBinary"] = status.get("browserBinary")
        metadata["chromeNativeHostModeSupported"] = status.get("nativeHostModeSupported")
        native_host = status.get("nativeHostManifest") if isinstance(status.get("nativeHostManifest"), dict) else {}
        if native_host.get("manifestPath"):
            metadata["chromeNativeHostManifestPath"] = native_host.get("manifestPath")
        metadata["chromeUpdatedAt"] = timestamp
    return metadata
