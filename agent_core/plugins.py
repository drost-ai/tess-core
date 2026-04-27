from __future__ import annotations

import asyncio
import importlib.util
import os
import shlex
import subprocess
import sys
from typing import Any

from .plugin_contracts import (
    normalizePluginMcpServers,
    parsePluginManifest,
    summarizePluginCapabilities,
    validatePluginCapabilityContract,
    validatePluginManifest,
)
from .storage import loadPlugins, resolveMaybeRelative


async def loadPluginRuntime(config: dict[str, Any]) -> dict[str, Any]:
    sources = await _resolve_plugin_sources(config)
    plugins: list[dict[str, Any]] = []
    errors: list[str] = []

    for source in sources:
        try:
            plugins.append(await _load_plugin(source, config.get("cwd") or os.getcwd()))
        except Exception as exc:
            errors.append(f"{source.get('id')}: {exc}")

    return {
        "plugins": plugins,
        "tools": [tool for plugin in plugins for tool in plugin.get("tools", [])],
        "commands": [command for plugin in plugins for command in plugin.get("commands", [])],
        "mcpServers": [server for plugin in plugins for server in plugin.get("mcpServers", [])],
        "errors": errors,
        "diagnostics": [diagnostic for plugin in plugins for diagnostic in plugin.get("diagnostics", [])],
    }


async def _resolve_plugin_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    cwd = config.get("cwd") or os.getcwd()
    by_id: dict[str, dict[str, Any]] = {}

    for record in await _load_installed_plugins(cwd):
        manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
        plugin_id = manifest.get("id") or "plugin"
        try:
            source = await _resolve_plugin_record_source(record, cwd)
        except Exception:
            source = {"id": plugin_id, "manifest": manifest}
        by_id[str(source.get("id") or plugin_id)] = source

    for plugin_dir in config.get("pluginDirs") or []:
        source = await _resolve_plugin_path(str(plugin_dir), cwd)
        by_id[str(source.get("id"))] = source

    return list(by_id.values())


async def _load_installed_plugins(cwd: str) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for scope in ("user", "project", "local"):
        for record in await loadPlugins(scope, cwd):
            manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {}
            plugin_id = manifest.get("id")
            if not plugin_id:
                continue
            if not record.get("enabled"):
                merged.pop(str(plugin_id), None)
                continue
            merged[str(plugin_id)] = record
    return list(merged.values())


async def _resolve_plugin_record_source(record: dict[str, Any], cwd: str) -> dict[str, Any]:
    manifest = record.get("manifest") if isinstance(record.get("manifest"), dict) else {"id": "plugin", "version": "0.0.0"}
    source = str(record.get("source") or "")
    if not source:
        return {"id": manifest.get("id"), "manifest": manifest}
    if source.startswith("http://") or source.startswith("https://"):
        return {"id": manifest.get("id"), "manifest": manifest}

    candidate_path = resolveMaybeRelative(cwd, source)
    if not os.path.exists(candidate_path):
        return {"id": manifest.get("id"), "manifest": manifest}
    return await _resolve_plugin_path(candidate_path, cwd)


async def _resolve_plugin_path(plugin_path: str, cwd: str) -> dict[str, Any]:
    resolved_path = resolveMaybeRelative(cwd, plugin_path)
    if os.path.isdir(resolved_path):
        manifest_path = _find_first_existing(
            [
                os.path.join(resolved_path, "claude-plugin.json"),
                os.path.join(resolved_path, "plugin.json"),
                os.path.join(resolved_path, "package.json"),
            ]
        )
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = parsePluginManifest(handle.read())
        return {"id": manifest.get("id"), "manifest": manifest, "rootDir": resolved_path, "sourcePath": resolved_path}

    with open(resolved_path, "r", encoding="utf-8") as handle:
        manifest = parsePluginManifest(handle.read())
    return {
        "id": manifest.get("id"),
        "manifest": manifest,
        "rootDir": os.path.dirname(resolved_path),
        "sourcePath": resolved_path,
    }


async def _load_plugin(source: dict[str, Any], cwd: str) -> dict[str, Any]:
    manifest = source.get("manifest") if isinstance(source.get("manifest"), dict) else {}
    manifest_diagnostics = validatePluginManifest(manifest)
    manifest_errors = [diagnostic for diagnostic in manifest_diagnostics if diagnostic.get("severity") == "error"]
    if manifest_errors:
        raise RuntimeError(" ".join(str(diagnostic.get("message")) for diagnostic in manifest_errors))

    manifest_commands = [
        _create_shell_slash_command(str(manifest.get("id")), command.get("name"), command.get("description"), command.get("command"))
        for command in manifest.get("commands") or []
    ]
    manifest_servers = _normalize_mcp_servers(manifest.get("mcpServers") or [])
    module_shape = (
        await _load_plugin_module(str(source.get("rootDir")), manifest, cwd)
        if source.get("rootDir") and manifest.get("entry")
        else None
    )

    tools = _array_value(module_shape, "tools")
    commands = [*manifest_commands, *_array_value(module_shape, "commands")]
    mcp_servers = [*manifest_servers, *_normalize_mcp_servers(_array_value(module_shape, "mcpServers"))]
    capabilities = summarizePluginCapabilities(
        {
            "manifest": manifest,
            "toolCount": len(tools),
            "commandCount": len(commands),
            "mcpServerCount": len(mcp_servers),
        }
    )
    diagnostics = [
        *manifest_diagnostics,
        *validatePluginCapabilityContract({"manifest": manifest, "capabilitySummary": capabilities}),
    ]
    return {
        "id": manifest.get("id"),
        "manifest": manifest,
        "source": source.get("sourcePath"),
        "tools": tools,
        "commands": commands,
        "mcpServers": mcp_servers,
        "capabilities": capabilities,
        "diagnostics": diagnostics,
    }


