from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent_core.runtime.events.shared import KNOWN_RUNTIME_EVENTS

ROOT = Path(__file__).resolve().parents[1]

# Emission callables whose first (or second, for bound runtimes) argument is
# the event name. Collected statically so a new emit site cannot ship an
# unregistered event name.
_EMIT_CALLEES = {"safe_emit", "_safe_emit", "_emit", "emitEvent", "emit", "_emit_runtime"}

# Dynamic or non-runtime channels the registry deliberately does not cover:
# daemon wire frames, harness-internal events, and prefix-bucketed families.
_ALLOWED_PREFIXES = ("mcp.", "bridge.", "remote.", "platform.", "plan.", "permission.")
_ALLOWED_EXTRA = {"storage.lock_timeout", "budget.exceeded"}


def _callee_name(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _emitted_literals() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "agent_core").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee_name(node) not in _EMIT_CALLEES:
                continue
            for arg in node.args[:2]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "." in arg.value:
                    name = arg.value
                    if name.count(".") >= 1 and " " not in name and name == name.lower():
                        found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
                    break
    return found


class EventRegistryTests(unittest.TestCase):
    def test_every_emitted_event_name_is_registered(self):
        offenders = []
        for name, files in sorted(_emitted_literals().items()):
            if name in KNOWN_RUNTIME_EVENTS or name in _ALLOWED_EXTRA:
                continue
            if name.startswith(_ALLOWED_PREFIXES):
                continue
            offenders.append(f"{name} (emitted in {', '.join(sorted(files))})")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
