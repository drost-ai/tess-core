"""Interactive remote-session selection: TTY prompts for teleport and resume."""
from __future__ import annotations

import math
import sys
from typing import Any

from ..remote.sessions import findMatchingRemoteSessions, formatRemoteSessionCandidate
from ..storage import list_remote_sessions


async def resolveTeleportSession(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    teleport = args.get("teleport")
    options = args.get("options") or {}
    if not teleport:
        return None
    sessions = await list_remote_sessions()
    if not sessions:
        raise RuntimeError("No remote sessions are available for teleport.")
    if isinstance(teleport, bool):
        if not canPromptForSelection(options):
            raise RuntimeError(
                "`--teleport` without a value requires an interactive terminal or an explicit session ID."
            )
        selected = await promptToPickRemoteSession(sessions, "Select a remote session to resume:")
        if not selected:
            raise RuntimeError("No remote session was selected.")
        return {"remote": selected, "sessionId": selected.get("sessionId")}
    matches = findMatchingRemoteSessions(sessions, str(teleport))
    if not matches:
        raise RuntimeError(f"No remote session matched: {teleport}")
    selected = await resolveRemoteSelection(matches, str(teleport), options)
    if not selected:
        raise RuntimeError("No remote session was selected.")
    return {"remote": selected, "sessionId": selected.get("sessionId")}


def canPromptForSelection(options: dict[str, Any] | None = None) -> bool:
    options = options or {}
    return not options.get("print") and bool(sys.stdin.isatty() and sys.stdout.isatty())


async def resolveRemoteSelection(
    matches: list[dict[str, Any]],
    query: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if len(matches) == 1:
        return matches[0]
    if not canPromptForSelection(options):
        raise RuntimeError(
            "\n".join(
                [
                    f'Multiple remote sessions matched "{query}".',
                    *[f"  {formatRemoteSessionCandidate(remote)}" for remote in matches[:10]],
                ]
            )
        )
    return await promptToPickRemoteSession(matches, f'Select a remote session matching "{query}":')


async def promptToPickRemoteSession(
    remoteSessions: list[dict[str, Any]],
    prompt: str,
) -> dict[str, Any] | None:
    sys.stdout.write(f"\n{prompt}\n")
    for index, remote in enumerate(remoteSessions[:12]):
        sys.stdout.write(f"  {index + 1}. {formatRemoteSessionCandidate(remote)}\n")
    answer = input("choice> ").strip()
    if not answer:
        return None
    numeric = _parse_integer_selection(answer)
    if numeric is not None and 1 <= numeric <= len(remoteSessions):
        return remoteSessions[numeric - 1]
    matches = findMatchingRemoteSessions(remoteSessions, answer)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            "\n".join(
                [
                    f'Multiple remote sessions still matched "{answer}".',
                    *[f"  {formatRemoteSessionCandidate(remote)}" for remote in matches[:10]],
                ]
            )
        )
    raise RuntimeError(f'No remote session matched "{answer}".')


def _parse_integer_selection(value: str) -> int | None:
    try:
        numeric = float(value)
    except ValueError:
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)
