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
MODULE_PATH_IN_STRING = re.compile(r"agent_core((?:\.[a-z_][a-z_0-9]*)+)")

SEARCH_ROOTS = ("agent_core", "tests", "agent_core_harness")

# "agent_core.py" and friends are filenames in prose or fixtures, not modules.
FILE_SUFFIXES = {"py", "json", "jsonl", "md", "txt", "log", "toml"}


def _top_level_names() -> set[str]:
    names = {path.stem for path in PACKAGE.glob("*.py")}
    names |= {d.name for d in PACKAGE.iterdir() if d.is_dir() and not d.name.startswith("__")}
    return names


def _resolves(segments: list[str]) -> bool:
    """Walk a dotted path across the package tree.

    Trailing segments that are attributes rather than modules are fine -- the
    walk stops as soon as it reaches a ``.py`` module. It only reports failure
    when a segment is missing while its parent is still a *package directory*,
    which is what a stale module path looks like.
    """
    here = PACKAGE
    for index, segment in enumerate(segments):
        if (here / f"{segment}.py").is_file():
            return True  # reached a module; the rest are attributes
        # A directory only counts as a package if it still has an __init__.py:
        # a leftover __pycache__ keeps the directory alive after a move and
        # would otherwise make every stale path look resolvable.
        if (here / segment / "__init__.py").is_file():
            here = here / segment
            continue
        return index > 0 and not (here / "__init__.py").is_file()
    return True


def _legacy_aliases() -> set[str]:
    text = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    block = text.split("_LEGACY_MODULE_ALIASES = {")[1].split("}")[0]
    return set(re.findall(r'"([a-z_0-9]+)":', block))


class ModulePathStringTests(unittest.TestCase):
    def test_module_paths_written_as_strings_still_resolve(self):
        # Only legacy aliases short-circuit: current names are checked by
        # walking the tree, so a stale *deep* path is caught too.
        resolvable = _legacy_aliases()
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
                        segments = match.group(1).lstrip(".").split(".")
                        if segments[0] in FILE_SUFFIXES:
                            continue
                        if segments[0] in resolvable:
                            continue  # a legacy alias resolves via sys.modules
                        if _resolves(segments):
                            continue
                        offenders.append(f"{path.relative_to(ROOT)}:{number}: agent_core.{'.'.join(segments)}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
