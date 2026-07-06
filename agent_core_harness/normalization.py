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
            normalized = {key: self.normalize(val, (*key_path, str(key))) for key, val in value.items()}
            return self._normalize_dynamic_dict(normalized, key_path)
        if isinstance(value, list):
            normalized_items = [self.normalize(item, (*key_path, str(index))) for index, item in enumerate(value)]
            if key_path and key_path[-1] == "workspace_tree":
                return [item for item in normalized_items if not _is_empty_project_config_dir(item)]
            return normalized_items
        if isinstance(value, tuple):
            return [self.normalize(item, (*key_path, str(index))) for index, item in enumerate(value)]
        if isinstance(value, str):
            return self._normalize_string(value, key_path)
        if isinstance(value, int) and not isinstance(value, bool):
            return self._normalize_int(value, key_path)
        return value

    def _normalize_dynamic_dict(self, value: dict[str, Any], key_path: tuple[str, ...]) -> dict[str, Any]:
        for key in ("git_branch", "gitBranch"):
            if key in value:
                value[key] = "<git-branch>"
        for key in ("git_root", "gitRoot"):
            if key in value:
                value[key] = "<git-root>"
        for key in ("workspace_name", "workspaceName", "remoteTitle"):
            if key in value:
                value[key] = "<workspace-name>"
        for key in ("workspaceId", "workspace_id"):
            if key in value:
                value[key] = "<workspace-id>"
        for key in ("workspaceMode", "workspace_mode"):
            if key in value:
                value[key] = "<workspace-mode>"
        for key in ("workspaceRoot", "workspace_root"):
            if key in value:
                value[key] = "<workspace-root>"
        if _is_estimated_token_record(value):
            for key in (
                "base_input_tokens",
                "input_tokens",
                "remaining_output_tokens",
                "baseInputTokens",
                "inputTokens",
                "remainingOutputTokens",
            ):
                if key in value:
                    value[key] = "<token-estimate>"
        if key_path and key_path[-1] in {"continuity_pressure", "continuityPressure"}:
            value["totalTokenEstimate"] = "<token-estimate>"
            categories = value.get("categories")
            if isinstance(categories, dict):
                for key in categories:
                    categories[key] = "<token-estimate>"
        if key_path and key_path[-1] == "categories" and any("tokens" in key.lower() for key in value):
            for key in value:
                if "tokens" in key.lower():
                    value[key] = "<token-estimate>"
        for key in ("elapsed_ms_since_last_checkpoint", "elapsedMsSinceLastCheckpoint"):
            if key in value:
                value[key] = "<duration-ms>"
        return value

    def _normalize_int(self, value: int, key_path: tuple[str, ...]) -> Any:
        key = key_path[-1].lower() if key_path else ""
        if _is_token_estimate_key(key):
            return "<token-estimate>"
        if key in {"pid", "ppid", "processid", "process_id"} or key.endswith("pid"):
            return self._placeholder(self.pid_map, str(value), "pid")
        if "port" in key and 0 < value <= 65535:
            return self._placeholder(self.port_map, str(value), "port")
        if key == "size" and key_path and key_path[0] in {"logs", "storage_tree", "tool_artifacts", "compaction_artifacts"}:
            return "<size>"
        return value

    def _normalize_string(self, value: str, key_path: tuple[str, ...]) -> str:
        normalized = self._replace_paths(value)
        normalized = normalized.replace(self.sandbox.run_id, "<run-id>")
        if _TIMESTAMP_PATTERN.fullmatch(normalized):
            return "<timestamp>"
        normalized = _TIMESTAMP_PATTERN.sub("<timestamp>", normalized)
        normalized = _EMBEDDED_DURATION_FIELD_PATTERN.sub(lambda match: f'{match.group(1)}"<duration-ms>"', normalized)
        normalized = _EMBEDDED_WORKSPACE_NAME_FIELD_PATTERN.sub(lambda match: f'{match.group(1)}"<workspace-name>"', normalized)
        normalized = _EMBEDDED_GIT_ROOT_FIELD_PATTERN.sub(lambda match: f'{match.group(1)}"<git-root>"', normalized)
        normalized = _EMBEDDED_GIT_BRANCH_FIELD_PATTERN.sub(lambda match: f'{match.group(1)}"<git-branch>"', normalized)
        normalized = _PYTHON_EXECUTABLE_PATTERN.sub("<python>", normalized)
        normalized = re.sub(r"\bpreflight\s+\d+\s+via\b", "preflight <token-estimate> via", normalized)
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
        replacements = _path_replacements(
            [
                (Path(self.sandbox.home), "<runtime-home>"),
                (Path(self.sandbox.workspace), "<workspace>"),
                (Path(self.sandbox.run_dir), "<run-dir>"),
                (Path(REPO_ROOT), "<repo>"),
                (Path(tempfile.gettempdir()), "<tmp>"),
            ]
        )
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


def _path_replacements(items: list[tuple[Path, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path, marker in items:
        for candidate in (path, path.resolve()):
            key = (str(candidate), marker)
            if key[0] and key not in seen:
                seen.add(key)
                out.append(key)
    return sorted(out, key=lambda item: len(item[0]), reverse=True)


def _is_estimated_token_record(value: dict[str, Any]) -> bool:
    return value.get("source") == "openai_responses_estimate" or value.get("inputTokenSource") == "openai_responses_estimate"


def _is_token_estimate_key(key: str) -> bool:
    normalized = key.replace("_", "")
    return "tokenestimate" in normalized or ("token" in normalized and "estimate" in normalized) or normalized in {
        "rawtokendeltasincelastcheckpoint",
        "rawtokendelta",
    }


_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_EMBEDDED_DURATION_FIELD_PATTERN = re.compile(r'("(?:elapsed_ms_since_last_checkpoint|elapsedMsSinceLastCheckpoint)":)(?:\d+)')
_EMBEDDED_WORKSPACE_NAME_FIELD_PATTERN = re.compile(r'("(?:workspace_name|workspaceName)":)"[^"]*"')
_EMBEDDED_GIT_ROOT_FIELD_PATTERN = re.compile(r'("(?:git_root|gitRoot)":)(?:"[^"]*"|null)')
_EMBEDDED_GIT_BRANCH_FIELD_PATTERN = re.compile(r'("(?:git_branch|gitBranch)":)(?:"[^"]*"|null)')
_UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_PYTHON_EXECUTABLE_PATTERN = re.compile(
    r"(?<![\w<])(?:<repo>/\.venv/bin/python\d*(?:\.\d+)?|/(?:[\w.+@-]+/)*python\d*(?:\.\d+)?)(?![\w.])"
)


def _is_empty_project_config_dir(item: Any) -> bool:
    return isinstance(item, dict) and item.get("path") == ".tess/project" and item.get("type") == "dir" and len(item) == 2
