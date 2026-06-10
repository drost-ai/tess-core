"""Runtime validator for contract boundary schemas.

This module is intentionally dependency-free and shallow. It checks a mapping
against a ``TypedDict`` schema: required keys, unknown keys (strict mode),
``Literal`` membership, scalar types, and declared nested TypedDicts. It is a
test/harness aid, never a production hot-path guard, and it must never reject
a record the runtime currently accepts — legacy duplicate spellings are
declared in the schemas themselves so they validate cleanly.
"""

from __future__ import annotations

import types
import typing
from typing import Any, Literal, Mapping, Union

# PEP 604 unions (str | None) resolve to types.UnionType, typing.Optional to
# typing.Union; the validator accepts both spellings.
_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


def _type_name(annotation: Any) -> str:
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else str(annotation)


def _check_value(value: Any, annotation: Any, path: str, strict: bool) -> list[str]:
    if annotation is Any:
        return []
    origin = typing.get_origin(annotation)
    if origin is Literal:
        allowed = typing.get_args(annotation)
        if value not in allowed:
            return [f"{path}: value {value!r} not in allowed set {sorted(str(item) for item in allowed)}"]
        return []
    if origin in _UNION_ORIGINS:
        members = typing.get_args(annotation)
        candidate_errors: list[str] = []
        for member in members:
            errors = _check_value(value, member, path, strict)
            if not errors:
                return []
            candidate_errors = errors
        expected = " | ".join(_type_name(member) for member in members)
        if len(members) > 2 or type(None) not in members:
            return [f"{path}: value of type {type(value).__name__} did not match {expected}"]
        return candidate_errors
    if annotation is type(None):
        return [] if value is None else [f"{path}: expected None, got {type(value).__name__}"]
    if typing.is_typeddict(annotation):
        if not isinstance(value, Mapping):
            return [f"{path}: expected mapping for {_type_name(annotation)}, got {type(value).__name__}"]
        return [f"{path}.{error}" for error in validate(value, annotation, strict=strict)]
    if origin is list:
        if not isinstance(value, list):
            return [f"{path}: expected list, got {type(value).__name__}"]
        args = typing.get_args(annotation)
        if not args or args[0] is Any:
            return []
        errors = []
        for index, item in enumerate(value):
            errors.extend(_check_value(item, args[0], f"{path}[{index}]", strict))
        return errors
    if origin is dict:
        if not isinstance(value, Mapping):
            return [f"{path}: expected mapping, got {type(value).__name__}"]
        return []
    if annotation is bool:
        return [] if isinstance(value, bool) else [f"{path}: expected bool, got {type(value).__name__}"]
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path}: expected int, got {type(value).__name__}"]
        return []
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path}: expected number, got {type(value).__name__}"]
        return []
    if isinstance(annotation, type):
        if isinstance(value, annotation):
            return []
        return [f"{path}: expected {_type_name(annotation)}, got {type(value).__name__}"]
    return []


def validate(record: Any, schema: type, *, strict: bool = False) -> list[str]:
    """Return contract violations for ``record`` against a TypedDict ``schema``.

    Empty list means valid. ``strict=True`` additionally reports keys that the
    schema does not declare. The check is shallow by design: one level plus
    declared nested TypedDicts and list elements.
    """
    if not typing.is_typeddict(schema):
        raise TypeError(f"schema must be a TypedDict class, got {schema!r}")
    if not isinstance(record, Mapping):
        return [f"expected mapping for {_type_name(schema)}, got {type(record).__name__}"]
    hints = typing.get_type_hints(schema)
    required: frozenset[str] = getattr(schema, "__required_keys__", frozenset())
    errors: list[str] = []
    for key in sorted(required):
        if key not in record:
            errors.append(f"missing required key '{key}'")
    if strict:
        for key in record:
            if key not in hints:
                errors.append(f"unknown key '{key}'")
    for key, value in record.items():
        if key in hints:
            errors.extend(_check_value(value, hints[key], str(key), strict))
    return errors


def assert_valid(record: Any, schema: type, *, strict: bool = False, label: str | None = None) -> None:
    """Raise ``ValueError`` listing violations when ``record`` does not match."""
    errors = validate(record, schema, strict=strict)
    if errors:
        subject = label or _type_name(schema)
        raise ValueError(f"contract violation for {subject}: " + "; ".join(errors))
