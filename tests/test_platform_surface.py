from __future__ import annotations

from tests.support.agent_core_case import *


class PlatformSurfaceTests(AgentCoreTestCase):
    def test_platform_detection_tmux_ide_chrome_and_doctor(self):
        root, home, workspace, previous = self.temp_context()
        try:
            calls = []

            def which(command):
                return {"tmux": "/usr/bin/tmux", "code": "/usr/local/bin/code", "google-chrome": "/usr/bin/google-chrome"}.get(command)

            def exec_text(command, args):
                calls.append(("exec_text", command, tuple(args)))
                return "tmux 3.4\n" if args == ["-V"] else ""

            def exec_cmd(command, args, cwd=None):
                calls.append(("exec", command, tuple(args), cwd))
                return ""

            def spawn_detached(command, args, cwd=None):
                calls.append(("spawn", command, tuple(args), cwd))
                return 4321

            runner = {"which": which, "exec_text": exec_text, "exec": exec_cmd, "spawn_detached": spawn_detached}
            tmux = run(core.detect_tmux_availability({"runner": runner}))
            self.assertTrue(tmux["available"])
            self.assertEqual(tmux["version"], "tmux 3.4")
            self.assertNotIn("path", tmux)
            self.assertEqual(run(core.detect_tmux_availability({"runner": {"which": lambda _command: None}})), {"available": False})
            self.assertEqual(core.quote_shell_arg("abc:/tmp/file"), "abc:/tmp/file")
            self.assertEqual(core.build_shell_command("/bin/echo", ["hello world"], {"KEEP": "yes", "DROP": ""}), "KEEP=yes /bin/echo 'hello world'")

            handoff = run(
                core.maybe_exec_into_tmux(
                    {
                        "options": {"tmux": True},
                        "launch": {"cwd": str(workspace), "originalCwd": str(workspace), "worktreePath": str(workspace / "wt")},
                        "argv": ["--tmux", "--worktree", str(workspace / "wt"), "-w", "ignored", "--foo"],
                        "runner": runner,
                        "env": {},
                    }
                )
            )
            self.assertTrue(handoff["handled"])
            self.assertNotIn("executed", handoff)
            self.assertEqual(handoff["event"]["event"], "platform.tmux_attached")
            self.assertTrue(any(call[0] == "exec" and call[2][0] == "new-session" for call in calls))
            tmux_command = next(call[2][-1] for call in calls if call[0] == "exec" and call[2][0] == "new-session")
            self.assertIn("--foo", tmux_command)
            self.assertNotIn("--worktree", tmux_command)
            self.assertEqual(core.resolve_tmux_launch_context_from_env({"TESS_LAUNCH_CWD": "/w", "TESS_LAUNCH_ORIGINAL_CWD": "/o", "TESS_LAUNCH_WORKTREE_PATH": "/wt", "TESS_TMUX_SESSION_NAME": "s"})["tmuxSessionName"], "s")
            self.assertIsNone(core.resolve_tmux_launch_context_from_env({"TMUX": "/tmp/tmux.sock"}))
            self.assertIn("requires `--worktree`", run(core.maybe_exec_into_tmux({"options": {"tmux": True}, "launch": {"cwd": str(workspace)}, "runner": runner, "env": {}}))["error"])

            ide_targets = run(core.detect_ide_targets({"runner": runner, "env": {}}))
            self.assertEqual(ide_targets[0]["id"], "vscode")
            self.assertNotIn("name", ide_targets[0])
            ide_connect = run(core.maybe_auto_connect_ide(str(workspace), {"runner": runner, "env": {}}))
            self.assertEqual(ide_connect["status"], "connected")
            self.assertNotIn("connected", ide_connect)
            self.assertTrue(any(call[0] == "spawn" and call[2] == ("-r", str(workspace)) for call in calls))
            env_ide = run(core.detect_ide_targets({"runner": {"which": lambda _command: None}, "env": {"TERM_PROGRAM": "vscode", "CURSOR_TRACE_ID": "1"}}))
            self.assertEqual(env_ide[0]["id"], "cursor")
            self.assertEqual(run(core.detect_ide_targets({"runner": {"which": lambda _command: None}, "env": {"TERM_PROGRAM": "windsurf"}}))[0]["id"], "windsurf")
            self.assertEqual(run(core.detect_ide_targets({"runner": {"which": lambda _command: None}, "env": {"CURSOR_TRACE_ID": "1"}}))[0]["id"], "cursor")
            self.assertEqual(run(core.maybe_auto_connect_ide(str(workspace), {"runner": {"which": lambda _command: None}, "env": {}}))["status"], "skipped")
            multi_ide = run(core.maybe_auto_connect_ide(str(workspace), {"runner": {"which": lambda command: f"/usr/bin/{command}" if command in {"code", "cursor"} else None}, "env": {}}))
            self.assertEqual(multi_ide["status"], "skipped")
            self.assertNotIn("targets", multi_ide)
            failed_ide = run(core.maybe_auto_connect_ide(str(workspace), {"runner": {"which": lambda command: "/usr/bin/code" if command == "code" else None, "spawn_detached": lambda *_: (_ for _ in ()).throw(RuntimeError("launch failed"))}, "env": {}}))
            self.assertEqual(failed_ide["status"], "skipped")
            self.assertNotIn("target", failed_ide)

            mtime_root = workspace / "mtime"
            (mtime_root / ".git").mkdir(parents=True)
            (mtime_root / "node_modules").mkdir()
            included = mtime_root / "included.txt"
            ignored_git = mtime_root / ".git" / "ignored.txt"
            ignored_modules = mtime_root / "node_modules" / "ignored.txt"
            included.write_text("old", encoding="utf-8")
            ignored_git.write_text("new", encoding="utf-8")
            ignored_modules.write_text("new", encoding="utf-8")
            os.utime(included, (1000, 1000))
            os.utime(ignored_git, (3000, 3000))
            os.utime(ignored_modules, (4000, 4000))
            self.assertEqual(run(core.newest_mtime_in_tree(str(mtime_root))), 1000)
            self.assertIsNone(run(core.newest_mtime_in_tree(str(workspace / "missing-mtime"))))

            chrome = run(core.detect_chrome_companion_status({"runner": runner, "platform": "linux"}))
            self.assertTrue(chrome["supported"])
            self.assertEqual(chrome["browserBinary"], "/usr/bin/google-chrome")
            chrome_server = run(core.resolve_chrome_companion_server({"runner": runner, "platform": "linux"}))
            self.assertTrue(chrome_server["available"])
            self.assertTrue(chrome_server["enabled"])
            self.assertEqual(chrome_server["server"]["args"][:2], ["-m", "agent_core.platform_support.chrome_companion"])
            rpc_status = run(core.handle_chrome_companion_rpc_payload(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "chrome_status"}})))
            self.assertEqual(rpc_status["id"], 7)
            self.assertEqual(rpc_status["result"]["content"][0]["type"], "text")
            initialize_payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()
            framed = initialize_payload + b"\n"
            mcp_out = io.BytesIO()
            run(core.run_chrome_companion_mcp_server(io.BytesIO(framed), mcp_out))
            self.assertIn(b"tess-chrome-companion", mcp_out.getvalue())
            native_payload = json.dumps({"type": "ping"}).encode()
            native_out = io.BytesIO()
            run(core.run_chrome_native_host(io.BytesIO(len(native_payload).to_bytes(4, "little") + native_payload), native_out))
            native_raw = native_out.getvalue()
            native_length = int.from_bytes(native_raw[:4], "little")
            self.assertEqual(json.loads(native_raw[4 : 4 + native_length])["type"], "pong")

            install = run(core.inspect_install_state({"runner": runner}))
            self.assertTrue(install["installed"])
            self.assertEqual(install["mode"], "repo-only")
            install_result = run(core.run_repo_install({"runner": runner, "python": "/usr/bin/python3"}))
            self.assertEqual(install_result["steps"][0]["step"], "link")
            self.assertEqual(install_result["steps"][0]["status"], "run")
            self.assertTrue(any(call[0] == "exec" and call[1] == "/usr/bin/python3" and call[2][:4] == ("-m", "pip", "install", "-e") for call in calls))
            linked_install = run(core.run_repo_install({"runner": runner, "python": "/usr/bin/python3", "globalPackagePath": str(core.REPO_ROOT), "globalBinPath": "/usr/local/bin/agent-core"}))
            self.assertEqual(linked_install["steps"][0]["status"], "skipped")
            self.assertEqual(run(core.run_repo_update({"runner": runner}))["upstreamStatus"], "skipped")
            with self.assertRaises(RuntimeError):
                run(core.run_repo_install({"runner": runner, "target": "nightly"}))

            fake_repo = workspace / "platform-repo"
            (fake_repo / "agent_core").mkdir(parents=True)
            (fake_repo / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
            git_calls = []
            git_state = {"head": "aaa", "dirty": "", "has_upstream": True}

            def git_which(command):
                return "/usr/bin/git" if command == "git" else None

            def git_exec(command, args, cwd=None):
                git_calls.append((command, tuple(args), cwd))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(fake_repo)
                if args == ["status", "--porcelain"]:
                    return git_state["dirty"]
                if args == ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
                    if not git_state["has_upstream"]:
                        raise RuntimeError("no upstream")
                    return "origin/main\n"
                if args == ["rev-parse", "HEAD"]:
                    return f"{git_state['head']}\n"
                if args == ["pull", "--ff-only"]:
                    git_state["head"] = "bbb"
                    return "updated\n"
                return ""

            update = run(core.run_repo_update({"runner": {"which": git_which, "exec": git_exec}, "repoRoot": str(fake_repo)}))
            self.assertEqual(update["upstreamStatus"], "updated")
            self.assertEqual(update["steps"][0]["step"], "git_pull")
            self.assertTrue(any(call[1] == ("pull", "--ff-only") for call in git_calls))
            self.assertEqual(update["installStateAfter"]["repoVersion"], "1.2.3")

            git_state["head"] = "bbb"
            git_calls.clear()
            up_to_date = run(core.run_repo_update({"runner": {"which": git_which, "exec": git_exec}, "repoRoot": str(fake_repo)}))
            self.assertEqual(up_to_date["upstreamStatus"], "up-to-date")
            self.assertTrue(any(call[1] == ("pull", "--ff-only") for call in git_calls))

            git_state["dirty"] = " M file.py\n"
            git_calls.clear()
            dirty = run(core.run_repo_update({"runner": {"which": git_which, "exec": git_exec}, "repoRoot": str(fake_repo)}))
            self.assertEqual(dirty["upstreamStatus"], "skipped")
            self.assertIn("dirty", dirty["upstreamDetail"])
            self.assertFalse(any(call[1] == ("pull", "--ff-only") for call in git_calls))

            report = run(core.collect_doctor_report(str(workspace), {"runner": runner, "platform": "linux"}))
            self.assertTrue(report["healthy"])
            check_ids = {check["id"] for check in report["checks"]}
            self.assertTrue({"auth", "dist", "global_link", "package", "tmux", "ide", "chrome", "daemon", "git", "workspace", "extensions"}.issubset(check_ids))
            self.assertEqual(next(check for check in report["checks"] if check["id"] == "tmux")["status"], "pass")
            self.assertEqual(next(check for check in report["checks"] if check["id"] == "global_link")["status"], "warn")
            self.assertEqual(next(check for check in report["checks"] if check["id"] == "daemon")["status"], "warn")
            run(core.save_daemon_state("doctor-instance", {"phase": "running", "pid": os.getpid(), "socketPath": str(workspace / "daemon.sock"), "supervisorId": "sup", "epoch": 1, "updatedAt": "2026-01-01T00:00:00Z"}))
            daemon_report = run(core.collect_doctor_report(str(workspace), {"runner": runner, "platform": "linux"}))
            self.assertEqual(next(check for check in daemon_report["checks"] if check["id"] == "daemon")["status"], "pass")
        finally:
            self.restore_home(root, previous)

    def test_platform_install_state_tracks_source_staleness_and_global_path(self):
        root, home, workspace, previous = self.temp_context()
        try:
            fake_repo = workspace / "install-state"
            source = fake_repo / "agent_core"
            dist = fake_repo / "dist_pkg"
            bundled = fake_repo / "package"
            source.mkdir(parents=True)
            dist.mkdir(parents=True)
            bundled.mkdir(parents=True)
            (fake_repo / "pyproject.toml").write_text('[project]\nname = "agent-core-python"\nversion = "1.2.3"\n', encoding="utf-8")
            (bundled / "pyproject.toml").write_text('[project]\nversion = "1.1.0"\n', encoding="utf-8")
            source_file = source / "new.py"
            dist_file = dist / "old.py"
            source_file.write_text("new", encoding="utf-8")
            dist_file.write_text("old", encoding="utf-8")
            os.utime(source_file, (3000, 3000))
            os.utime(dist_file, (1000, 1000))
            calls = []

            def which(command):
                return "/usr/local/bin/agent-core" if command == "agent-core" else None

            def exec_cmd(command, args, cwd=None):
                calls.append((command, tuple(args), cwd))
                if args[:3] == ["-m", "pip", "show"]:
                    return f"Name: agent-core-python\nVersion: 1.2.3\nEditable-project-location: {fake_repo}\n"
                return ""

            state = run(
                core.inspect_install_state(
                    {
                        "repoRoot": str(fake_repo),
                        "sourcePath": str(source),
                        "distPath": str(dist),
                        "bundledPyprojectPath": str(bundled / "pyproject.toml"),
                        "runner": {"which": which, "exec": exec_cmd},
                        "python": "/usr/bin/python3",
                    }
                )
            )
            self.assertEqual(state["repoVersion"], "1.2.3")
            self.assertEqual(state["bundledReleasedVersion"], "1.1.0")
            self.assertTrue(state["distExists"])
            self.assertTrue(state["distStale"])
            self.assertEqual(state["mode"], "linked-global")
            self.assertEqual(state["globalPackagePath"], str(fake_repo))
            self.assertTrue(any(call[1][:3] == ("-m", "pip", "show") for call in calls))

            os.utime(dist_file, (4000, 4000))
            fresh = run(core.inspect_install_state({"repoRoot": str(fake_repo), "sourcePath": str(source), "distPath": str(dist), "globalPackagePath": str(workspace / "global-copy")}))
            self.assertFalse(fresh["distStale"])
            self.assertEqual(fresh["mode"], "global-copy")
        finally:
            self.restore_home(root, previous)

    def test_platform_chrome_native_host_manifest_install_and_inspection(self):
        root, home, workspace, previous = self.temp_context()
        try:
            manifest_path = workspace / "native-hosts" / "com.example.tess.json"
            state_root = workspace / "state"
            planned = run(
                core.install_chrome_native_host_manifest(
                    {
                        "platform": "linux",
                        "home": str(home),
                        "stateRoot": str(state_root),
                        "manifestPath": str(manifest_path),
                        "hostName": "com.example.tess",
                        "allowedOrigins": ["chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/"],
                        "dryRun": True,
                    }
                )
            )
            self.assertEqual(planned["status"], "planned")
            self.assertFalse(manifest_path.exists())
            self.assertFalse(Path(planned["expectedManifest"]["path"]).exists())

            installed = run(
                core.install_chrome_native_host_manifest(
                    {
                        "platform": "linux",
                        "home": str(home),
                        "stateRoot": str(state_root),
                        "manifestPath": str(manifest_path),
                        "hostName": "com.example.tess",
                        "allowedOrigins": ["chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/"],
                    }
                )
            )
            self.assertEqual(installed["status"], "installed")
            self.assertTrue(installed["installed"])
            self.assertTrue(manifest_path.exists())
            host_path = Path(installed["hostPath"])
            self.assertTrue(host_path.exists())
            self.assertTrue(os.access(host_path, os.X_OK))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], "com.example.tess")
            self.assertEqual(manifest["type"], "stdio")
            self.assertEqual(manifest["path"], str(host_path))

            inspected = run(core.inspect_chrome_native_host_manifest({"platform": "linux", "manifestPath": str(manifest_path), "hostName": "com.example.tess", "nativeHostPath": str(host_path), "allowedOrigins": manifest["allowed_origins"]}))
            self.assertTrue(inspected["installed"])
            self.assertEqual(inspected["issues"], [])
            unsupported = run(core.inspect_chrome_native_host_manifest({"platform": "freebsd", "manifestPath": str(manifest_path)}))
            self.assertFalse(unsupported["supported"])
            self.assertIn("Unsupported platform", unsupported["issues"][0])

            report = run(core.collect_doctor_report(str(workspace), {"runner": {"which": lambda _command: None}, "platform": "linux", "manifestPath": str(manifest_path), "hostName": "com.example.tess", "nativeHostPath": str(host_path), "allowedOrigins": manifest["allowed_origins"]}))
            native_check = next(check for check in report["checks"] if check["id"] == "chrome_native_host")
            self.assertEqual(native_check["status"], "pass")
        finally:
            self.restore_home(root, previous)

    def test_platform_launch_metadata_capture_for_workspace_integrations(self):
        session = {"id": "session-platform-metadata", "cwd": "/tmp/work", "metadata": {}}
        tmux_result = {
            "handled": True,
            "event": {
                "event": "platform.tmux_attached",
                "data": {
                    "mode": "default",
                    "tmux_session_name": "tess-work",
                    "tmux_window_name": "work",
                    "cwd": "/tmp/work",
                },
            },
        }
        ide_result = {
            "status": "connected",
            "target": {"id": "cursor", "displayName": "Cursor", "command": "cursor", "source": "env"},
        }
        chrome_result = {
            "enabled": True,
            "server": {"name": "chrome_companion"},
            "status": {
                "platform": "linux",
                "browserBinary": "/usr/bin/google-chrome",
                "nativeHostModeSupported": True,
                "nativeHostManifest": {"manifestPath": "/tmp/native-host.json"},
            },
        }
        metadata = core.apply_platform_integration_metadata(session, {"tmux": tmux_result, "ide": ide_result, "chrome": chrome_result, "updatedAt": "2026-01-01T00:00:00Z"})
        self.assertEqual(metadata["tmuxSessionName"], "tess-work")
        self.assertEqual(metadata["tmuxWindowName"], "work")
        self.assertEqual(metadata["tmuxMode"], "default")
        self.assertEqual(metadata["tmuxLastEvent"], "platform.tmux_attached")
        self.assertEqual(metadata["ideName"], "Cursor")
        self.assertEqual(metadata["ideTargetId"], "cursor")
        self.assertEqual(metadata["ideSource"], "env")
        self.assertTrue(metadata["chromeIntegrationEnabled"])
        self.assertEqual(metadata["chromeIntegrationMode"], "mcp")
        self.assertEqual(metadata["chromeBrowserBinary"], "/usr/bin/google-chrome")
        self.assertEqual(metadata["chromeNativeHostManifestPath"], "/tmp/native-host.json")
