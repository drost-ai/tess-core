from __future__ import annotations

from tests.support.agent_core_case import *


class ToolWorktreeNotebookTests(AgentCoreTestCase):
    def test_enter_worktree_fallback_uses_reference_branch_and_target_naming(self):
        if not shutil.which("git"):
            self.skipTest("git is required for worktree parity")
        root, home, workspace, previous = self.temp_context()
        try:
            subprocess.run(["git", "init"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "agent-core@example.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Agent Core"], cwd=workspace, check=True)
            (workspace / "README.md").write_text("root\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            tools = {tool.name: tool for tool in core.createBuiltinTools()}
            cwd_updates = []

            def update_cwd(next_cwd):
                cwd_updates.append(next_cwd)
                session["cwd"] = next_cwd

            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "updateCwd": update_cwd}
            entered = json.loads(run(tools["EnterWorktree"].handler({"name": " Feature Work "}, context))["content"])
            self.assertRegex(entered["branch"], r"^tess/feature-work-[0-9a-f]{6}$")
            self.assertRegex(Path(entered["cwd"]).name, rf"^{re.escape(workspace.name)}-feature-work-[0-9a-f]{{6}}$")
            self.assertEqual(session["cwd"], entered["cwd"])
            self.assertEqual(session["metadata"]["worktreePath"], entered["cwd"])
            self.assertNotIn("workspace", entered)
            self.assertNotIn("execution", entered)

            exited = json.loads(run(tools["ExitWorktree"].handler({}, context))["content"])
            self.assertEqual(exited, {"cwd": str(workspace)})
            self.assertEqual(cwd_updates, [entered["cwd"], str(workspace)])
        finally:
            self.restore_home(root, previous)

    def test_create_git_worktree_reports_target_and_branch_collisions(self):
        if not shutil.which("git"):
            self.skipTest("git is required for worktree parity")
        root, home, workspace, previous = self.temp_context()
        try:
            subprocess.run(["git", "init"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "agent-core@example.test"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Agent Core"], cwd=workspace, check=True)
            (workspace / "README.md").write_text("root\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            existing_target = Path(root.name) / "existing-worktree"
            existing_target.mkdir()
            with self.assertRaisesRegex(RuntimeError, "Worktree target already exists"):
                run(core.createGitWorktree(cwd=str(workspace), name="collision", path=str(existing_target), branchName="tess/collision"))

            subprocess.run(["git", "branch", "tess/collision"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            with self.assertRaisesRegex(RuntimeError, "Worktree branch already exists"):
                run(core.createGitWorktree(cwd=str(workspace), name="collision", path=str(Path(root.name) / "new-worktree"), branchName="tess/collision"))
        finally:
            self.restore_home(root, previous)

    def test_enter_worktree_does_not_fabricate_missing_original_cwd(self):
        root, home, workspace, previous = self.temp_context()
        try:
            worktree = Path(root.name) / "raw-worktree"
            worktree.mkdir()
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = {"id": "raw-worktree-session", "cwd": str(workspace), "metadata": {}}

            class Controller:
                async def createWorktree(self, requested_name):
                    return {"worktree": {"targetPath": str(worktree), "branchName": f"tess/{requested_name}"}, "snapshot": None}

                async def exitWorktree(self):
                    return None

            tools = {tool.name: tool for tool in core.createBuiltinTools({"workspaceController": Controller()})}
            cwd_updates = []

            def update_cwd(next_cwd):
                cwd_updates.append(next_cwd)
                session["cwd"] = next_cwd

            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "updateCwd": update_cwd}
            entered = json.loads(run(tools["EnterWorktree"].handler({"name": "Raw"}, context))["content"])
            self.assertEqual(entered, {"cwd": str(worktree), "branch": "tess/Raw", "created": True})
            self.assertEqual(session["metadata"], {"worktreePath": str(worktree)})

            exited = json.loads(run(tools["ExitWorktree"].handler({}, context))["content"])
            self.assertEqual(exited, {})
            self.assertEqual(cwd_updates, [str(worktree), None])
        finally:
            self.restore_home(root, previous)

    def test_worktree_tools_update_workspace_metadata_and_execution_descriptors(self):
        root, home, workspace, previous = self.temp_context()
        try:
            worktree = Path(root.name) / "workspace-slice-5"
            worktree.mkdir()
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")

            enter_snapshot = {
                "workspaceId": "workspace_slice_5",
                "workspaceName": "workspace-slice-5",
                "workspaceRoot": str(worktree),
                "cwd": str(worktree),
                "mode": "worktree",
                "gitRoot": str(workspace),
                "gitBranch": "tess/slice-5",
                "worktreePath": str(worktree),
            }
            exit_snapshot = {
                "workspaceId": "workspace",
                "workspaceName": "workspace",
                "workspaceRoot": str(workspace),
                "cwd": str(workspace),
                "mode": "repo",
                "gitRoot": str(workspace),
                "gitBranch": "main",
                "worktreePath": str(worktree),
            }

            class WorktreeController:
                async def createWorktree(self, requested_name):
                    return {
                        "worktree": {
                            "gitRoot": str(workspace),
                            "targetPath": str(worktree),
                            "branchName": "tess/slice-5",
                            "requestedName": requested_name,
                        },
                        "snapshot": enter_snapshot,
                    }

                async def exitWorktree(self):
                    return exit_snapshot

            tools = core.createBuiltinTools({"workspaceController": WorktreeController()})
            enter = next(tool for tool in tools if tool.name == "EnterWorktree")
            exit_tool = next(tool for tool in tools if tool.name == "ExitWorktree")
            cwd_updates = []

            def update_cwd(next_cwd):
                cwd_updates.append(next_cwd)
                session["cwd"] = next_cwd

            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "updateCwd": update_cwd}
            entered = run(enter.handler({"name": "slice-5"}, context))
            entered_payload = json.loads(entered["content"])
            self.assertEqual(entered_payload["cwd"], str(worktree))
            self.assertEqual(entered_payload["workspace"]["mode"], "worktree")
            self.assertEqual(entered_payload["execution"]["id"], "workspace_slice_5:enter")
            self.assertEqual(entered["meta"]["execution"]["metadata"]["worktree_path"], str(worktree))
            self.assertEqual(session["cwd"], str(worktree))
            self.assertEqual(session["metadata"]["originalCwd"], str(workspace))
            self.assertEqual(session["metadata"]["worktreePath"], str(worktree))
            self.assertEqual(session["metadata"]["workspaceMode"], "worktree")

            exited = run(exit_tool.handler({}, context))
            exited_payload = json.loads(exited["content"])
            self.assertEqual(exited_payload["cwd"], str(workspace))
            self.assertEqual(exited_payload["workspace"]["mode"], "repo")
            self.assertEqual(exited_payload["execution"]["id"], "workspace:exit")
            self.assertEqual(exited["meta"]["execution"]["title"], "Returned to workspace workspace")
            self.assertEqual(session["cwd"], str(workspace))
            self.assertEqual(session["metadata"]["worktreePath"], str(worktree))
            self.assertEqual(session["metadata"]["workspaceMode"], "repo")
            self.assertEqual(cwd_updates, [str(worktree), str(workspace)])

            bare_worktree = Path(root.name) / "workspace-no-snapshot"
            bare_worktree.mkdir()

            class BareWorktreeController:
                async def createWorktree(self, requested_name):
                    return {
                        "worktree": {
                            "gitRoot": str(workspace),
                            "targetPath": str(bare_worktree),
                            "branchName": f"tess/{requested_name}",
                            "requestedName": requested_name,
                        }
                    }

                async def exitWorktree(self):
                    return None

            bare_session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            bare_tools = core.createBuiltinTools({"workspaceController": BareWorktreeController()})
            bare_enter = next(tool for tool in bare_tools if tool.name == "EnterWorktree")
            bare_exit = next(tool for tool in bare_tools if tool.name == "ExitWorktree")
            bare_context = {"session": bare_session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "updateCwd": lambda next_cwd: bare_session.__setitem__("cwd", next_cwd)}

            bare_entered = run(bare_enter.handler({"name": "no-snapshot"}, bare_context))
            bare_entered_payload = json.loads(bare_entered["content"])
            self.assertEqual(bare_entered_payload, {"cwd": str(bare_worktree), "branch": "tess/no-snapshot", "created": True})
            self.assertNotIn("meta", bare_entered)

            bare_exited = run(bare_exit.handler({}, bare_context))
            bare_exited_payload = json.loads(bare_exited["content"])
            self.assertEqual(bare_exited_payload, {"cwd": str(workspace)})
            self.assertNotIn("meta", bare_exited)

            missing_original_session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            missing_original_session["metadata"].pop("originalCwd", None)
            missing_updates = []
            missing_context = {
                "session": missing_original_session,
                "config": config,
                "interactive": False,
                "askQuestion": lambda *_: "",
                "outputJson": lambda *_: None,
                "cwd": str(workspace),
                "updateCwd": lambda next_cwd: missing_updates.append(next_cwd),
            }
            missing_exit = run(bare_exit.handler({}, missing_context))
            self.assertEqual(json.loads(missing_exit["content"]), {})
            self.assertEqual(missing_updates, [None])
            self.assertIsNone(missing_original_session["cwd"])
        finally:
            self.restore_home(root, previous)

    def test_workspace_snapshot_detects_worktree_mode_from_metadata(self):
        root, home, workspace, previous = self.temp_context()
        try:
            worktree = Path(root.name) / "workspace-slice-5"
            worktree.mkdir()
            snapshot = run(core.captureWorkspaceSnapshot(str(worktree), {"originalCwd": str(workspace), "worktreePath": str(worktree)}))
            self.assertEqual(snapshot["mode"], "worktree")
            self.assertEqual(snapshot["worktreePath"], str(worktree))
            self.assertEqual(snapshot["workspaceId"], re.sub(r"[^a-zA-Z0-9]+", "_", str(worktree)).strip("_").lower())

            exited = run(core.captureWorkspaceSnapshot(str(workspace), {"originalCwd": str(workspace), "worktreePath": str(worktree)}))
            self.assertEqual(exited["mode"], "directory")
            self.assertEqual(exited["worktreePath"], str(worktree))
        finally:
            self.restore_home(root, previous)

    def test_notebook_edit_replace_insert_delete(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            notebook_path = workspace / "demo.ipynb"
            notebook_path.write_text(json.dumps({"cells": [{"id": "cell-1", "cell_type": "code", "source": ["print(1)"], "metadata": {}, "outputs": []}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "NotebookEdit")
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}
            replaced = run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "cell-1", "new_source": "print(2)"}, context))
            self.assertEqual(replaced, {"content": f"Updated notebook {notebook_path}"})
            self.assertEqual(json.loads(notebook_path.read_text())["cells"][0]["source"], ["print(2)"])
            self.assertTrue(notebook_path.read_text(encoding="utf-8").endswith("\n"))

            inserted = run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "cell-1", "new_source": "print(3)", "cell_type": "markdown", "edit_mode": "insert"}, context))
            self.assertEqual(inserted, {"content": f"Updated notebook {notebook_path}"})
            inserted_cells = json.loads(notebook_path.read_text())["cells"]
            self.assertEqual(len(inserted_cells), 2)
            self.assertEqual(inserted_cells[1]["cell_type"], "markdown")
            self.assertEqual(inserted_cells[1]["source"], ["print(3)"])
            self.assertEqual(inserted_cells[1]["metadata"], {})
            self.assertEqual(inserted_cells[1]["outputs"], [])

            deleted = run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "cell-1", "new_source": "ignored", "edit_mode": "delete"}, context))
            self.assertEqual(deleted, {"content": f"Updated notebook {notebook_path}"})
            cells = json.loads(notebook_path.read_text())["cells"]
            self.assertEqual(len(cells), 1)
            self.assertEqual(cells[0]["source"], ["print(3)"])

            missing = run(tool.handler({"notebook_path": str(notebook_path), "cell_id": "missing", "new_source": "ignored", "edit_mode": "delete"}, context))
            self.assertEqual(missing, {"content": "Cell not found", "isError": True})
        finally:
            self.restore_home(root, previous)
