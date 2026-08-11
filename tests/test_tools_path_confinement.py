from __future__ import annotations

from tests.support.agent_core_case import *


# A real file that exists outside every test session's allowed roots (the
# repo lives outside tempdir, the temp workspace, and the temp runtime home).
OUTSIDE_FILE = os.path.abspath(__file__)
OUTSIDE_DIR = os.path.dirname(OUTSIDE_FILE)


class ToolPathConfinementTests(AgentCoreTestCase):
    def _context(self, home, workspace, *, mode="default", interactive=False, ask=None, add_dirs=None):
        options = {"provider": "openai", "runtimeHome": str(home), "permissionMode": mode}
        if add_dirs:
            options["addDir"] = list(add_dirs)
        config = run(core.resolveConfig(options, str(workspace)))
        session = core.createEmptySession(
            cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"],
            model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=mode,
        )
        return {
            "session": session, "config": config, "interactive": interactive,
            "askQuestion": ask or (lambda *_: "Deny"), "outputJson": lambda *_: None,
            "cwd": str(workspace), "persistSession": lambda: None, "abortSignal": None,
            "allowedRoots": config.get("allowedRoots", []),
        }

    def _tool(self, name):
        return next(tool for tool in core.createBuiltinTools() if tool.name == name)

    def test_reads_outside_roots_are_refused_without_an_approval_path(self):
        root, home, workspace, previous = self.temp_context()
        try:
            for mode, marker in (
                ("default", "approve interactively"),
                ("bypassPermissions", "containment sessions cannot widen"),
                ("auto", "non-interactive modes cannot approve"),
            ):
                with self.subTest(mode=mode):
                    context = self._context(home, workspace, mode=mode)
                    with self.assertRaises(PermissionError) as refusal:
                        run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, context))
                    self.assertIn("outside this session's allowed roots", str(refusal.exception))
                    self.assertIn(marker, str(refusal.exception))
        finally:
            self.restore_home(root, previous)

    def test_bypass_refuses_even_interactively_and_default_asks(self):
        root, home, workspace, previous = self.temp_context()
        try:
            asked = []

            def approve(prompt, options, *_detail):
                asked.append(options)
                return "Approve once"

            bypass = self._context(home, workspace, mode="bypassPermissions", interactive=True, ask=approve)
            with self.assertRaises(PermissionError):
                run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, bypass))
            self.assertEqual(asked, [])

            interactive = self._context(home, workspace, interactive=True, ask=approve)
            approved = run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, interactive))
            self.assertFalse(approved.get("isError"))
            self.assertEqual(asked, [["Approve once", "Deny"]])

            denying = self._context(home, workspace, interactive=True, ask=lambda *_: "Deny")
            with self.assertRaises(PermissionError):
                run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, denying))
        finally:
            self.restore_home(root, previous)

    def test_workspace_runtime_home_and_add_dir_roots_are_admitted(self):
        root, home, workspace, previous = self.temp_context()
        try:
            inside = workspace / "inside.txt"
            inside.write_text("workspace content\n", encoding="utf-8")
            context = self._context(home, workspace)
            read = run(self._tool("Read").handler({"file_path": str(inside)}, context))
            self.assertFalse(read.get("isError"))

            home_file = home / "home-note.txt"
            home_file.write_text("runtime home content\n", encoding="utf-8")
            from_home = run(self._tool("Read").handler({"file_path": str(home_file)}, context))
            self.assertFalse(from_home.get("isError"))

            granted = self._context(home, workspace, add_dirs=[OUTSIDE_DIR])
            from_repo = run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, granted))
            self.assertFalse(from_repo.get("isError"))
        finally:
            self.restore_home(root, previous)

    def test_symlinks_cannot_smuggle_paths_outside_the_roots(self):
        root, home, workspace, previous = self.temp_context()
        try:
            link = workspace / "escape"
            link.symlink_to(OUTSIDE_DIR)
            context = self._context(home, workspace)
            with self.assertRaises(PermissionError) as refusal:
                run(self._tool("Read").handler({"file_path": str(link / os.path.basename(OUTSIDE_FILE))}, context))
            self.assertIn("outside this session's allowed roots", str(refusal.exception))
        finally:
            self.restore_home(root, previous)

    def test_write_edit_glob_and_grep_are_confined_before_any_side_effect(self):
        root, home, workspace, previous = self.temp_context()
        try:
            context = self._context(home, workspace)
            target_dir = os.path.join(OUTSIDE_DIR, "confinement-should-never-create")
            target = os.path.join(target_dir, "escape.txt")
            with self.assertRaises(PermissionError):
                run(self._tool("Write").handler({"file_path": target, "content": "x"}, context))
            self.assertFalse(os.path.exists(target_dir))
            with self.assertRaises(PermissionError):
                run(self._tool("Edit").handler({"file_path": OUTSIDE_FILE, "old_string": "a", "new_string": "b"}, context))
            with self.assertRaises(PermissionError):
                run(self._tool("Glob").handler({"pattern": "*.py", "path": OUTSIDE_DIR}, context))
            with self.assertRaises(PermissionError):
                run(self._tool("Grep").handler({"pattern": "class", "path": OUTSIDE_DIR}, context))
        finally:
            self.restore_home(root, previous)

    def test_captured_authority_readable_roots_govern_confinement(self):
        root, home, workspace, previous = self.temp_context()
        try:
            context = self._context(home, workspace)
            with self.assertRaises(PermissionError):
                run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, context))
            context["session"]["metadata"]["effectiveAgentAuthority"] = {
                "readableRoots": [os.path.realpath(str(workspace)), os.path.realpath(OUTSIDE_DIR)],
            }
            granted = run(self._tool("Read").handler({"file_path": OUTSIDE_FILE}, context))
            self.assertFalse(granted.get("isError"))
        finally:
            self.restore_home(root, previous)
