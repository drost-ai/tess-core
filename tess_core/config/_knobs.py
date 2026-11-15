from __future__ import annotations

"""Runtime knob registry.

Every tunable runtime value belongs here: one declaration with a dotted
name, default, type, bounds, and description. Overrides come from the
``knobs:`` section of <runtime-home>/settings/config.yaml (validated
strictly against this registry — unknown names are fatal) and from
process environment variables (``TESS_CORE_SUBAGENTS_MAX_LIVE`` overrides
``subagents.max_live``). Precedence: default < config.yaml < environment.

``configure_knobs`` is called by config resolution; modules read values via
``knob_value`` so no tunable is a hardcoded module constant. The checked-in
``config.yaml.example`` is generated from this registry (lockstep test).

A ceiling (a cap on calls, tools, time, retries) is never defaulted in code:
its kind carries a ``?`` (``int?``), its default is ``None``, and the code
treats ``None`` as "no ceiling". The configuration sets it or there is none.

Declarations live in ``_knob_registry.py``; this module keeps the
machinery. Whether a constant may become a knob at all is decided by
``KNOB_CLASSIFICATION.md`` — classify new constants there at review time.
"""

import os
import sys
from typing import Any

from ._knob_registry import KNOB_REGISTRY, Knob

_ENV_PREFIX = "TESS_CORE_"

_configured: dict[str, Any] = {}


def knob_value(name: str) -> Any:
    knob = KNOB_REGISTRY.get(name)
    if knob is None:
        raise KeyError(f"Unknown knob {name!r}; declare it in KNOB_REGISTRY first.")
    if name in _configured:
        return _configured[name]
    env_value = os.environ.get(_env_name(name))
    if env_value is not None:
        return _coerce(knob, env_value, source="environment")
    return knob.default


def configure_knobs(overrides: Any) -> None:
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


def reset_configured_knobs() -> None:
    _configured.clear()


_warned_legacy_env: set[str] = set()


def warn_legacy_env_alias(legacy_name: str, knob_name: str) -> None:
    """One-time stderr warning when a deprecated env alias is in use."""

    if legacy_name in _warned_legacy_env or not os.environ.get(legacy_name):
        return
    _warned_legacy_env.add(legacy_name)
    print(
        f"[tess-core] {legacy_name} is deprecated; use {_env_name(knob_name)} (knob {knob_name}) instead.",
        file=sys.stderr,
    )


def render_knobs_example() -> str:
    lines = [
        "# Runtime configuration knobs — generated from tess_core/config/_knobs.py.",
        "# Copy wanted entries under the `knobs:` section of",
        "# <runtime-home>/settings/config.yaml. Values shown are the defaults.",
        "# Environment overrides: TESS_CORE_<NAME> with dots as underscores,",
        "# e.g. TESS_CORE_SUBAGENTS_MAX_LIVE=4.",
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


_UNSET_TOKENS = {"", "null", "none", "~"}


def _coerce(knob: Knob, raw: Any, *, source: str) -> Any:
    value: Any = raw
    kind = knob.kind
    if kind.endswith("?"):
        # A ceiling: the configuration sets it or there is none. Never a
        # number invented here.
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in _UNSET_TOKENS):
            return None
        kind = kind[:-1]
    if kind == "int_list":
        return _coerce_int_list(knob, raw, source=source)
    if kind == "bool":
        if isinstance(raw, bool):
            value = raw
        elif isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            value = raw.strip().lower() == "true"
        else:
            raise ValueError(f"Knob {knob.name} from {source} must be a boolean, got {raw!r}.")
    elif kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f"Knob {knob.name} from {source} must be an integer, got {raw!r}.")
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"Knob {knob.name} from {source} must be an integer, got {raw!r}.") from None
    elif kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"Knob {knob.name} from {source} must be a number, got {raw!r}.")
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"Knob {knob.name} from {source} must be a number, got {raw!r}.") from None
    elif kind == "str":
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
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value == "":
        return '""'
    return str(value)


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
