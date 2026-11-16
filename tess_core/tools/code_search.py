from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.tools import ToolResult

import asyncio
import math
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .._utils import pretty_json
from ..payloads.cursor import fingerprint_payload_source, resolve_payload_cursor
from ..acquisition.search import build_streaming_search_from_output, run_streaming_search
from ..payloads.search_results import build_streaming_glob_payload, build_streaming_grep_payload
from ..payloads.tools import (
    apply_default_glob_excludes,
    filter_default_ignored_glob_matches,
    get_tool_payload_policy,
)
from .cancellation import _create_tool_abort_error
from .definitions import ToolDefinition
from .paths import _authorize_path
from .schemas import builtin_tool
from .validation import _require_optional_number, _require_string


def _glob_tool(search: dict[str, Any] | None = None) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        cwd = context.get("cwd") or context["session"]["cwd"]
        query = await _resolve_glob_search(_require_string(input.get("pattern"), "pattern"), input.get("path"), context)
        pattern = query["pattern"]
        search_path = query["searchPath"]
        rg_path, fallback = _search_command(await ((search or {}).get("resolveBinary") or _resolve_ripgrep_binary)())
        if _is_filesystem_root(search_path):
            return {"content": "Glob refuses to search the filesystem root. Provide a narrower path or an absolute glob inside the target directory.", "isError": True}
        args = apply_default_glob_excludes(["--files", "--sort", "path", "-g", pattern])
        args.append(search_path)
        policy = get_tool_payload_policy(context["config"])
        source_fingerprint = fingerprint_payload_source(["glob", cwd, *args])
        cursor = resolve_payload_cursor(
            family="glob",
            sourceFingerprint=source_fingerprint,
            cursor=input.get("cursor"),
            legacyOffset=_optional_offset(input.get("offset")) if "offset" in input else None,
            legacyBase=0,
        )
        _require_search_record_cursor(cursor, "Glob")
        offset = cursor["recordOffset"]
        limit = min(
            _normalize_positive_limit(_require_optional_number(input.get("limit")), policy["globInlineItems"]),
            policy["globInlineItems"],
        )
        acquisition = await _run_search(
            search,
            binary=rg_path,
            argv=[*fallback, *args],
            cwd=cwd,
            sessionId=context["session"]["id"],
            toolName="Glob",
            family="glob",
            contentType="paths",
            offset=offset,
            sampleItems=limit,
            sampleBytes=policy["globInlineBytes"],
            accept=lambda record: bool(filter_default_ignored_glob_matches([_below_search_root(record, search_path)])),
            abortSignal=context.get("abortSignal"),
        )
        if acquisition["aborted"]:
            raise _create_tool_abort_error()
        _require_matching_search_snapshot(cursor, acquisition["resultFingerprint"], "Glob")
        failure = _build_search_process_failure("Glob", acquisition)
        if failure:
            return failure
        payload = build_streaming_glob_payload(
            pattern=pattern,
            searchPath=search_path,
            offset=offset,
            limit=limit,
            result=acquisition,
            sourceFingerprint=source_fingerprint,
        )
        return {"content": pretty_json(payload)}

    return builtin_tool("Glob", handler)


def _grep_tool(search: dict[str, Any] | None = None) -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = _require_string(input.get("pattern"), "pattern")
        cwd = context.get("cwd") or context["session"]["cwd"]
        search_path = await _authorize_path(_require_string(input.get("path"), "path"), context, "Grep") if input.get("path") else cwd
        rg_path, fallback = _search_command(await ((search or {}).get("resolveBinary") or _resolve_ripgrep_binary)())
        output_mode = input.get("output_mode") if isinstance(input.get("output_mode"), str) else "files_with_matches"
        args = ["--color", "never", "--sort", "path"]
        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        else:
            args.append("-n")
        apply_default_glob_excludes(args)
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
        policy = get_tool_payload_policy(context["config"])
        source_fingerprint = fingerprint_payload_source(["grep", cwd, *args])
        cursor = resolve_payload_cursor(
            family="grep",
            sourceFingerprint=source_fingerprint,
            cursor=input.get("cursor"),
            legacyOffset=_optional_offset(input.get("offset")) if "offset" in input else None,
            legacyBase=0,
        )
        _require_search_record_cursor(cursor, "Grep")
        offset = cursor["recordOffset"]
        requested_head_limit = _require_optional_number(input.get("head_limit"))
        head_limit = min(
            policy["grepInlineMatches"] if requested_head_limit == 0 else _normalize_positive_limit(requested_head_limit, policy["grepInlineMatches"]),
            policy["grepInlineMatches"],
        )
        acquisition = await _run_search(
            search,
            binary=rg_path,
            argv=[*fallback, *args],
            cwd=cwd,
            sessionId=context["session"]["id"],
            toolName="Grep",
            family="grep",
            contentType="text",
            offset=offset,
            sampleItems=head_limit,
            sampleBytes=policy["grepInlineBytes"],
            abortSignal=context.get("abortSignal"),
        )
        if acquisition["aborted"]:
            raise _create_tool_abort_error()
        _require_matching_search_snapshot(cursor, acquisition["resultFingerprint"], "Grep")
        failure = _build_search_process_failure("Grep", acquisition)
        if failure:
            return failure
        payload = build_streaming_grep_payload(
            pattern=pattern,
            searchPath=search_path,
            outputMode=output_mode,
            offset=offset,
            limit=head_limit,
            result=acquisition,
            sourceFingerprint=source_fingerprint,
        )
        return {"content": pretty_json(payload)}

    return builtin_tool("Grep", handler)


