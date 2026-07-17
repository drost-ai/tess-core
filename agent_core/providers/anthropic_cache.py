from __future__ import annotations

import copy
from typing import Any, Mapping

from .prompt_cache import (
    anthropicMessageTailCacheEnabled,
    anthropicToolDefinitionCacheEnabled,
)
from .prompt_cache_diagnostics import hashPromptCacheValue


MAX_ANTHROPIC_CACHE_BREAKPOINTS = 4
_PLACEMENTS = ("system", "tools", "tail_a", "tail_b")


def planAnthropicCache(input: Mapping[str, Any]) -> dict[str, Any]:
    policy = input.get("policy") if isinstance(input.get("policy"), Mapping) else {}
    system = _records(input.get("normalizedSystem"))
    tools = _records(input.get("normalizedTools"))
    messages = _messages(input.get("normalizedMessages"))
    automatic = _automatic_enabled(policy)
    explicit = _explicit_enabled(policy)
    plan: dict[str, Any] = {
        "mode": "auto" if automatic else "explicit" if explicit else "off",
        "maxBreakpoints": MAX_ANTHROPIC_CACHE_BREAKPOINTS,
        "automatic": automatic,
        "system": False,
        "tools": False,
        "tailA": None,
        "tailB": None,
        "usedBreakpoints": 1 if automatic else 0,
        "placements": ["automatic"] if automatic else [],
        "omitted": [],
        "cacheControl": _cache_control(policy),
    }

    system_candidate = explicit and _find_last_eligible_block(system) is not None
    tools_candidate = anthropicToolDefinitionCacheEnabled(dict(policy)) and bool(tools)
    tail_a = (
        _find_newest_stable_anchor(system, tools, messages)
        if anthropicMessageTailCacheEnabled(dict(policy))
        else None
    )
    tail_b, omission = _resolve_previous_anchor(
        system,
        tools,
        messages,
        input.get("previousAnchor"),
        tail_a,
    )
    if omission:
        plan["omitted"].append(omission)

    _allocate(plan, "system", system_candidate, lambda: plan.__setitem__("system", True))
    _allocate(plan, "tools", tools_candidate, lambda: plan.__setitem__("tools", True))
    _allocate(plan, "tail_a", tail_a is not None, lambda: plan.__setitem__("tailA", tail_a))
    _allocate(plan, "tail_b", tail_b is not None, lambda: plan.__setitem__("tailB", tail_b))
    if plan["usedBreakpoints"] > MAX_ANTHROPIC_CACHE_BREAKPOINTS:
        raise RuntimeError("Anthropic cache plan exceeded four breakpoints.")
    return plan


