from __future__ import annotations

"""Writing settings YAML inside the strict subset that ``_yaml`` reads.

The runtime and its host write settings files (config.yaml, provider
registry blocks) only through this emitter, so the engine, its task daemon
and standard YAML readers all see the same value. Block style, two-space
indentation, list items indented under their key; strings are quoted
whenever another YAML reader could take them for something else.
"""

from typing import Any

from ._yaml import (
    _FALSE_TOKENS,
    _FLOAT_RE,
    _INDENT_STEP,
    _INT_RE,
    _NULL_TOKENS,
    _PLAIN_KEY_RE,
    _REJECTED_LEADS,
    _TRUE_TOKENS,
    YamlSubsetError,
    parse_yaml_subset,
)


def dump_yaml_subset(value: Any) -> str:
    """Block-style YAML inside this subset, for the settings files the runtime
    or its host writes. Refuses anything that would not read back as the same
    value (non-finite floats, non-string keys, unsupported types)."""
    lines: list[str] = []
    if isinstance(value, dict) and value:
        _emit_mapping(value, 0, lines)
    elif isinstance(value, list) and value:
        _emit_sequence(value, 0, lines)
    else:
        lines.append(_scalar_text(value))
    text = "\n".join(lines) + "\n"
    if parse_yaml_subset(text, source="emitted settings") != value:
        raise YamlSubsetError("value cannot be written in the settings YAML subset", source="emitted settings", line=1)
    return text


def _is_block(value: Any) -> bool:
    return isinstance(value, (dict, list)) and bool(value)


def _key_text(key: Any) -> str:
    if not isinstance(key, str):
        raise YamlSubsetError("settings keys must be strings", source="emitted settings", line=1)
    return key if _PLAIN_KEY_RE.match(key) else _double_quoted(key)


def _emit_mapping(mapping: dict[str, Any], indent: int, lines: list[str], *, first_prefix: str | None = None) -> None:
    pad = " " * indent
    for index, (key, item) in enumerate(mapping.items()):
        lead = first_prefix if index == 0 and first_prefix is not None else pad
        if _is_block(item):
            lines.append(f"{lead}{_key_text(key)}:")
            _emit_block(item, indent + _INDENT_STEP, lines)
        else:
            lines.append(f"{lead}{_key_text(key)}: {_scalar_text(item)}")


def _emit_sequence(items: list[Any], indent: int, lines: list[str]) -> None:
    pad = " " * indent
    for item in items:
        if isinstance(item, dict) and item:
            # "- key: value", the item's further keys two spaces in.
            _emit_mapping(item, indent + _INDENT_STEP, lines, first_prefix=f"{pad}- ")
        elif isinstance(item, list) and item:
            lines.append(f"{pad}-")
            _emit_sequence(item, indent + _INDENT_STEP, lines)
        else:
            lines.append(f"{pad}- {_scalar_text(item)}")


def _emit_block(value: Any, indent: int, lines: list[str]) -> None:
    if isinstance(value, dict):
        _emit_mapping(value, indent, lines)
    else:
        _emit_sequence(value, indent, lines)


def _scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise YamlSubsetError("non-finite numbers are not supported", source="emitted settings", line=1)
        return repr(value)
    if isinstance(value, dict) and not value:
        return "{}"
    if isinstance(value, list) and not value:
        return "[]"
    if not isinstance(value, str):
        raise YamlSubsetError(f"unsupported settings value type {type(value).__name__}", source="emitted settings", line=1)
    plain = (value and value == value.strip() and value[0] not in _REJECTED_LEADS and value[0] not in "'\"|>#-"
             and ":" not in value and " #" not in value  # 12:00 is a number to YAML 1.1 readers
             and value not in _NULL_TOKENS | _TRUE_TOKENS | _FALSE_TOKENS
             and value.lower() not in {"yes", "no", "on", "off", "y", "n"}
             and not _INT_RE.match(value) and not _FLOAT_RE.match(value)
             and all(char.isprintable() for char in value))
    return value if plain else _double_quoted(value)


def _double_quoted(text: str) -> str:
    escapes = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0"}
    out = []
    for char in text:
        if char in escapes:
            out.append(escapes[char])
        elif not char.isprintable():
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'