async def _load_plugin_module(root_dir: str, manifest: dict[str, Any], cwd: str) -> dict[str, Any] | None:
    entry_path = os.path.abspath(os.path.join(root_dir, str(manifest.get("entry"))))
    module_name = f"agent_core_plugin_{abs(hash((entry_path, os.getpid())))}"
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load plugin entry: {entry_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    candidate: Any
    default = getattr(module, "default", None)
    if callable(default):
        candidate = default({"manifest": manifest, "rootDir": root_dir, "cwd": cwd})
        if hasattr(candidate, "__await__"):
            candidate = await candidate
    elif default is not None:
        candidate = default
    elif hasattr(module, "plugin"):
        candidate = getattr(module, "plugin")
    else:
        candidate = {key: getattr(module, key) for key in ("tools", "commands", "mcpServers") if hasattr(module, key)}

    if candidate is None:
        return None
    if isinstance(candidate, dict):
        return candidate
    return {
        key: getattr(candidate, key)
        for key in ("tools", "commands", "mcpServers")
        if hasattr(candidate, key)
    }


def _create_shell_slash_command(plugin_id: str, name: Any, description: Any, command: Any) -> dict[str, Any]:
    normalized_name = str(name or "command").removeprefix("/")
    shell_command = str(command or "")

    async def handler(payload: dict[str, Any]) -> str:
        session = payload.get("session") or {}
        hooks = payload.get("hooks") or {}
        arg_text = str(payload.get("argText") or "")
        built = _build_shell_command(shell_command, arg_text, str(session.get("cwd") or os.getcwd()), str(session.get("id") or ""))
        env = {
            **os.environ,
            "TESS_SESSION_ID": str(session.get("id") or ""),
            "TESS_PLUGIN_ID": plugin_id,
            "TESS_PLUGIN_ARGS": arg_text,
            "TESS_CWD": str(session.get("cwd") or os.getcwd()),
        }
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                _shell_spec(built),
                cwd=str(session.get("cwd") or os.getcwd()),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout or f"exit code {completed.returncode}").strip())
            body = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
            _hook_info(hooks, f"Plugin {plugin_id}", body or f"/{normalized_name} completed with no output.")
        except Exception as exc:
            _hook_error(hooks, f"/{normalized_name} failed: {exc}")
        return "handled"

    return {
        "name": normalized_name,
        "description": description if isinstance(description, str) else f"Run plugin command from {plugin_id}",
        "usage": f"/{normalized_name} [args]",
        "handler": handler,
    }


def _build_shell_command(command: str, arg_text: str, cwd: str, session_id: str) -> str:
    replaced = (
        command.replace("{{args}}", arg_text)
        .replace("{{quoted_args}}", shlex.quote(arg_text))
        .replace("{{cwd}}", shlex.quote(cwd))
        .replace("{{session_id}}", shlex.quote(session_id))
    )
    if not arg_text or "{{args}}" in command or "{{quoted_args}}" in command:
        return replaced
    return f"{replaced} {shlex.quote(arg_text)}".strip()


def _shell_spec(command: str) -> list[str]:
    if os.name == "nt":
        return ["cmd.exe", "/d", "/s", "/c", command]
    return [os.environ.get("SHELL") or "/bin/sh", "-lc", command]


def _normalize_mcp_servers(servers: list[Any]) -> list[dict[str, Any]]:
    return normalizePluginMcpServers([server for server in servers if isinstance(server, dict)])


def _array_value(value: Any, key: str) -> list[Any]:
    if not isinstance(value, dict):
        return []
    candidate = value.get(key)
    return candidate if isinstance(candidate, list) else []


def _find_first_existing(paths: list[str]) -> str:
    for candidate in paths:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("No plugin manifest found.")


def _hook_info(hooks: dict[str, Any], title: str, message: str) -> None:
    fn = hooks.get("info")
    if callable(fn):
        fn(title, message)


def _hook_error(hooks: dict[str, Any], message: str) -> None:
    fn = hooks.get("error")
    if callable(fn):
        fn(message)
