from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from .._utils import now_iso
from ..storage import getStateRoot
from .commands import _parse_json_object, _runner_which, quoteShellArg
from .shared import DEFAULT_CHROME_EXTENSION_ORIGIN, DEFAULT_CHROME_NATIVE_HOST_NAME

async def resolveChromeCompanionServer(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    status = await detectChromeCompanionStatus(args, **kwargs)
    enabled = bool(status.get("supported"))
    server = (
        {
            "name": "chrome_companion",
            "transport": "stdio",
            "commandOrUrl": sys.executable,
            "args": ["-m", "agent_core.platform_support.chrome_companion", "mcp"],
            "updatedAt": now_iso(),
        }
        if enabled
        else None
    )
    return {"available": bool(status.get("supported") and status.get("browserBinary")), "enabled": enabled, "status": status, "server": server}

async def detectChromeCompanionStatus(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    system = args.get("platform") or sys.platform
    browser = await _find_chrome_binary(runner, system)
    supported = system.startswith("darwin") or system.startswith("linux") or system.startswith("win")
    status = {"enabled": bool(browser), "supported": supported, "browserBinary": browser, "nativeHostModeSupported": supported, "platform": system}
    if args.get("includeNativeHostManifest"):
        native_host = await inspectChromeNativeHostManifest({**args, "platform": system})
        status["nativeHostManifest"] = native_host
        status["nativeHostInstalled"] = native_host.get("installed")
    return status

def getChromeNativeHostManifestPath(args: dict[str, Any] | None = None, **kwargs: Any) -> str | None:
    args = {**(args or {}), **kwargs}
    if args.get("manifestPath"):
        return str(args["manifestPath"])
    system = args.get("platform") or sys.platform
    host_name = args.get("hostName") or DEFAULT_CHROME_NATIVE_HOST_NAME
    home = Path(args.get("home") or Path.home())
    if system.startswith("darwin"):
        return str(home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / f"{host_name}.json")
    if system.startswith("linux"):
        return str(home / ".config" / "google-chrome" / "NativeMessagingHosts" / f"{host_name}.json")
    if system.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(args.get("home") or local) if args.get("home") or local else None
        return str(base / "Google" / "Chrome" / "NativeMessagingHosts" / f"{host_name}.json") if base else None
    return None

async def buildChromeNativeHostManifest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    host_path = args.get("nativeHostPath") or await _chrome_native_host_wrapper_path(args)
    origins = args.get("allowedOrigins") or args.get("allowed_origins") or [DEFAULT_CHROME_EXTENSION_ORIGIN]
    return {
        "name": args.get("hostName") or DEFAULT_CHROME_NATIVE_HOST_NAME,
        "description": args.get("description") or "Tess local Chrome companion native host.",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [str(origin) for origin in origins if str(origin).strip()],
    }

async def inspectChromeNativeHostManifest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    system = args.get("platform") or sys.platform
    supported = system.startswith("darwin") or system.startswith("linux") or system.startswith("win")
    manifest_path = getChromeNativeHostManifestPath(args)
    expected = await buildChromeNativeHostManifest(args)
    manifest = None
    issues: list[str] = []
    if not supported:
        issues.append(f"Unsupported platform: {system}")
    if not manifest_path:
        issues.append("No native-host manifest path is available.")
    elif os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except Exception as exc:
            issues.append(f"Manifest is not valid JSON: {exc}")
    else:
        issues.append("Native-host manifest is not installed.")
    if isinstance(manifest, dict):
        if manifest.get("name") != expected.get("name"):
            issues.append("Manifest host name does not match.")
        if manifest.get("type") != "stdio":
            issues.append("Manifest type must be stdio.")
        if not isinstance(manifest.get("allowed_origins"), list) or not manifest.get("allowed_origins"):
            issues.append("Manifest has no allowed_origins.")
        host_path = manifest.get("path")
        if not isinstance(host_path, str) or not host_path:
            issues.append("Manifest has no native host path.")
        elif not os.path.exists(host_path):
            issues.append("Native host executable is missing.")
    installed = bool(supported and manifest_path and isinstance(manifest, dict) and not issues)
    return {
        "supported": supported,
        "installed": installed,
        "manifestPath": manifest_path,
        "manifestExists": bool(manifest_path and os.path.exists(manifest_path)),
        "hostPath": (manifest or expected).get("path") if isinstance(manifest or expected, dict) else None,
        "hostExists": bool((manifest or expected).get("path") and os.path.exists((manifest or expected).get("path"))) if isinstance(manifest or expected, dict) else False,
        "manifest": manifest,
        "expectedManifest": expected,
        "issues": issues,
        "platform": system,
    }

async def installChromeNativeHostManifest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    status = await inspectChromeNativeHostManifest(args)
    if not status.get("supported") or not status.get("manifestPath"):
        return {**status, "status": "skipped", "reason": "; ".join(status.get("issues") or ["Native-host manifests are not supported."])}
    manifest = status["expectedManifest"]
    host_path = manifest["path"]
    if args.get("dryRun"):
        return {**status, "status": "planned", "manifest": manifest}
    if not args.get("nativeHostPath"):
        await _write_chrome_native_host_wrapper(host_path)
    os.makedirs(os.path.dirname(status["manifestPath"]), exist_ok=True)
    with open(status["manifestPath"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    installed = await inspectChromeNativeHostManifest({**args, "nativeHostPath": host_path})
    return {**installed, "status": "installed" if installed.get("installed") else "degraded", "manifest": manifest}

async def runChromeCompanionMcpServer(input_stream: Any | None = None, output_stream: Any | None = None) -> None:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    buffer = b""
    while True:
        chunk = await asyncio.to_thread(_read_stream_chunk, input_stream)
        if not chunk:
            return
        buffer += chunk
        while buffer:
            parsed = _try_read_rpc_message(buffer)
            if not parsed:
                break
            payload, buffer = parsed
            response = await handleChromeCompanionRpcPayload(payload)
            if response is not None:
                _write_rpc_message(output_stream, response)

async def runChromeNativeHost(input_stream: Any | None = None, output_stream: Any | None = None) -> None:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    while True:
        header = await asyncio.to_thread(_read_exact_stream, input_stream, 4)
        if not header:
            return
        length = int.from_bytes(header, "little")
        payload_bytes = await asyncio.to_thread(_read_exact_stream, input_stream, length)
        if payload_bytes is None:
            return
        payload = _parse_json_object(payload_bytes.decode("utf-8")) or {}
        response = {"type": "pong", "version": "0.1.0"} if payload.get("type") == "ping" else {"type": "status", "status": await detectChromeCompanionStatus()}
        _write_native_host_message(output_stream, response)

async def handleChromeCompanionRpcPayload(payload: str) -> dict[str, Any] | None:
    value = _parse_json_object(payload)
    if not value:
        return None
    method = value.get("method") if isinstance(value.get("method"), str) else ""
    request_id = value.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "tess-chrome-companion", "version": "0.1.0"}, "capabilities": {"tools": {}, "resources": {}}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "chrome_status", "description": "Return local Chrome companion status and browser availability.", "inputSchema": {"type": "object", "properties": {}}}]}}
    if method == "tools/call":
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        name = params.get("name") if isinstance(params.get("name"), str) else ""
        if name != "chrome_status":
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(await detectChromeCompanionStatus(), indent=2)}]}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": [{"uri": "tess://chrome/status", "name": "chrome_status", "mimeType": "application/json", "description": "Current local Chrome companion status."}]}}
    if method == "resources/read":
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        uri = params.get("uri") if isinstance(params.get("uri"), str) else ""
        if uri != "tess://chrome/status":
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown resource: {uri}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(await detectChromeCompanionStatus(), indent=2)}]}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not implemented: {method}"}}

