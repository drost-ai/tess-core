from __future__ import annotations

from typing import Any


async def createCollaborationRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"workers": [], "snapshot": lambda: {"workers": []}, "close": lambda: None}
