from __future__ import annotations

"""Runtime knob registry.

Every tunable runtime value belongs here: one declaration with a dotted
name, default, type, bounds, and description. Overrides come from the
``knobs:`` section of <runtime-home>/settings/config.yaml (validated
strictly against this registry — unknown names are fatal) and from
process environment variables (``AGENT_CORE_SUBAGENTS_MAX_LIVE`` overrides
``subagents.max_live``). Precedence: default < config.yaml < environment.

``configureKnobs`` is called by config resolution; modules read values via
``knobValue`` so no tunable is a hardcoded module constant. The checked-in
``config.yaml.example`` is generated from this registry (lockstep test).
"""

import os
from dataclasses import dataclass
from typing import Any

_ENV_PREFIX = "AGENT_CORE_"


@dataclass(frozen=True)
class Knob:
    name: str
    default: Any
    kind: str  # "int" | "float" | "bool" | "str" | "int_list"
    description: str
    minimum: float | None = None  # for int_list: applies to every element
    maximum: float | None = None


def _declare(*knobs: Knob) -> dict[str, Knob]:
    registry: dict[str, Knob] = {}
    for knob in knobs:
        if knob.name in registry:
            raise ValueError(f"Duplicate knob declaration: {knob.name}")
        registry[knob.name] = knob
    return registry


KNOB_REGISTRY: dict[str, Knob] = _declare(
    Knob(
        "subagents.max_live",
        8,
        "int",
        "Maximum live subagent processes per runtime instance; at capacity the Agent tool returns the live-children roster instead of spawning.",
        minimum=1,
        maximum=64,
    ),
    Knob(
        "provider.max_output_tokens",
        16384,
        "int",
        "Fallback max output tokens per provider request when neither the request nor the provider profile specifies one.",
        minimum=256,
        maximum=1_000_000,
    ),
    Knob(
        "provider.request_start_timeout_ms",
        120_000,
        "int",
        "Milliseconds to wait for a provider request to start streaming before the attempt is failed as timed out.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "provider.stream_idle_timeout_ms",
        60_000,
        "int",
        "Milliseconds of stream silence tolerated before a provider attempt is failed as idle.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "provider.json_response_timeout_ms",
        120_000,
        "int",
        "Milliseconds to wait for a complete non-streaming provider JSON response.",
        minimum=1_000,
        maximum=3_600_000,
    ),
    Knob(
        "provider.openai_retry_delays_ms",
        (1_000, 3_000, 5_000, 10_000, 30_000),
        "int_list",
        "Escalating retry backoff ladder in milliseconds for OpenAI-kind provider requests; attempts beyond the ladder reuse its last entry.",
        minimum=0,
        maximum=600_000,
    ),
    Knob(
        "provider.anthropic_retry_delays_ms",
        (350, 700),
        "int_list",
        "Quick-retry delay ladder in milliseconds for Anthropic messages requests; a small random jitter is added on top.",
        minimum=0,
        maximum=600_000,
    ),
)

_configured: dict[str, Any] = {}


def knobValue(name: str) -> Any:
    knob = KNOB_REGISTRY.get(name)
    if knob is None:
        raise KeyError(f"Unknown knob {name!r}; declare it in KNOB_REGISTRY first.")
    if name in _configured:
        return _configured[name]
    env_value = os.environ.get(_env_name(name))
    if env_value is not None:
        return _coerce(knob, env_value, source="environment")
    return knob.default


def configureKnobs(overrides: Any) -> None:
    """Install config.yaml knob overrides (plus environment) process-wide."""

    flattened = _flatten(overrides or {})
    unknown = sorted(set(flattened) - set(KNOB_REGISTRY))
    if unknown:
        known = ", ".join(sorted(KNOB_REGISTRY))
        raise ValueError(
            f"Unknown knob(s) in settings: {', '.join(unknown)}. Registered knobs: {known}"
        )
    resolved: dict[str, Any] = {}
    for name, raw in flattened.items():
        resolved[name] = _coerce(KNOB_REGISTRY[name], raw, source="config.yaml")
    for name in KNOB_REGISTRY:
        env_value = os.environ.get(_env_name(name))
        if env_value is not None:
            resolved[name] = _coerce(KNOB_REGISTRY[name], env_value, source="environment")
    _configured.clear()
    _configured.update(resolved)


