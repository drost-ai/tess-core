from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import ScenarioValidationError
from .paths import DEFAULT_RUNS_DIR


@dataclass
class ScenarioSandbox:
    scenario_id: str
    runs_dir: Path = DEFAULT_RUNS_DIR
    cleanup: str = "never"
    run_id: str = field(default_factory=lambda: f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    previous_env: dict[str, str | None] = field(default_factory=dict)
    process_registry: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.run_dir = Path(self.runs_dir) / self.run_id / self.scenario_id
        self.home = self.run_dir / "home"
        self.workspace = self.run_dir / "workspace"
        self.logs = self.run_dir / "logs"
        self.raw = self.run_dir / "raw"
        self.normalized = self.run_dir / "normalized"
        self.debug = self.run_dir / "debug"

    def create(self) -> "ScenarioSandbox":
        for path in (self.home, self.workspace, self.logs, self.raw, self.normalized, self.debug):
            path.mkdir(parents=True, exist_ok=True)
        self._set_env(
            {
                "AGENT_CORE_HOME": str(self.home),
                "AGENT_CORE_HARNESS_RUN_ID": self.run_id,
                "AGENT_CORE_HARNESS_SCENARIO_ID": self.scenario_id,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return self

    def apply_setup(self, setup: dict[str, Any]) -> None:
        workspace_setup = setup.get("workspace") or {}
        for rel_path in workspace_setup.get("directories") or []:
            (self.workspace / rel_path).mkdir(parents=True, exist_ok=True)
        for rel_path, content in (workspace_setup.get("files") or {}).items():
            target = self.workspace / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        env = setup.get("environment") or {}
        if env:
            if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
                raise ScenarioValidationError("setup.environment must be an object of string values")
            self._set_env(env)

    def cleanup_sandbox(self, *, passed: bool) -> None:
        for process in list(self.process_registry):
            popen = process.get("process")
            if popen is not None and getattr(popen, "poll", lambda: None)() is None:
                popen.terminate()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if passed and self.cleanup == "success":
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def paths_dict(self) -> dict[str, str]:
        return {
            "runDir": str(self.run_dir),
            "runtimeHome": str(self.home),
            "workspace": str(self.workspace),
            "logs": str(self.logs),
            "raw": str(self.raw),
            "normalized": str(self.normalized),
            "debug": str(self.debug),
        }

    def _set_env(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            if key not in self.previous_env:
                self.previous_env[key] = os.environ.get(key)
            os.environ[key] = value
