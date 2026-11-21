from tests.support.tess_core_case import *
from tests.test_session_turns import Listener, _start, _reply
from tess_core import SessionOptions, ToolDefinition
from tess_core.authority.agent import capture_agent_authority
from tess_core.config import filter_tools_by_policy, get_default_tool_list
from tess_core.runtime import host as host_module
from tess_core.runtime.support import augment_config_tools


class SessionToolSelectionTests(TessCoreTestCase):
    """A session's tool lists decide what the model is offered, and
    ``session.tool_names`` reports exactly that."""

    def setUp(self):
        host_module._ACTIVE = None

    def _offered(self, **extra):
        """Open a session with one host tool and these config options, run a
        turn, and return (session.tool_names, the tools the provider was sent)."""
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                seen = []

                async def handler(args, context):
                    return {"content": "ok"}

                tool = ToolDefinition("Workspace", "Host tool", {"type": "object", "properties": {}}, handler)

                async def send(request):
                    seen.append(request)
                    return _reply("done")

                runtime = await _start(
                    home, workspace, Listener(),
                    session_options=lambda sid, reason: SessionOptions(host_tools=(tool,), provider_override={"send": send}, extra=extra),
                )
                try:
                    session = await runtime.sessions.open()
                    before = session.tool_names
                    await session.run_turn("go")
                    sent = [item.get("name") if isinstance(item, dict) else item.name for item in seen[-1].get("tools") or []]
                    self.assertEqual(before, session.tool_names)
                    return session.tool_names, sent
                finally:
                    await runtime.stop()

            return run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_default_session_is_offered_everything_including_host_tools(self):
        names, sent = self._offered()
        self.assertEqual(names, sent)
        for name in ("Read", "Bash", "Agent", "Workspace"):
            self.assertIn(name, sent)

    def test_tools_names_the_only_tools_the_session_is_offered(self):
        names, sent = self._offered(tools=["Workspace"])
        self.assertEqual(sent, ["Workspace"])
        self.assertEqual(names, ["Workspace"])

    def test_allowed_tools_narrows_and_tool_names_says_so(self):
        names, sent = self._offered(allowedTools=["Workspace", "Read"])
        self.assertEqual(sorted(sent), ["Read", "Workspace"])
        self.assertEqual(names, sent)

    def test_disallowed_tools_are_removed_and_tool_names_says_so(self):
        names, sent = self._offered(disallowedTools=["Bash", "Write"])
        self.assertNotIn("Bash", sent)
        self.assertNotIn("Write", sent)
        self.assertIn("Read", sent)
        self.assertIn("Workspace", sent)
        self.assertEqual(names, sent)

    def test_an_empty_tool_list_is_no_tools(self):
        for extra in ({"tools": []}, {"tools": [""]}, {"allowedTools": []}, {"allowedTools": [""]}):
            with self.subTest(extra=extra):
                names, sent = self._offered(**extra)
                self.assertEqual(sent, [])
                self.assertEqual(names, [])

    def test_allowed_tools_narrows_a_chosen_tool_list(self):
        names, sent = self._offered(tools=["Read", "Grep", "Workspace"], allowedTools=["Read", "Bash", "Workspace"])
        self.assertEqual(sorted(sent), ["Read", "Workspace"])
        self.assertEqual(names, sent)

    def test_runtime_tools_join_default_lists_and_leave_chosen_lists_alone(self):
        defaults = get_default_tool_list()
        runtime_names = [*defaults, "mcp__crm__lookup", "Workspace"]

        config = {"tools": list(defaults), "allowedTools": list(defaults), "disallowedTools": []}
        augment_config_tools(config, runtime_names)
        self.assertEqual(filter_tools_by_policy(config), runtime_names)

        config = {"tools": [], "allowedTools": [], "disallowedTools": []}
        augment_config_tools(config, runtime_names)
        self.assertEqual(filter_tools_by_policy(config), [])

        config = {"tools": ["Read"], "allowedTools": ["Read"], "disallowedTools": []}
        augment_config_tools(config, runtime_names)
        self.assertEqual(filter_tools_by_policy(config), ["Read"])

        config = {"tools": list(defaults), "allowedTools": ["Workspace"], "disallowedTools": []}
        augment_config_tools(config, runtime_names)
        self.assertEqual(filter_tools_by_policy(config), ["Workspace"])

        # A config assembled by hand without a tool list is unrestricted.
        config = {}
        augment_config_tools(config, runtime_names)
        self.assertEqual(filter_tools_by_policy(config), runtime_names)

    def test_a_session_with_no_tools_has_no_tools_to_delegate(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario(tools):
                config = await core.resolve_config({"runtimeHome": str(home), "settingSources": "", **tools}, str(workspace))
                session = await core.resolve_session({}, config, {"cwd": str(workspace), "originalCwd": str(workspace)})
                authority = await capture_agent_authority(config=config, session=session, tools=core.create_builtin_tools())
                return authority["enabledTools"]

            self.assertEqual(run(scenario({"tools": []})), [])
            self.assertEqual(run(scenario({"tools": ["Read", "Grep"]})), ["Grep", "Read"])
        finally:
            self.restore_home(root, previous)
