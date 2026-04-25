from __future__ import annotations

from typing import Any

from ._utils import now_iso


def createRuntimeEvent(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    return {"event": args.get("event") or args.get("type"), "type": args.get("event") or args.get("type"), "data": args.get("data") or {}, "createdAt": args.get("createdAt") or now_iso()}
