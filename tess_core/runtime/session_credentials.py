"""A session keeps the credentials it was opened with.

A provider profile names the environment variable its credential is read from, and a
session reads it whenever it is loaded: when a host opens it, and again when a turn
reopens it after the runtime unloaded it for being idle. A host may hold a credential in
the environment only while it opens a session, so that nothing the session runs can
read it there. The reopening must not fail for that: it is the runtime's doing, and
the host cannot see it coming.

So the runtime remembers, per session and in memory only, each credential the session
was given from the environment, and reads through that memory. The environment wins
whenever it has a value, which is how a rotated credential reaches a session. The
memory is used only when the variable is gone, and it is dropped when the session ends
or the runtime stops. It is never written to disk.
"""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping


class SessionCredentialEnvironment(Mapping[str, str]):
    """The environment, as one session's credentials are read from it: a variable that
    is no longer set reads as the value this session last read from it."""

    def __init__(self, remembered: dict[str, str]) -> None:
        self._remembered = remembered

    def __getitem__(self, name: str) -> str:
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            self._remembered[name] = value
            return value
        return self._remembered[name]

    def __iter__(self) -> Iterator[str]:
        return iter({*os.environ, *self._remembered})

    def __len__(self) -> int:
        return len({*os.environ, *self._remembered})
