from __future__ import annotations

from typing import Any


class _MonitorBootstrapBaseline:
    """Suppress narration for adopted monitors until something changes."""

    def __init__(self) -> None:
        self._ids: set[str] | None = None
        self._observations: dict[str, tuple[Any, int, Any]] = {}

    def begin(self, monitors: list[dict[str, Any]]) -> bool:
        if self._ids is not None:
            return False
        self._ids = {
            monitor["id"]
            for monitor in monitors
            if isinstance(monitor.get("id"), str) and monitor.get("id")
        }
        return True

    def includes(self, monitor_id: Any) -> bool:
        return isinstance(monitor_id, str) and monitor_id in (self._ids or set())

    def resolve_reason(
        self,
        monitor: dict[str, Any],
        observation: tuple[Any, int, Any],
        reason: str | None,
        bootstrap: bool,
    ) -> str | None:
        monitor_id = monitor.get("id") if isinstance(monitor.get("id"), str) else ""
        if bootstrap:
            self._observations[monitor_id] = observation
            return None
        baseline = self._observations.get(monitor_id)
        if baseline is None or monitor.get("lastAssistantUpdateAt"):
            return reason
        if observation == baseline:
            return None
        final_status, output_size, task_status = observation
        if final_status in {"completed", "failed", "stopped"}:
            return final_status
        return "progress" if output_size > baseline[1] or task_status != baseline[2] else None

    def narrated(self, monitor_id: Any) -> None:
        if isinstance(monitor_id, str):
            self._observations.pop(monitor_id, None)

    def reset(self) -> None:
        self._ids = None
        self._observations.clear()
