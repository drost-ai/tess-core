from __future__ import annotations

from ..contracts.context import ToolContext

import os
import tempfile

from ..storage import resolveMaybeRelative
from .permissions import _ensure_path_confinement


async def _authorize_path(raw_path: str, context: ToolContext, tool_name: str) -> str:
    """Resolve a tool-supplied path against the session cwd and confine it to
    the session's allowed roots. Symlinks and dot-segments are resolved before
    the containment check, so a link inside the workspace cannot reach outside
    it; the caller still receives the path shape it asked for."""
    resolved = os.path.abspath(resolveMaybeRelative(context["session"]["cwd"], raw_path))
    canonical = os.path.realpath(resolved)
    if not any(_contained(root, canonical) for root in _confinement_roots(context)):
        await _ensure_path_confinement(context, tool_name, canonical)
    return resolved


def _confinement_roots(context: ToolContext) -> set[str]:
    session = context.get("session") or {}
    metadata = session.get("metadata") or {}
    authority = metadata.get("effectiveAgentAuthority") if isinstance(metadata, dict) else None
    if isinstance(authority, dict) and authority.get("readableRoots"):
        roots = list(authority["readableRoots"])
    else:
        roots = [context.get("cwd") or session.get("cwd"), *(context.get("allowedRoots") or [])]
    # Scratch space and the runtime home are runtime infrastructure every
    # session may touch (task-output recovery files, artifact spill), not
    # authority widenings.
    runtime_home = (context.get("config") or {}).get("runtimeHome")
    if isinstance(runtime_home, str) and runtime_home:
        roots.append(runtime_home)
    roots.extend((tempfile.gettempdir(), "/tmp"))
    return {os.path.realpath(os.path.abspath(root)) for root in roots if isinstance(root, str) and root}


def _contained(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False


def _display_path(context: ToolContext, file_path: str) -> str:
    try:
        return os.path.relpath(file_path, context["session"]["cwd"])
    except Exception:
        return file_path
