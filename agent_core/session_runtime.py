from __future__ import annotations

from typing import Any


def createSessionRuntimeController(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    return {"session": args.get("session"), "runtime": args.get("runtime"), "close": lambda: None}
