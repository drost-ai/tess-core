from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScenarioStep:
    type: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    area: str
    modes: list[str]
    setup: dict[str, Any]
    steps: list[ScenarioStep]
    capture: dict[str, Any]
    path: Path | None = None


@dataclass
class StepResult:
    index: int
    type: str
    status: str
    startedAt: str
    finishedAt: str
    observationKeys: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "type": self.type,
            "status": self.status,
            "startedAt": self.startedAt,
            "finishedAt": self.finishedAt,
            "observationKeys": self.observationKeys,
        }
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class ScenarioResult:
    scenarioId: str
    status: str
    runId: str
    runDir: str
    steps: list[StepResult]
    observationsPath: str
    normalizedPath: str
    golden: dict[str, Any] | None = None
    debugDir: str | None = None
    failure: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "scenarioId": self.scenarioId,
            "status": self.status,
            "runId": self.runId,
            "runDir": self.runDir,
            "steps": [step.to_dict() for step in self.steps],
            "observationsPath": self.observationsPath,
            "normalizedPath": self.normalizedPath,
        }
        if self.golden:
            data["golden"] = self.golden
        if self.debugDir:
            data["debugDir"] = self.debugDir
        if self.failure:
            data["failure"] = self.failure
        return data


class ScenarioValidationError(ValueError):
    pass


class ScenarioAssertionError(AssertionError):
    def __init__(self, message: str, *, category: str = "assertion") -> None:
        super().__init__(message)
        self.category = category
