"""Shared TERM→grace→KILL→verify ladder for owned worker processes.

Every path that force-terminates an owned process — launch reconciliation,
supervisor shutdown, daemon containment — escalates through the same
ladder so a wedged child cannot survive: SIGTERM, wait the term grace,
SIGKILL, wait the kill grace, then report what actually happened. Grace
windows are runtime knobs (process.term_grace_ms / process.kill_grace_ms).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any

from .identity import (
    ProcessIdentityError,
    signalOwnedProcess,
    verifyOwnedProcessIdentity,
)

_POLL_INTERVAL_SECONDS = 0.02


def _grace_ms(explicit: int | None, knob_name: str) -> int:
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    # Call-time import: the config package imports process helpers at load.
    from ..config._knobs import knobValue

    return int(knobValue(knob_name))


async def drain_owned_process(
    identity: dict[str, Any],
    *,
    term_grace_ms: int | None = None,
    kill_grace_ms: int | None = None,
) -> str | None:
    """Identity-verified ladder. Returns the identity failure code once the
    process is gone (its natural "it is dead" signal), ``process.not_found``
    if the pid vanished mid-signal, or ``process.drain_timeout`` if the
    process survived SIGKILL."""
    try:
        await signalOwnedProcess(identity, signal.SIGTERM)
    except ProcessIdentityError as exc:
        return exc.code
    except OSError:
        return "process.not_found"
    outcome = await _await_identity_death(identity, _grace_ms(term_grace_ms, "process.term_grace_ms"))
    if outcome is not None:
        return outcome
    try:
        await signalOwnedProcess(identity, signal.SIGKILL)
    except ProcessIdentityError as exc:
        return exc.code
    except OSError:
        return "process.not_found"
    outcome = await _await_identity_death(identity, _grace_ms(kill_grace_ms, "process.kill_grace_ms"))
    if outcome is not None:
        return outcome
    return "process.drain_timeout"


async def _await_identity_death(identity: dict[str, Any], grace_ms: int) -> str | None:
    for _ in range(max(1, int(grace_ms / 1000 / _POLL_INTERVAL_SECONDS))):
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            await verifyOwnedProcessIdentity(identity)
        except ProcessIdentityError as exc:
            return exc.code
    return None


async def terminate_popen_group(
    proc: subprocess.Popen[Any],
    *,
    term_grace_ms: int | None = None,
    kill_grace_ms: int | None = None,
) -> str:
    """Ladder for a supervisor-owned Popen and its process group.

    Returns ``already_exited``, ``terminated`` (died within the SIGTERM
    grace), ``killed`` (needed SIGKILL), or ``survived`` (still alive after
    SIGKILL — pathological, reported so callers can surface it)."""
    if proc.poll() is not None:
        return "already_exited"
    _signal_process_group(proc.pid, signal.SIGTERM)
    if await _await_popen_death(proc, _grace_ms(term_grace_ms, "process.term_grace_ms")):
        return "terminated"
    _signal_process_group(proc.pid, signal.SIGKILL)
    if await _await_popen_death(proc, _grace_ms(kill_grace_ms, "process.kill_grace_ms")):
        return "killed"
    return "survived"


async def _await_popen_death(proc: subprocess.Popen[Any], grace_ms: int) -> bool:
    for _ in range(max(1, int(grace_ms / 1000 / _POLL_INTERVAL_SECONDS))):
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        if proc.poll() is not None:
            return True
    return proc.poll() is not None


def _signal_process_group(pid: int, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), signum)
            return
        os.kill(pid, signum)