async def _chrome_native_host_wrapper_path(args: dict[str, Any]) -> str:
    system = args.get("platform") or sys.platform
    suffix = ".cmd" if system.startswith("win") else ""
    root = Path(args.get("stateRoot") or getStateRoot()) / "chrome-native-host"
    return str(root / f"{args.get('hostName') or DEFAULT_CHROME_NATIVE_HOST_NAME}{suffix}")

async def _write_chrome_native_host_wrapper(host_path: str) -> None:
    os.makedirs(os.path.dirname(host_path), exist_ok=True)
    if host_path.endswith(".cmd"):
        body = f'@echo off\r\n"{sys.executable}" -m agent_core.platform_support.chrome_companion native %*\r\n'
    else:
        body = f'#!/bin/sh\nexec {quoteShellArg(sys.executable)} -m agent_core.platform_support.chrome_companion native "$@"\n'
    with open(host_path, "w", encoding="utf-8", newline="" if host_path.endswith(".cmd") else None) as handle:
        handle.write(body)
    if not host_path.endswith(".cmd"):
        current = os.stat(host_path).st_mode
        os.chmod(host_path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def _read_stream_chunk(stream: Any) -> bytes:
    reader = getattr(stream, "read1", None)
    chunk = reader(4096) if callable(reader) else stream.read(4096)
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    return chunk or b""

def _read_exact_stream(stream: Any, size: int) -> bytes | None:
    buffer = b""
    while len(buffer) < size:
        chunk = stream.read(size - len(buffer))
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not chunk:
            return None
        buffer += chunk
    return buffer

def _try_read_rpc_message(buffer: bytes) -> tuple[str, bytes] | None:
    newline = buffer.find(b"\n")
    if newline == -1:
        return None
    line = buffer[:newline]
    if line.endswith(b"\r"):
        line = line[:-1]
    return line.decode("utf-8"), buffer[newline + 1 :]

def _write_rpc_message(stream: Any, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value).encode("utf-8") + b"\n")
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()

def _write_native_host_message(stream: Any, message: dict[str, Any]) -> None:
    payload = json.dumps(message).encode("utf-8")
    stream.write(len(payload).to_bytes(4, "little") + payload)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()

async def _find_chrome_binary(runner: dict[str, Any], system: str) -> str | None:
    commands = ("chrome", "chrome.exe") if system.startswith("win") else ("google-chrome", "chromium", "chrome", "google-chrome-stable", "chromium-browser")
    for command in commands:
        found = await _runner_which(runner, command)
        if found:
            return found
    if system.startswith("darwin"):
        app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(app):
            return app
    return None
