from __future__ import annotations

from tests.support.agent_core_case import *
import agent_core.tools.web_search_api as web_search_api_module


class ToolPayloadSearchTests(AgentCoreTestCase):
    def test_default_glob_excludes_python_dependency_caches(self):
        excludes = core.getDefaultGlobExcludes()
        self.assertIn(".venv/**", excludes)
        self.assertIn("venv/**", excludes)
        self.assertIn("__pycache__/**", excludes)
        self.assertIn(".gradle/**", excludes)

        self.assertEqual(
            core.filterDefaultIgnoredGlobMatches(
                [
                    "/repo/src/app.py",
                    "/repo/.venv/lib/python/site-packages/pkg.py",
                    "/repo/venv/lib/python/site-packages/pkg.py",
                    "/repo/src/__pycache__/app.cpython-312.pyc",
                    "/repo/.gradle/caches/modules.lock",
                    "/repo/src/domain/model.py",
                ]
            ),
            ["/repo/src/app.py", "/repo/src/domain/model.py"],
        )

    def test_glob_tool_absolute_patterns_and_root_refusal(self):
        root, home, workspace, previous = self.temp_context()
        try:
            plan_dir = workspace / "product-planning" / "02-offensive-ceiling"
            plan_dir.mkdir(parents=True)
            (plan_dir / "README.md").write_text("# Plan\n", encoding="utf-8")
            (plan_dir / "tier1.md").write_text("# Tier 1\n", encoding="utf-8")
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home)}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=config["permissionMode"])
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "Glob")

            result = run(tool.handler({"pattern": str(plan_dir / "**" / "*"), "path": os.path.abspath(os.path.sep)}, context))
            self.assertFalse(result.get("isError"), result.get("content"))
            payload = json.loads(result["content"])
            self.assertEqual(payload["pattern"], "**/*")
            self.assertEqual(payload["path"], str(plan_dir))
            self.assertEqual(payload["totalMatches"], 2)
            self.assertEqual(sorted(payload["matches"]), sorted([str(plan_dir / "README.md"), str(plan_dir / "tier1.md")]))

            root_result = run(tool.handler({"pattern": "**/*", "path": os.path.abspath(os.path.sep)}, context))
            self.assertTrue(root_result.get("isError"))
            self.assertIn("refuses to search the filesystem root", root_result["content"])
        finally:
            self.restore_home(root, previous)

    def test_search_payload_sampling_truncation_and_artifacts_match_reference(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({
                "provider": "openai",
                "runtimeHome": str(home),
                "permissionMode": "bypassPermissions",
                "openaiBaseUrl": "http://compatible.test",
                "openaiApiMode": "chat",
                "model": "small-model",
            }, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            payload_config = {**config, "providerKind": "openai", "openaiBaseUrl": "http://compatible.test", "openaiApiMode": "chat", "model": "small-model"}

            glob_matches = [f"src/a/file-{index:03}.py" for index in range(80)]
            glob_matches.extend(f"src/b/file-{index:03}.py" for index in range(50))
            glob_matches.append("README.md")
            glob_payload = run(core.shapeGlobPayload(config=payload_config, sessionId=session["id"], pattern="*.py", searchPath=str(workspace), matches=glob_matches))

            self.assertEqual(glob_payload["type"], "glob")
            self.assertEqual(glob_payload["totalMatches"], 131)
            self.assertTrue(glob_payload["truncated"])
            self.assertEqual(glob_payload["offset"], 0)
            self.assertEqual(glob_payload["limit"], 80)
            self.assertEqual(glob_payload["returnedMatches"], 80)
            self.assertEqual(glob_payload["nextOffset"], 80)
            self.assertEqual(glob_payload["matches"][:3], [
                "src/a/file-000.py",
                "src/a/file-001.py",
                "src/a/file-002.py",
            ])
            self.assertEqual(len(glob_payload["matches"]), 80)
            self.assertEqual(glob_payload["directoryCounts"][:3], [
                {"directory": "src/a", "count": 80},
                {"directory": "src/b", "count": 50},
                {"directory": "(root)", "count": 1},
            ])
            self.assertEqual(glob_payload["transcriptView"]["strategy"], "count_and_sample")
            self.assertEqual(glob_payload["transcriptView"]["originalLines"], 131)
            self.assertEqual(glob_payload["transcriptView"]["returnedLines"], 80)
            glob_artifact = glob_payload["artifact"]
            self.assertEqual(glob_artifact["toolName"], "Glob")
            self.assertEqual(glob_artifact["family"], "glob")
            self.assertEqual(glob_artifact["contentType"], "paths")
            self.assertEqual(glob_artifact["itemCount"], 131)
            self.assertEqual(glob_artifact["lines"], 131)
            self.assertEqual(Path(glob_artifact["filePath"]).read_text(encoding="utf-8").splitlines(), glob_matches)

            next_glob_payload = run(core.shapeGlobPayload(config=payload_config, sessionId=session["id"], pattern="*.py", searchPath=str(workspace), matches=glob_matches, offset=glob_payload["nextOffset"], requestedLimit=10))
            self.assertEqual(next_glob_payload["offset"], 80)
            self.assertEqual(next_glob_payload["limit"], 10)
            self.assertEqual(next_glob_payload["matches"][0], "src/b/file-000.py")
            self.assertEqual(next_glob_payload["returnedMatches"], 10)

            grep_matches = [f"src/a.py:{line}:needle line {line}" for line in range(75)]
            grep_payload = run(core.shapeGrepPayload(config=payload_config, sessionId=session["id"], pattern="needle", searchPath=str(workspace), outputMode="content", matches=grep_matches, offset=10, requestedHeadLimit=0))

            self.assertEqual(grep_payload["type"], "grep")
            self.assertEqual(grep_payload["offset"], 10)
            self.assertEqual(grep_payload["headLimit"], 60)
            self.assertEqual(grep_payload["matches"][0], grep_matches[10])
            self.assertEqual(grep_payload["matches"][-1], grep_matches[69])
            self.assertEqual(len(grep_payload["matches"]), 60)
            self.assertEqual(grep_payload["totalMatches"], 75)
            self.assertTrue(grep_payload["truncated"])
            self.assertEqual(grep_payload["nextOffset"], 70)
            self.assertEqual(grep_payload["transcriptView"]["strategy"], "count_and_sample")
            self.assertEqual(grep_payload["transcriptView"]["originalLines"], 75)
            self.assertEqual(grep_payload["transcriptView"]["returnedLines"], 60)
            grep_artifact = grep_payload["artifact"]
            self.assertEqual(grep_artifact["toolName"], "Grep")
            self.assertEqual(grep_artifact["family"], "grep")
            self.assertEqual(grep_artifact["contentType"], "text")
            self.assertEqual(grep_artifact["itemCount"], 75)
            self.assertEqual(grep_artifact["lines"], 75)
            self.assertEqual(Path(grep_artifact["filePath"]).read_text(encoding="utf-8").splitlines(), grep_matches)

            tail_payload = run(core.shapeGrepPayload(config=payload_config, sessionId=session["id"], pattern="needle", searchPath=str(workspace), outputMode="content", matches=grep_matches, offset=70, requestedHeadLimit=0))
            self.assertFalse(tail_payload["truncated"])
            self.assertNotIn("nextOffset", tail_payload)
            self.assertEqual(tail_payload["transcriptView"]["strategy"], "inline_full")
            self.assertEqual(tail_payload["matches"], grep_matches[70:])
            self.assertIn("artifact", tail_payload)
        finally:
            self.restore_home(root, previous)

    def test_shell_and_task_output_manifests_persist_artifacts_match_reference(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = run(core.resolveConfig({
                "provider": "openai",
                "runtimeHome": str(home),
                "permissionMode": "bypassPermissions",
                "openaiBaseUrl": "http://compatible.test",
                "openaiApiMode": "chat",
                "model": "small-model",
            }, str(workspace)))
            config.update({"providerKind": "openai", "openaiBaseUrl": "http://compatible.test", "openaiApiMode": "chat", "model": "small-model"})
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            context = {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None}
            long_output = "\n".join(f"line-{index:03} {'x' * 40}" for index in range(220)) + "\n"

            bash = next(tool for tool in core.createBuiltinTools() if tool.name == "Bash")
            command = "python3 - <<'PY'\nfor index in range(220):\n    print(f\"line-{index:03} {'x' * 40}\")\nPY"
            bash_result = run(bash.handler({"command": command}, context))
            bash_payload = json.loads(bash_result["content"])
            self.assertFalse(bash_result.get("isError"))
            self.assertEqual(bash_payload["stderr"], "")
            self.assertNotIn("artifact", bash_payload["stderr_manifest"])
            self.assertEqual(bash_payload["stdout_manifest"]["strategy"], "inline_head_tail")
            self.assertTrue(bash_payload["stdout_manifest"]["truncated"])
            self.assertGreater(bash_payload["stdout_manifest"]["originalLines"], bash_payload["stdout_manifest"]["returnedLines"])
            bash_artifact = bash_payload["stdout_manifest"]["artifact"]
            self.assertEqual(bash_artifact["toolName"], "Bash")
            self.assertEqual(bash_artifact["family"], "bash")
            self.assertEqual(bash_artifact["contentType"], "text")
            self.assertEqual(Path(bash_artifact["filePath"]).read_text(encoding="utf-8"), long_output)

            task_id = "task-manifest-fixture"
            output_path = core.taskOutputPath(task_id)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(long_output, encoding="utf-8")
            run(core.writeTaskMeta(task_id, {"taskId": task_id, "id": task_id, "sessionId": session["id"], "command": "long task", "cwd": str(workspace), "startedAt": "2026-01-01T00:00:00Z", "status": "completed", "outputFile": output_path}))
            session["metadata"]["taskIds"] = [task_id]
            task_output = next(tool for tool in core.createBuiltinTools() if tool.name == "TaskOutput")
            task_result = run(task_output.handler({"task_id": task_id, "block": False, "timeout": 1000}, context))
            task_payload = json.loads(task_result["content"])
            self.assertEqual(task_payload["output_manifest"]["strategy"], "inline_head_tail")
            self.assertTrue(task_payload["output_manifest"]["truncated"])
            self.assertFalse(task_payload["output_truncated"])
            task_artifact = task_payload["output_manifest"]["artifact"]
            self.assertEqual(task_artifact["toolName"], "TaskOutput")
            self.assertEqual(task_artifact["family"], "bash")
            self.assertEqual(task_artifact["taskId"], task_id)
            self.assertEqual(Path(task_artifact["filePath"]).read_text(encoding="utf-8"), long_output)
            self.assertEqual(task_result["meta"]["execution"]["metadata"]["output_file"], output_path)
        finally:
            self.restore_home(root, previous)

    def test_websearch_decodes_redirects_and_bing_fallback(self):
        root, home, workspace, previous = self.temp_context()
        original_fetch = tools_module.fetch
        previous_exa = os.environ.pop("EXA_API_KEY", None)
        previous_provider = os.environ.pop("TESS_WEB_SEARCH_PROVIDER", None)
        try:
            async def ddg_fetch(*args, **kwargs):
                return Response(
                    """<!doctype html><html><body>
                    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdevelopers.openai.com%2Fapi%2Freference%2Fchat-completions%2Foverview&amp;rut=test">Chat</a>
                    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdevelopers.openai.com%2Fapi%2Freference%2Fchat-completions%2Foverview&amp;rut=test">Reference docs.</a>
                    </body></html>"""
                )

            tools_module.fetch = ddg_fetch
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home)}, str(workspace)))
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=config["permissionMode"])
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "WebSearch")
            result = run(tool.handler({"query": '\\"OpenAI\\" Chat Completions API', "allowed_domains": ["openai.com"]}, {"session": session, "config": config, "interactive": False, "abortSignal": None, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            payload = json.loads(result["content"])
            self.assertEqual(payload["provider"], "scraper")
            self.assertEqual(payload["query"], '"OpenAI" Chat Completions API')
            self.assertEqual(payload["raw_query"], '\\"OpenAI\\" Chat Completions API')
            self.assertEqual(payload["results"][0]["url"], "https://developers.openai.com/api/reference/chat-completions/overview")

            calls = {"count": 0}

            async def fallback_fetch(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    return Response('<div class="anomaly-modal__mask"></div>')
                return Response(
                    """<li class="b_algo">
                    <h2><a href="https://www.bing.com/ck/a?!&&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9yZXNlYXJjaA==">Research Result</a></h2>
                    <div class="b_caption"><p>Relevant fallback result.</p></div>
                    </li>"""
                )

            tools_module.fetch = fallback_fetch
            result = run(tool.handler({"query": "fallback test"}, {"session": session, "config": config, "interactive": False, "abortSignal": None, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)}))
            payload = json.loads(result["content"])
            self.assertEqual(payload["fallback_reason"], "duckduckgo_empty")
            self.assertEqual(payload["results"][0]["url"], "https://example.com/research")
            self.assertEqual(calls["count"], 2)
        finally:
            tools_module.fetch = original_fetch
            if previous_exa is not None:
                os.environ["EXA_API_KEY"] = previous_exa
            if previous_provider is not None:
                os.environ["TESS_WEB_SEARCH_PROVIDER"] = previous_provider
            self.restore_home(root, previous)

    def test_websearch_uses_exa_api_from_settings_env(self):
        root, home, workspace, previous = self.temp_context()
        original_urlopen = web_search_api_module.urlopen
        previous_provider = os.environ.pop("TESS_WEB_SEARCH_PROVIDER", None)
        previous_exa = os.environ.pop("EXA_API_KEY", None)
        try:
            (home / "settings.json").write_text(
                json.dumps({"env": {"EXA_API_KEY": "test-exa-key", "TESS_WEB_SEARCH_PROVIDER": "exa"}}),
                encoding="utf-8",
            )
            seen = {}

            class ExaResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return json.dumps(
                        {
                            "results": [
                                {
                                    "title": "OpenAI Agents SDK",
                                    "url": "https://openai.github.io/openai-agents-python/",
                                    "highlights": ["Build agents with tools and handoffs."],
                                }
                            ]
                        }
                    ).encode("utf-8")

            def exa_urlopen(request, timeout=0):
                seen["url"] = request.full_url
                seen["api_key"] = request.get_header("X-api-key")
                seen["timeout"] = timeout
                seen["body"] = json.loads(request.data.decode("utf-8"))
                return ExaResponse()

            web_search_api_module.urlopen = exa_urlopen
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home)}, str(workspace)))
            self.assertEqual(config["settingsEnv"]["EXA_API_KEY"], "test-exa-key")
            session = core.createEmptySession(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode=config["permissionMode"])
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "WebSearch")
            result = run(
                tool.handler(
                    {
                        "query": "OpenAI Agents SDK",
                        "count": 3,
                        "allowed_domains": ["openai.github.io", "*.example.org"],
                        "blocked_domains": ["blocked.example/path"],
                    },
                    {"session": session, "config": config, "interactive": False, "abortSignal": None, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace)},
                )
            )
            payload = json.loads(result["content"])
            self.assertEqual(payload["provider"], "exa")
            self.assertIsNone(payload["fallback_reason"])
            self.assertEqual(payload["results"][0]["url"], "https://openai.github.io/openai-agents-python/")
            self.assertEqual(payload["results"][0]["source"], "exa")
            self.assertEqual(seen["url"], "https://api.exa.ai/search")
            self.assertEqual(seen["api_key"], "test-exa-key")
            self.assertEqual(seen["body"]["numResults"], 3)
            self.assertEqual(seen["body"]["contents"]["text"]["maxCharacters"], 600)
            self.assertEqual(seen["body"]["includeDomains"], ["openai.github.io", "example.org"])
            self.assertEqual(seen["body"]["excludeDomains"], ["blocked.example"])
        finally:
            web_search_api_module.urlopen = original_urlopen
            if previous_exa is not None:
                os.environ["EXA_API_KEY"] = previous_exa
            if previous_provider is not None:
                os.environ["TESS_WEB_SEARCH_PROVIDER"] = previous_provider
            self.restore_home(root, previous)
