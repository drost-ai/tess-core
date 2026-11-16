from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import tess_core._facade as core
from tess_core.payloads.tools import apply_default_glob_excludes
from tests.support.tess_core_case import TessCoreTestCase, run

FALLBACK = [sys.executable, "-I", "-B", str(Path(core.__file__).with_name("tools") / "search_fallback.py")]


def _tree(root: Path) -> None:
    for folder in ("src/pkg", "docs", "node_modules/x", ".hidden", "build"):
        (root / folder).mkdir(parents=True)
    (root / "src/app.py").write_text("import os\n# TODO: one\ndef a():\n    return 1\n\n# todo: two\nclass B:\n    pass\n", encoding="utf-8")
    (root / "src/pkg/mod.py").write_text("x = 1\nTODO three\n", encoding="utf-8")
    (root / "docs/guide.md").write_text("# Title\n\nTODO four\nline\nline\nTODO five\n", encoding="utf-8")
    (root / ".hidden/h.py").write_text("TODO hidden\n", encoding="utf-8")
    (root / "node_modules/x/i.js").write_text("TODO nm\n", encoding="utf-8")
    (root / "build/out.py").write_text("TODO build\n", encoding="utf-8")
    (root / "src/blob.bin").write_bytes(b"\x00\x01TODO bin\n")
    (root / "data.json").write_text('{"a": "TODO json"}\n', encoding="utf-8")


class SearchWithoutRipgrepTests(TessCoreTestCase):
    """Glob and Grep work on a machine with no ripgrep, and say what ripgrep would."""

    def _tools(self, workspace: Path, home: Path):
        config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home)}, str(workspace)))
        session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=config["permissionMode"])
        context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}

        async def no_ripgrep():
            return None

        tools = {tool.name: tool for tool in core.create_builtin_tools({"search": {"resolveBinary": no_ripgrep}})}
        return tools, context

    def test_glob_and_grep_run_on_the_built_in_search(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _tree(workspace)
            tools, context = self._tools(workspace, home)
            listing = json.loads(run(tools["Glob"].handler({"pattern": "**/*.py"}, context))["content"])
            self.assertEqual(listing["matches"], [str(workspace / "src/app.py"), str(workspace / "src/pkg/mod.py")])

            files = json.loads(run(tools["Grep"].handler({"pattern": "TODO"}, context))["content"])
            self.assertEqual(files["matches"], [str(workspace / "data.json"), str(workspace / "docs/guide.md"), str(workspace / "src/app.py"), str(workspace / "src/pkg/mod.py")])

            lines = json.loads(run(tools["Grep"].handler({"pattern": "todo", "-i": True, "output_mode": "content", "glob": "*.py", "path": str(workspace / "src")}, context))["content"])
            self.assertEqual(lines["matches"], [f"{workspace}/src/app.py:2:# TODO: one", f"{workspace}/src/app.py:6:# todo: two", f"{workspace}/src/pkg/mod.py:2:TODO three"])

            bad = run(tools["Grep"].handler({"pattern": "(unclosed"}, context))
            self.assertTrue(bad.get("isError"))
            self.assertIn("regex parse error", bad["content"])
        finally:
            self.restore_home(root, previous)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is not installed; nothing to compare with")
    def test_built_in_search_prints_what_ripgrep_prints(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "project"
            root.mkdir()
            _tree(root)
            where = str(root)

            def files(*args):
                return apply_default_glob_excludes(["--files", "--sort", "path", *args])

            def grep(mode, *args):
                return apply_default_glob_excludes(["--color", "never", "--sort", "path", mode]) + list(args)

            cases = [
                files("-g", "**/*.py") + [where], files("-g", "*.md") + [where], files("-g", "src/**") + [where], files("-g", "*.{py,md}") + [where],
                files("-g", "*.zzz") + [where], files("-g", "**/*") + [where], files("-g", "?pp.py") + [where], files("-g", "src/*.py") + [where],
                grep("-l", "TODO", where), grep("-c", "TODO", where), grep("-n", "TODO", where), grep("-n", "-i", "todo", where),
                grep("-n", "-C", "1", "TODO", where), grep("-n", "-A", "1", "TODO", f"{where}/docs"), grep("-n", "-B", "1", "TODO", where),
                grep("-n", "--glob", "*.py", "TODO", where), grep("-n", "--type", "md", "TODO", where), grep("-l", "--type", "py", "TODO", where),
                grep("-n", "TODO", f"{where}/src/app.py"), grep("-c", "TODO", f"{where}/src/app.py"), grep("-l", "TODO", f"{where}/src/app.py"),
                grep("-n", "-C", "1", "TODO", f"{where}/docs/guide.md"), grep("-n", "-U", "--multiline-dotall", "def a.*?return", where),
                grep("-n", r"^class \w+:", where), grep("-n", "NOPE_NOTHING", where), grep("-n", "TODO", f"{where}/src/blob.bin"),
            ]
            environment = {"PATH": os.environ.get("PATH", "")}
            for args in cases:
                with self.subTest(args=" ".join(args[-3:]).replace(where, "<root>")):
                    ripgrep = subprocess.run([shutil.which("rg"), *args], capture_output=True, text=True, cwd=where, env=environment)
                    built_in = subprocess.run([*FALLBACK, *args], capture_output=True, text=True, cwd=where, env=environment)
                    self.assertEqual((built_in.returncode, built_in.stdout), (ripgrep.returncode, ripgrep.stdout))


if __name__ == "__main__":
    unittest.main()
