from __future__ import annotations

from .shared import *


def extractFilePath(input: dict[str, Any]) -> str | None:
    for key in ("file_path", "path", "filePath", "target_file"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def extractCommand(input: dict[str, Any]) -> str | None:
    for key in ("command", "cmd", "shell_command"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def parseStructuredToolResult(content: str) -> dict[str, str]:
    try:
        parsed = _json_loads_strict(content)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    file_record = asRecord(parsed.get("file"))
    file_path = file_record.get("filePath") if file_record and isinstance(file_record.get("filePath"), str) else None
    command = parsed.get("command") if isinstance(parsed.get("command"), str) else None
    exit_code = parsed.get("exitCode") if isinstance(parsed.get("exitCode"), (int, float)) and not isinstance(parsed.get("exitCode"), bool) else None
    stderr = truncate(re.sub(r"\s+", " ", parsed["stderr"]), 180) if isinstance(parsed.get("stderr"), str) else None
    failure = f"command failed ({exit_code}){f': {stderr}' if stderr else ''}" if exit_code and exit_code != 0 else None
    return omitNone({"filePath": file_path, "command": command, "failure": failure})


def emptyReliefSummary(reactive_attempt: int = 0) -> dict[str, Any]:
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


def emptyCompactionResult(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "applied": False,
        "messages": messages,
        "preservedRawStartIndex": 0,
        "compactedMessageCount": 0,
        "droppedMessageCount": 0,
        "tokensSavedEstimate": 0,
        "reinjectionLines": [],
    }


def omitNone(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _json_loads_strict(content: str) -> Any:
    return json.loads(content, parse_constant=_reject_non_finite_json)


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _tool_result_content_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
