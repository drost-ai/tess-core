from __future__ import annotations

import json
import re
from .tokens import as_record, truncate
from typing import Any


def extract_file_path(input: dict[str, Any]) -> str | None:
    for key in ("file_path", "path", "filePath", "target_file"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def extract_command(input: dict[str, Any]) -> str | None:
    for key in ("command", "cmd", "shell_command"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def parse_structured_tool_result(content: str) -> dict[str, str]:
    try:
        parsed = _json_loads_strict(content)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    file_record = as_record(parsed.get("file"))
    file_path = file_record.get("filePath") if file_record and isinstance(file_record.get("filePath"), str) else None
    command = parsed.get("command") if isinstance(parsed.get("command"), str) else None
    exit_code = parsed.get("exitCode") if isinstance(parsed.get("exitCode"), (int, float)) and not isinstance(parsed.get("exitCode"), bool) else None
    stderr = truncate(re.sub(r"\s+", " ", parsed["stderr"]), 180) if isinstance(parsed.get("stderr"), str) else None
    failure = f"command failed ({exit_code}){f': {stderr}' if stderr else ''}" if exit_code and exit_code != 0 else None
    return omit_none({"filePath": file_path, "command": command, "failure": failure})


def empty_relief_summary(reactive_attempt: int = 0) -> dict[str, Any]:
    return {
        "applied": False,
        "selectedMode": "none",
        "appliedModes": [],
        "plannedModes": [],
        "pressureLevel": "low",
        "reactiveAttempt": reactive_attempt,
        "steps": [],
        "preservedRawStartIndex": 0,
        "compactedMessageCount": 0,
        "droppedMessageCount": 0,
        "tokensSavedEstimate": 0,
        "reinjectionLines": [],
        "reinjectionSummary": None,
        "detail": None,
    }


def empty_compaction_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "applied": False,
        "messages": messages,
        "preservedRawStartIndex": 0,
        "compactedMessageCount": 0,
        "droppedMessageCount": 0,
        "tokensSavedEstimate": 0,
        "reinjectionLines": [],
    }


def omit_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _json_loads_strict(content: str) -> Any:
    return json.loads(content, parse_constant=_reject_non_finite_json)


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


