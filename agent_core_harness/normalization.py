from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from .paths import REPO_ROOT
from .sandbox import ScenarioSandbox


class NormalizationContext:
    def __init__(self, sandbox: ScenarioSandbox) -> None:
        self.sandbox = sandbox
        self.uuid_map: dict[str, str] = {}
        self.pid_map: dict[str, str] = {}
        self.port_map: dict[str, str] = {}

    def normalize(self, value: Any, key_path: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {key: self.normalize(val, (*key_path, str(key))) for key, val in value.items()}
        if isinstance(value, list):
            normalized_items = [self.normalize(item, (*key_path, str(index))) for index, item in enumerate(value)]
            if key_path and key_path[-1] == "workspace_tree":
                return [item for item in normalized_items if not _is_empty_project_config_dir(item)]
            return normalized_items
        if isinstance(value, str):
            return self._normalize_string(value, key_path)
        if isinstance(value, int) and not isinstance(value, bool):
            return self._normalize_int(value, key_path)
        return value

    def _normalize_int(self, value: int, key_path: tuple[str, ...]) -> Any:
        key = key_path[-1].lower() if key_path else ""
        if key in {"pid", "ppid", "processid", "process_id"} or key.endswith("pid"):
            return self._placeholder(self.pid_map, str(value), "pid")
        if "port" in key and 0 < value <= 65535:
            return self._placeholder(self.port_map, str(value), "port")
        if key == "size" and key_path and key_path[0] in {"storage_tree", "logs"}:
            return "<size>"
        return value

    def _normalize_string(self, value: str, key_path: tuple[str, ...]) -> str:
        normalized = self._replace_paths(value)
        normalized = normalized.replace(self.sandbox.run_id, "<run-id>")
        if re.match(r"^\d{4}-\d{2}-\d{2}T", normalized):
            return "<timestamp>"
        normalized = self._replace_labeled_numbers(normalized, r"\b(pid|ppid|process_id|processId)[=:\s]+(\d+)\b", self.pid_map, "pid")
        normalized = self._replace_labeled_numbers(normalized, r"\b(port)[=:\s]+(\d+)\b", self.port_map, "port")
        normalized = self._replace_localhost_ports(normalized)
        normalized = _UUID_PATTERN.sub(lambda match: self._placeholder(self.uuid_map, match.group(0), "uuid"), normalized)
        if key_path and ("port" in key_path[-1].lower()) and normalized.isdigit():
            return self._placeholder(self.port_map, normalized, "port")
        if key_path and key_path[-1].lower().endswith("pid") and normalized.isdigit():
            return self._placeholder(self.pid_map, normalized, "pid")
        return normalized

    def _replace_paths(self, value: str) -> str:
        replacements = [
            (str(self.sandbox.home), "<runtime-home>"),
            (str(self.sandbox.workspace), "<workspace>"),
            (str(self.sandbox.run_dir), "<run-dir>"),
            (str(REPO_ROOT), "<repo>"),
            (tempfile.gettempdir(), "<tmp>"),
        ]
        normalized = value
        for raw, marker in replacements:
            normalized = _replace_path_prefix(normalized, raw, marker)
        return normalized

    def _replace_localhost_ports(self, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            port = match.group("port")
            return f"{match.group('host')}:{self._placeholder(self.port_map, port, 'port')}"

        return re.sub(r"(?P<host>https?://(?:127\.0\.0\.1|localhost|\[::1\])):(?P<port>\d+)", replace, value)

    def _replace_labeled_numbers(self, value: str, pattern: str, mapping: dict[str, str], prefix: str) -> str:
        def replace(match: re.Match[str]) -> str:
            return f"{match.group(1)} {self._placeholder(mapping, match.group(2), prefix)}"

        return re.sub(pattern, replace, value)

    @staticmethod
    def _placeholder(mapping: dict[str, str], raw: str, prefix: str) -> str:
        if raw not in mapping:
            mapping[raw] = f"<{prefix}:{len(mapping) + 1}>"
        return mapping[raw]


def normalize_observations(value: Any, sandbox: ScenarioSandbox) -> Any:
    return NormalizationContext(sandbox).normalize(value)


def _replace_path_prefix(value: str, raw: str, marker: str) -> str:
    if not raw:
        return value
    raw_path = str(Path(raw))
    normalized = value.replace(raw_path, marker)
    return normalized.replace(raw_path.replace("\\", "/"), marker)


_UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def _is_empty_project_config_dir(item: Any) -> bool:
    return isinstance(item, dict) and item.get("path") == ".tess/project" and item.get("type") == "dir" and len(item) == 2
