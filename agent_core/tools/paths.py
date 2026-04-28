from __future__ import annotations

import os
from typing import Any

from ..storage import resolveMaybeRelative


def _authorize_path(raw_path: str, context: dict[str, Any], create_allowed: bool = False) -> str:
    return os.path.abspath(resolveMaybeRelative(context["session"]["cwd"], raw_path))


def _display_path(context: dict[str, Any], file_path: str) -> str:
    try:
        return os.path.relpath(file_path, context["session"]["cwd"])
    except Exception:
        return file_path
