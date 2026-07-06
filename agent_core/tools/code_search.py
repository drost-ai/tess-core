from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .._utils import pretty_json
from ..tool_payloads import applyDefaultGlobExcludes, filterDefaultIgnoredGlobMatches, shapeGlobPayload, shapeGrepPayload
from .definitions import ToolDefinition
from .paths import _authorize_path
from .schemas import _tool_description, _tool_schema
from .validation import _require_optional_number, _require_string


def _glob_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        cwd = context.get("cwd") or context["session"]["cwd"]
        query = _resolve_glob_search(_require_string(input.get("pattern"), "pattern"), input.get("path"), context)
        pattern = query["pattern"]
        search_path = query["searchPath"]
        rg_path = await _facade_resolve_ripgrep_binary()
        if not rg_path:
            return {"content": "ripgrep is required for Glob and was not found.", "isError": True}
        if _is_filesystem_root(search_path):
            return {"content": "Glob refuses to search the filesystem root. Provide a narrower path or an absolute glob inside the target directory.", "isError": True}
        args = applyDefaultGlobExcludes(["--files", "-g", pattern])
        args.append(search_path)
        stdout, _stderr = await _facade_exec_ripgrep_allowing_no_matches(rg_path, args, cwd=cwd)
        matches = filterDefaultIgnoredGlobMatches([line for line in stdout.strip().splitlines() if line])
        payload = await shapeGlobPayload(
            config=context["config"],
            sessionId=context["session"]["id"],
            pattern=pattern,
            searchPath=search_path,
            matches=matches,
            offset=int(input.get("offset") or 0),
            requestedLimit=input.get("limit"),
            ignoredDefaultsApplied=True,
        )
        return {"content": pretty_json(payload)}

    return ToolDefinition("Glob", _tool_description("Glob"), _tool_schema("Glob"), handler)


def _grep_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = _require_string(input.get("pattern"), "pattern")
        cwd = context.get("cwd") or context["session"]["cwd"]
        search_path = _authorize_path(_require_string(input.get("path"), "path"), context) if input.get("path") else cwd
        rg_path = await _facade_resolve_ripgrep_binary()
        if not rg_path:
            return {"content": "ripgrep is required for Grep and was not found.", "isError": True}
        output_mode = input.get("output_mode") if isinstance(input.get("output_mode"), str) else "files_with_matches"
        args = ["--color", "never"]
        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        else:
            args.append("-n")
        applyDefaultGlobExcludes(args)
        if input.get("glob"):
            args.extend(["--glob", _require_string(input.get("glob"), "glob")])
        if input.get("type"):
            args.extend(["--type", _require_string(input.get("type"), "type")])
        if input.get("ignore_case") or input.get("-i"):
            args.append("-i")
        context_lines = _require_optional_number(input.get("context"))
        if context_lines is None:
            context_lines = _require_optional_number(input.get("-C"))
        if context_lines is not None:
            args.extend(["-C", str(context_lines)])
        else:
            before = _require_optional_number(input.get("before_context"))
            if before is None:
                before = _require_optional_number(input.get("-B"))
            after = _require_optional_number(input.get("after_context"))
            if after is None:
                after = _require_optional_number(input.get("-A"))
            if after is not None:
                args.extend(["-A", str(after)])
            if before is not None:
                args.extend(["-B", str(before)])
        if input.get("multiline"):
            args.extend(["-U", "--multiline-dotall"])
        args.extend([pattern, search_path])
        stdout, _stderr = await _facade_exec_ripgrep_allowing_no_matches(rg_path, args, cwd=cwd)
        matches = [line for line in stdout.strip().splitlines() if line]
        payload = await shapeGrepPayload(config=context["config"], sessionId=context["session"]["id"], pattern=pattern, searchPath=search_path, outputMode=output_mode, matches=matches, offset=int(input.get("offset") or 0), requestedHeadLimit=input.get("head_limit"))
        return {"content": pretty_json(payload)}

    return ToolDefinition("Grep", _tool_description("Grep"), _tool_schema("Grep"), handler)


async def _resolve_ripgrep_binary() -> str | None:
    packaged = _packaged_ripgrep_path()
    if packaged and os.path.exists(packaged):
        return packaged
    return shutil.which("rg")


async def _facade_resolve_ripgrep_binary() -> str | None:
    facade = sys.modules.get("agent_core.tools")
    current = getattr(facade, "_resolve_ripgrep_binary", None)
    if callable(current) and current is not _resolve_ripgrep_binary:
        return await current()
    return await _resolve_ripgrep_binary()


def _packaged_ripgrep_path() -> str | None:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64" if machine in ("x86_64", "amd64") else machine
    if sys.platform == "darwin":
        key = f"{arch}-darwin"
        binary = "rg"
    elif sys.platform.startswith("linux"):
        key = f"{arch}-linux"
        binary = "rg"
    elif sys.platform == "win32":
        key = f"{arch}-win32"
        binary = "rg.exe"
    else:
        return None
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root / "package" / "vendor" / "ripgrep" / key / binary)


async def _exec_ripgrep_allowing_no_matches(rg_path: str, args: list[str], *, cwd: str) -> tuple[str, str]:
    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([rg_path, *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    result = await asyncio.to_thread(run)
    if result.returncode in (0, 1):
        return result.stdout, result.stderr
    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ripgrep failed with exit code {result.returncode}")


async def _facade_exec_ripgrep_allowing_no_matches(rg_path: str, args: list[str], *, cwd: str) -> tuple[str, str]:
    facade = sys.modules.get("agent_core.tools")
    current = getattr(facade, "_exec_ripgrep_allowing_no_matches", None)
    if callable(current) and current is not _exec_ripgrep_allowing_no_matches:
        return await current(rg_path, args, cwd=cwd)
    return await _exec_ripgrep_allowing_no_matches(rg_path, args, cwd=cwd)


def _resolve_glob_search(pattern: str, requested_path: Any, context: ToolContext) -> dict[str, str]:
    if os.path.isabs(pattern):
        split = _split_absolute_glob_pattern(pattern)
        return {"pattern": split["pattern"], "searchPath": _authorize_path(split["searchPath"], context)}
    return {
        "pattern": pattern,
        "searchPath": _authorize_path(_require_string(requested_path, "path"), context) if requested_path else (context.get("cwd") or context["session"]["cwd"]),
    }


def _split_absolute_glob_pattern(pattern: str) -> dict[str, str]:
    normalized = os.path.normpath(pattern)
    drive, tail = os.path.splitdrive(normalized)
    root = drive + os.path.sep if normalized.startswith(os.path.sep) or drive else os.path.sep
    parts = [part for part in normalized[len(root):].split(os.path.sep) if part]
    first_glob = next((index for index, part in enumerate(parts) if _has_glob_magic(part)), -1)
    if first_glob == -1:
        return {"pattern": os.path.basename(normalized), "searchPath": os.path.dirname(normalized)}
    static_parts = parts[:first_glob]
    glob_parts = parts[first_glob:]
    search_path = os.path.join(root, *static_parts) if static_parts else root
    return {"pattern": "/".join(glob_parts), "searchPath": search_path}


def _has_glob_magic(value: str) -> bool:
    return bool(any(char in value for char in "*?[]{}"))


def _is_filesystem_root(value: str) -> bool:
    resolved = os.path.abspath(value)
    return resolved == os.path.abspath(os.path.sep)
