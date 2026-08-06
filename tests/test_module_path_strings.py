from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "agent_core"

# ``agent_core.<name>`` written inside a *string* rather than an import: the
# subprocess entry points (``python -m agent_core.process.daemon_worker``),
# mock.patch targets, and daemon process-table probes. Import rewriting cannot
# see these, so a module move silently breaks them at runtime instead of at
# import time -- which is exactly how it presents: a child process that exits 1.
MODULE_PATH_IN_STRING = re.compile(r"agent_core\.([a-z_][a-z_0-9]*)")

SEARCH_ROOTS = ("agent_core", "tests", "agent_core_harness")

# "agent_core.py" and friends are filenames in prose or fixtures, not modules.
FILE_SUFFIXES = {"py", "json", "jsonl", "md", "txt", "log", "toml"}


def _top_level_names() -> set[str]:
    names = {path.stem for path in PACKAGE.glob("*.py")}
    names |= {d.name for d in PACKAGE.iterdir() if d.is_dir() and not d.name.startswith("__")}
    return names


def _legacy_aliases() -> set[str]:
    text = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    block = text.split("_LEGACY_MODULE_ALIASES = {")[1].split("}")[0]
    return set(re.findall(r'"([a-z_0-9]+)":', block))


class ModulePathStringTests(unittest.TestCase):
    def test_module_paths_written_as_strings_still_resolve(self):
        resolvable = _top_level_names() | _legacy_aliases()
        offenders: list[str] = []
        for base in SEARCH_ROOTS:
            for path in sorted((ROOT / base).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                    stripped = line.lstrip()
                    if stripped.startswith(("from ", "import ")):
                        continue  # a real import; the interpreter checks these
                    for match in MODULE_PATH_IN_STRING.finditer(line):
                        name = match.group(1)
                        if name in FILE_SUFFIXES:
                            continue
                        if name not in resolvable:
                            offenders.append(f"{path.relative_to(ROOT)}:{number}: agent_core.{name}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
