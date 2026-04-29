from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import ScenarioAssertionError
from .sandbox import ScenarioSandbox


def assert_session(session: dict[str, Any] | None, expected: dict[str, Any]) -> None:
    if not session:
        raise ScenarioAssertionError("No current session is available")
    for key, expected_value in expected.items():
        actual = session.get(key)
        if actual != expected_value:
            raise ScenarioAssertionError(f"Expected session.{key} {expected_value!r} but got {actual!r}")


def assert_file_exists(sandbox: ScenarioSandbox, rel_path: str) -> None:
    target = sandbox.workspace / rel_path
    if not target.exists():
        raise ScenarioAssertionError(f"Expected workspace file to exist: {rel_path}")


def assert_observation(observations: dict[str, Any], expected: dict[str, Any]) -> None:
    for path, value in expected.items():
        actual = _get_path(observations, path)
        if actual != value:
            raise ScenarioAssertionError(f"Expected observation {path} {value!r} but got {actual!r}")


def _get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return None
    return current


def validate_workspace_rel_path(rel_path: str) -> str:
    path = Path(rel_path)
    if path.is_absolute() or ".." in path.parts:
        raise ScenarioAssertionError(f"Workspace path escapes sandbox: {rel_path}")
    return rel_path