def resetConfiguredKnobs() -> None:
    _configured.clear()


def renderKnobsExample() -> str:
    lines = [
        "# Runtime configuration knobs — generated from agent_core/config/_knobs.py.",
        "# Copy wanted entries under the `knobs:` section of",
        "# <runtime-home>/settings/config.yaml. Values shown are the defaults.",
        "# Environment overrides: AGENT_CORE_<NAME> with dots as underscores,",
        "# e.g. AGENT_CORE_SUBAGENTS_MAX_LIVE=4.",
        "",
        "knobs:",
    ]
    tree: dict[str, list[Knob]] = {}
    for knob in KNOB_REGISTRY.values():
        section = knob.name.split(".", 1)[0]
        tree.setdefault(section, []).append(knob)
    for section in sorted(tree):
        lines.append(f"  {section}:")
        for knob in sorted(tree[section], key=lambda item: item.name):
            leaf = knob.name.split(".", 1)[1]
            bounds = ""
            if knob.minimum is not None or knob.maximum is not None:
                bounds = f" [{_format_number(knob.minimum)}..{_format_number(knob.maximum)}]"
            lines.append(f"    # {knob.description}{bounds}")
            if knob.kind == "int_list":
                lines.append(f"    {leaf}:")
                lines.extend(f"      - {item}" for item in knob.default)
            else:
                lines.append(f"    {leaf}: {_render_value(knob.default)}")
    return "\n".join(lines) + "\n"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        if prefix:
            return {prefix: value}
        raise ValueError("The knobs section must be a mapping.")
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            flattened.update(_flatten(item, dotted))
        else:
            flattened[dotted] = item
    return flattened


def _coerce(knob: Knob, raw: Any, *, source: str) -> Any:
    value: Any = raw
    if knob.kind == "int_list":
        return _coerce_int_list(knob, raw, source=source)
    if knob.kind == "bool":
        if isinstance(raw, bool):
            value = raw
        elif isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            value = raw.strip().lower() == "true"
        else:
            raise ValueError(f"Knob {knob.name} from {source} must be a boolean, got {raw!r}.")
    elif knob.kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f"Knob {knob.name} from {source} must be an integer, got {raw!r}.")
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"Knob {knob.name} from {source} must be an integer, got {raw!r}.") from None
    elif knob.kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"Knob {knob.name} from {source} must be a number, got {raw!r}.")
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"Knob {knob.name} from {source} must be a number, got {raw!r}.") from None
    elif knob.kind == "str":
        if not isinstance(raw, str):
            raise ValueError(f"Knob {knob.name} from {source} must be a string, got {raw!r}.")
        value = raw
    else:
        raise ValueError(f"Knob {knob.name} declares unsupported kind {knob.kind!r}.")
    if knob.minimum is not None and value < knob.minimum:
        raise ValueError(f"Knob {knob.name} from {source} must be >= {_format_number(knob.minimum)}, got {value}.")
    if knob.maximum is not None and value > knob.maximum:
        raise ValueError(f"Knob {knob.name} from {source} must be <= {_format_number(knob.maximum)}, got {value}.")
    return value


def _coerce_int_list(knob: Knob, raw: Any, *, source: str) -> tuple[int, ...]:
    items: Any = raw
    if isinstance(items, str):
        items = [part.strip() for part in items.split(",")]
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError(
            f"Knob {knob.name} from {source} must be a non-empty list of integers "
            f"(or a comma-separated string), got {raw!r}."
        )
    values: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise ValueError(f"Knob {knob.name} from {source} must contain only integers, got {item!r}.")
        try:
            value = int(str(item).strip())
        except ValueError:
            raise ValueError(f"Knob {knob.name} from {source} must contain only integers, got {item!r}.") from None
        if knob.minimum is not None and value < knob.minimum:
            raise ValueError(f"Knob {knob.name} from {source} entries must be >= {_format_number(knob.minimum)}, got {value}.")
        if knob.maximum is not None and value > knob.maximum:
            raise ValueError(f"Knob {knob.name} from {source} entries must be <= {_format_number(knob.maximum)}, got {value}.")
        values.append(value)
    return tuple(values)


def _env_name(name: str) -> str:
    return _ENV_PREFIX + name.replace(".", "_").upper()


def _render_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
