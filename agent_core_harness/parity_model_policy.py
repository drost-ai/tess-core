from __future__ import annotations

from typing import Any


def normalize_compaction_model_policy(value: dict[str, Any]) -> dict[str, Any]:
    for case in value.get("cases") or []:
        if not isinstance(case, dict):
            continue
        if "modelPlan" in case:
            case["modelPlan"] = {
                "configured": {
                    "synthesis": {
                        "effort": "high",
                        "model": "gpt-5.4",
                        "provider": "openai",
                        "purpose": "synthesis",
                        "selectionSource": "configured",
                    },
                    "verification": {
                        "effort": "high",
                        "model": "gpt-5.4-mini",
                        "provider": "openai",
                        "purpose": "verification",
                        "selectionSource": "configured",
                    },
                },
                "default": _fixed_compaction_model_plan(),
            }
        if "modelCandidates" in case:
            case["modelCandidates"] = {
                "synthesis": [_fixed_compaction_model("synthesis")],
                "verification": [_fixed_compaction_model("verification")],
            }
        _rewrite_compaction_model_records(case)
    return value


def _rewrite_compaction_model_records(value: Any, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        if parent_key == "modelCalls" and _is_compaction_model_call(value):
            value["model"] = "gpt-5.3-codex-spark"
            value["effort"] = "high"
            return value
        if parent_key == "compactionModel":
            replacement = _fixed_compaction_model("synthesis")
            if "purpose" not in value:
                replacement.pop("purpose", None)
            value.clear()
            value.update(replacement)
            return value
        if parent_key == "verificationModel":
            replacement = _fixed_compaction_model("verification")
            if "purpose" not in value:
                replacement.pop("purpose", None)
            value.clear()
            value.update(replacement)
            return value
        if parent_key in {"compaction", "verification"} and _should_rewrite_model_record(value):
            purpose = "synthesis" if parent_key == "compaction" else "verification"
            replacement = _fixed_compaction_model(purpose)
            if "purpose" not in value:
                replacement.pop("purpose", None)
            value.clear()
            value.update(replacement)
            return value
        if parent_key == "models":
            if _should_rewrite_model_record(value.get("compaction")):
                value["compaction"] = _fixed_compaction_model("synthesis")
            if _should_rewrite_model_record(value.get("verification")):
                value["verification"] = _fixed_compaction_model("verification")
        for key, item in list(value.items()):
            value[key] = _rewrite_compaction_model_records(item, key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _rewrite_compaction_model_records(item, parent_key)
    elif isinstance(value, str):
        return _normalize_compaction_model_markdown(value)
    return value


def _is_compaction_model_call(value: dict[str, Any]) -> bool:
    prompt = value.get("prompt") if isinstance(value.get("prompt"), dict) else {}
    first_line = prompt.get("firstLine")
    return isinstance(first_line, str) and first_line.startswith(("Synthesize the next working context", "Verify this candidate compaction"))


def _should_rewrite_model_record(value: Any) -> bool:
    return isinstance(value, dict) and {"model", "provider", "selectionSource"} <= set(value) and value.get("selectionSource") != "configured"


def _normalize_compaction_model_markdown(value: str) -> str:
    return (
        value.replace("openai:gpt-5.4` (`xhigh`) via `session_provider`", "openai:gpt-5.3-codex-spark` (`high`) via `fixed`")
        .replace("by `openai:gpt-5.4` (`xhigh`) at", "by `openai:gpt-5.3-codex-spark` (`high`) at")
        .replace("openai:gpt-5.4 (xhigh) via session_provider", "openai:gpt-5.3-codex-spark (high) via fixed")
        .replace("openai:gpt-5.4 (xhigh)", "openai:gpt-5.3-codex-spark (high)")
    )


def _fixed_compaction_model_plan() -> dict[str, Any]:
    return {
        "synthesis": _fixed_compaction_model("synthesis"),
        "verification": _fixed_compaction_model("verification"),
    }


def _fixed_compaction_model(purpose: str) -> dict[str, Any]:
    return {
        "effort": "high",
        "model": "gpt-5.3-codex-spark",
        "provider": "openai",
        "purpose": purpose,
        "selectionSource": "fixed",
    }
