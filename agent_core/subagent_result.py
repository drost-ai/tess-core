from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from .storage import taskDir, writeJsonFile


def structured_result_path(task_id: str) -> str:
    return os.path.join(taskDir(task_id), "structured-result.json")


def extract_structured_result_json(text: str) -> Any:
    source = (text or "").strip()
    if not source:
        raise ValueError("No JSON result found.")
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", source, re.S | re.I)
    if fenced:
        return json.loads(fenced.group(1).strip())
    start = min([index for index in (source.find("{"), source.find("[")) if index >= 0], default=-1)
    if start < 0:
        raise ValueError("No JSON object or array found.")
    return json.loads(source[start:].strip())


def _schema_errors(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "null": type(None),
        }
        expected = type_map.get(str(expected_type))
        if expected is not None:
            if expected_type == "number" and isinstance(value, bool):
                errors.append(f"{path}: expected number")
            elif expected_type == "integer" and isinstance(value, bool):
                errors.append(f"{path}: expected integer")
            elif not isinstance(value, expected):
                errors.append(f"{path}: expected {expected_type}")
                return errors
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: expected one of {enum!r}")
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(f"{path}.{key}: required")
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, Mapping):
                    errors.extend(_schema_errors(value[key], child_schema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False and isinstance(properties, Mapping):
            allowed = set(properties.keys())
            for key in value:
                if key not in allowed:
                    errors.append(f"{path}.{key}: additional property not allowed")
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def evaluate_subagent_result(text: str, response_schema: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response_schema, Mapping):
        return {"status": "not_configured", "data": None, "errors": []}
    try:
        data = extract_structured_result_json(text)
    except Exception as exc:
        return {"status": "invalid", "data": None, "errors": [str(exc)]}
    errors = _schema_errors(data, response_schema)
    return {"status": "valid" if not errors else "invalid", "data": data, "errors": errors}


async def persist_structured_subagent_result(task_id: str, evaluation: Mapping[str, Any]) -> str:
    path = structured_result_path(task_id)
    await writeJsonFile(path, dict(evaluation))
    return path


evaluateSubagentResult = evaluate_subagent_result
extractStructuredResultJson = extract_structured_result_json
persistStructuredSubagentResult = persist_structured_subagent_result
structuredResultPath = structured_result_path


__all__ = [
    "evaluateSubagentResult",
    "evaluate_subagent_result",
    "extractStructuredResultJson",
    "extract_structured_result_json",
    "persistStructuredSubagentResult",
    "persist_structured_subagent_result",
    "structuredResultPath",
    "structured_result_path",
]
