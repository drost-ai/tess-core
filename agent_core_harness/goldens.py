from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from .model import Scenario
from .paths import DEFAULT_GOLDENS_DIR


def update_golden(scenario: Scenario, normalized: dict[str, Any], goldens_dir: str | None = None) -> dict[str, Any]:
    target = _scenario_golden_dir(scenario, goldens_dir)
    families = target / "families"
    target.mkdir(parents=True, exist_ok=True)
    families.mkdir(parents=True, exist_ok=True)
    _write_json(target / "manifest.normalized.json", _manifest_for(scenario), pretty=True)
    _write_json(target / "observations.normalized.json", normalized, pretty=False)
    for key, value in sorted(normalized.items()):
        _write_json(families / f"{key}.json", value, pretty=True)
    return {"status": "updated", "goldenDir": str(target), "expectedPath": str(target / "observations.normalized.json")}


def compare_golden(
    scenario: Scenario,
    normalized: dict[str, Any],
    *,
    goldens_dir: str | None = None,
    diff_path: Path | None = None,
) -> dict[str, Any]:
    target = _scenario_golden_dir(scenario, goldens_dir)
    expected_path = target / "observations.normalized.json"
    if not expected_path.exists():
        return {"status": "missing", "goldenDir": str(target), "expectedPath": str(expected_path), "message": "Golden observations file is missing."}
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    if expected == normalized:
        return {"status": "matched", "goldenDir": str(target), "expectedPath": str(expected_path)}
    diff = render_diff(expected, normalized, fromfile=str(expected_path), tofile="actual observations")
    if diff_path:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff, encoding="utf-8")
    return {
        "status": "mismatched",
        "goldenDir": str(target),
        "expectedPath": str(expected_path),
        "diffPath": str(diff_path) if diff_path else None,
        "message": "Normalized observations differ from golden.",
    }


def render_diff(expected: Any, actual: Any, *, fromfile: str = "expected", tofile: str = "actual") -> str:
    expected_lines = _pretty_json(expected).splitlines(keepends=True)
    actual_lines = _pretty_json(actual).splitlines(keepends=True)
    return "".join(difflib.unified_diff(expected_lines, actual_lines, fromfile=fromfile, tofile=tofile))


def expected_observations(scenario: Scenario, goldens_dir: str | None = None) -> dict[str, Any] | None:
    path = _scenario_golden_dir(scenario, goldens_dir) / "observations.normalized.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _scenario_golden_dir(scenario: Scenario, goldens_dir: str | None) -> Path:
    root = DEFAULT_GOLDENS_DIR if goldens_dir is None else Path(goldens_dir)
    return root / scenario.id


def _manifest_for(scenario: Scenario) -> dict[str, Any]:
    if scenario.path:
        return json.loads(scenario.path.read_text(encoding="utf-8"))
    return {"id": scenario.id, "title": scenario.title, "area": scenario.area, "mode": scenario.modes}


def _write_json(path: Path, value: Any, *, pretty: bool) -> None:
    rendered = _pretty_json(value) if pretty else json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if pretty and len(rendered.splitlines()) > 480:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(rendered, encoding="utf-8")


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