def applyAnthropicCachePlan(parts: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    system_index = _find_last_eligible_block(_records(parts.get("system")))
    marked = stripAnthropicCacheControls(parts)
    control = copy.deepcopy(plan.get("cacheControl") or {"type": "ephemeral"})
    if plan.get("system"):
        if system_index is not None:
            marked["system"][system_index]["cache_control"] = copy.deepcopy(control)
    if plan.get("tools") and marked["tools"]:
        marked["tools"][-1]["cache_control"] = copy.deepcopy(control)
    for anchor in (plan.get("tailA"), plan.get("tailB")):
        if not isinstance(anchor, Mapping):
            continue
        message_index = anchor.get("messageIndex")
        block_index = anchor.get("blockIndex")
        if not _nonnegative_integer(message_index) or not _nonnegative_integer(block_index):
            continue
        try:
            marked["messages"][message_index]["content"][block_index]["cache_control"] = copy.deepcopy(control)
        except (IndexError, KeyError, TypeError):
            continue
    return marked


def stripAnthropicCacheControls(parts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "system": [_strip_marker(value) for value in _records(parts.get("system"))],
        "tools": [_strip_marker(value) for value in _records(parts.get("tools"))],
        "messages": [
            {
                "role": message["role"],
                "content": [_strip_marker(block) for block in message["content"]],
            }
            for message in _messages(parts.get("messages"))
        ],
    }


def sanitizeAnthropicCountPayload(payload: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = copy.deepcopy(dict(payload))
    sanitized.pop("cache_control", None)
    sanitized.pop("diagnostics", None)
    if isinstance(sanitized.get("tools"), list):
        sanitized["tools"] = [_strip_marker(value) for value in sanitized["tools"]]
    if isinstance(sanitized.get("system"), list):
        sanitized["system"] = [_strip_marker(value) for value in sanitized["system"]]
    if isinstance(sanitized.get("messages"), list):
        messages = []
        for value in sanitized["messages"]:
            if not isinstance(value, Mapping) or not isinstance(value.get("content"), list):
                messages.append(value)
                continue
            messages.append(
                {
                    **dict(value),
                    "content": [_strip_marker(block) for block in value["content"]],
                }
            )
        sanitized["messages"] = messages
    return sanitized


def countAnthropicCacheControls(payload: Mapping[str, Any]) -> int:
    count = 1 if "cache_control" in payload else 0
    for key in ("tools", "system"):
        values = payload.get(key)
        if isinstance(values, list):
            count += sum(
                1 for value in values if isinstance(value, Mapping) and value.get("cache_control")
            )
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list):
                count += sum(
                    1
                    for value in content
                    if isinstance(value, Mapping) and value.get("cache_control")
                )
    return count


def buildAnthropicMessageAnchorIdentity(
    *,
    normalizedSystem: list[dict[str, Any]],
    normalizedTools: list[dict[str, Any]],
    normalizedMessages: list[dict[str, Any]],
    messageIndex: int,
    blockIndex: int,
) -> str:
    prefix_messages = []
    for index, message in enumerate(normalizedMessages[: messageIndex + 1]):
        content = message["content"]
        if index == messageIndex:
            content = content[: blockIndex + 1]
        prefix_messages.append(
            {"role": message["role"], "content": [_strip_marker(block) for block in content]}
        )
    return hashPromptCacheValue(
        "anthropic-anchor",
        {
            "tools": [_strip_marker(value) for value in normalizedTools],
            "system": [_strip_marker(value) for value in normalizedSystem],
            "messages": prefix_messages,
        },
    )


def isAnthropicCacheableBlock(block: Mapping[str, Any] | None) -> bool:
    if not isinstance(block, Mapping) or not isinstance(block.get("type"), str):
        return False
    if block.get("_cache_target") is False:
        return False
    kind = block["type"]
    if kind == "text":
        return isinstance(block.get("text"), str) and bool(block["text"])
    if kind == "image":
        return isinstance(block.get("source"), Mapping)
    if kind == "tool_use":
        return (
            isinstance(block.get("id"), str)
            and isinstance(block.get("name"), str)
            and isinstance(block.get("input"), Mapping)
        )
    if kind == "tool_result":
        return isinstance(block.get("tool_use_id"), str)
    return False


def _find_newest_stable_anchor(
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for message_index in range(len(messages) - 2, -1, -1):
        block_index = _find_last_eligible_block(messages[message_index]["content"])
        if block_index is None:
            continue
        return {
            "messageIndex": message_index,
            "blockIndex": block_index,
            "identity": buildAnthropicMessageAnchorIdentity(
                normalizedSystem=system,
                normalizedTools=tools,
                normalizedMessages=messages,
                messageIndex=message_index,
                blockIndex=block_index,
            ),
        }
    return None


def _resolve_previous_anchor(
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    previous: Any,
    tail_a: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if not isinstance(previous, Mapping):
        return None, None
    message_index = previous.get("messageIndex")
    block_index = previous.get("blockIndex")
    if not _nonnegative_integer(message_index) or not _nonnegative_integer(block_index):
        return None, {"placement": "tail_b", "reason": "out_of_range"}
    try:
        block = messages[message_index]["content"][block_index]
    except (IndexError, KeyError, TypeError):
        return None, {"placement": "tail_b", "reason": "out_of_range"}
    if not isAnthropicCacheableBlock(block):
        return None, {"placement": "tail_b", "reason": "illegal_block"}
    if not tail_a or _compare_anchors(previous, tail_a) >= 0:
        return None, {"placement": "tail_b", "reason": "not_strict_prefix"}
    identity = buildAnthropicMessageAnchorIdentity(
        normalizedSystem=system,
        normalizedTools=tools,
        normalizedMessages=messages,
        messageIndex=message_index,
        blockIndex=block_index,
    )
    if identity != previous.get("identity"):
        return None, {"placement": "tail_b", "reason": "identity_mismatch"}
    return {"messageIndex": message_index, "blockIndex": block_index, "identity": identity}, None


def _allocate(
    plan: dict[str, Any], placement: str, candidate: bool, accept: Any
) -> None:
    if not candidate:
        return
    if plan["usedBreakpoints"] >= plan["maxBreakpoints"]:
        plan["omitted"].append({"placement": placement, "reason": "budget"})
        return
    accept()
    plan["usedBreakpoints"] += 1
    plan["placements"].append(placement)


def _automatic_enabled(policy: Mapping[str, Any]) -> bool:
    return bool(
        policy.get("enabled")
        and policy.get("mode") == "auto"
        and not policy.get("compatibilitySuppressedReason")
    )


def _explicit_enabled(policy: Mapping[str, Any]) -> bool:
    return bool(
        policy.get("enabled")
        and policy.get("mode") in {"auto", "explicit"}
        and policy.get("explicit") is not False
        and not policy.get("compatibilitySuppressedReason")
    )


def _cache_control(policy: Mapping[str, Any]) -> dict[str, str]:
    control = {"type": "ephemeral"}
    if policy.get("ttl") in {"5m", "1h"}:
        control["ttl"] = str(policy["ttl"])
    return control


def _find_last_eligible_block(blocks: list[dict[str, Any]]) -> int | None:
    for index in range(len(blocks) - 1, -1, -1):
        if isAnthropicCacheableBlock(blocks[index]):
            return index
    return None


def _compare_anchors(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    left_coordinate = (int(left["messageIndex"]), int(left["blockIndex"]))
    right_coordinate = (int(right["messageIndex"]), int(right["blockIndex"]))
    return (left_coordinate > right_coordinate) - (left_coordinate < right_coordinate)


def _strip_marker(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    copied = copy.deepcopy(dict(value))
    copied.pop("cache_control", None)
    copied.pop("_cache_target", None)
    return copied


def _records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        output.append(
            {
                "role": str(item.get("role") or ""),
                "content": _records(item.get("content")),
            }
        )
    return output


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


plan_anthropic_cache = planAnthropicCache
apply_anthropic_cache_plan = applyAnthropicCachePlan
sanitize_anthropic_count_payload = sanitizeAnthropicCountPayload
count_anthropic_cache_controls = countAnthropicCacheControls