def _search_command(rg_path: str | None) -> tuple[str, list[str]]:
    """ripgrep when it is installed; otherwise this package's own search,
    run as a separate process that takes the same arguments."""
    if rg_path:
        return rg_path, []
    return sys.executable, ["-I", "-B", str(Path(__file__).with_name("search_fallback.py"))]


async def _resolve_ripgrep_binary() -> str | None:
    packaged = _packaged_ripgrep_path()
    if packaged and os.path.exists(packaged):
        return packaged
    return shutil.which("rg")


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


async def _run_search(search: dict[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
    """Run a streaming ripgrep search, honoring an injected executor.

    Tests inject execSearch via create_builtin_tools(options={"search": ...});
    its output feeds the same streaming-result builder the real search uses.
    """
    exec_search = (search or {}).get("execSearch")
    if exec_search is None:
        return await run_streaming_search(**kwargs)
    stdout, stderr = await exec_search(kwargs["binary"], kwargs["argv"], cwd=kwargs["cwd"])
    return await build_streaming_search_from_output(stdout=stdout, stderr=stderr, **kwargs)


def _build_search_process_failure(tool_name: str, result: dict[str, Any]) -> ToolResult | None:
    if result["outputLimitExceeded"] or result["timedOut"] or result["exitCode"] in (0, 1):
        return None
    return {
        "content": pretty_json({
            "type": "search_error",
            "tool": tool_name,
            "status": "failed",
            "exitCode": result["exitCode"],
            "exitSignal": result["exitSignal"],
            "stderr": result["stderr"],
        }),
        "isError": True,
    }


def _optional_offset(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return 0
    return max(0, math.floor(value))


def _normalize_positive_limit(value: int | float | None, fallback: int) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return fallback
    return math.floor(value)


def _require_search_record_cursor(cursor: dict[str, Any], tool_name: str) -> None:
    if any(cursor.get(key) is not None for key in ("byteOffset", "sourceByteOffset", "recordBytes")):
        raise ValueError(f"{tool_name} cursors must point to a result-record boundary.")


def _require_matching_search_snapshot(cursor: dict[str, Any], fingerprint: str, tool_name: str) -> None:
    expected = cursor.get("snapshotFingerprint")
    if expected is not None and expected != fingerprint:
        raise ValueError(f"{tool_name} results changed after the cursor was issued; restart pagination.")


async def _resolve_glob_search(pattern: str, requested_path: Any, context: ToolContext) -> dict[str, str]:
    if os.path.isabs(pattern):
        split = _split_absolute_glob_pattern(pattern)
        return {"pattern": split["pattern"], "searchPath": await _authorize_path(split["searchPath"], context, "Glob")}
    return {
        "pattern": pattern,
        "searchPath": await _authorize_path(_require_string(requested_path, "path"), context, "Glob") if requested_path else (context.get("cwd") or context["session"]["cwd"]),
    }


def _below_search_root(record: str, search_path: str) -> str:
    """The part of a match below the directory searched. Ignored directory names
    apply inside the search, never to where the workspace itself happens to live."""
    try:
        relative = os.path.relpath(record, search_path)
    except ValueError:
        return record
    return record if relative.startswith(os.pardir) else relative


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
