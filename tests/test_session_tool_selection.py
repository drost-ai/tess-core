import sys

from tests.support.tess_core_case import *
from tests.test_session_turns import Listener, _start, _reply
from tess_core import SessionOptions, ToolDefinition
from tess_core.authority.agent import capture_agent_authority
from tess_core.config import filter_tools_by_policy, get_default_tool_list
from tess_core.runtime import host as host_module
from tess_core.runtime.support import augment_config_tools
from tess_core_harness.mcp_fakes import _server_config


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

        # The answer is read before anything is added, and holds on a later call.
        config = {"tools": list(defaults), "allowedTools": list(defaults), "disallowedTools": []}
        kept = augment_config_tools(config, runtime_names)
        self.assertEqual(kept, (True, True))
        augment_config_tools(config, [*runtime_names, "mcp__crm__late"], kept)
        self.assertIn("mcp__crm__late", filter_tools_by_policy(config))

        config = {"tools": ["Read"], "allowedTools": ["Read"], "disallowedTools": []}
        kept = augment_config_tools(config, runtime_names)
        augment_config_tools(config, [*runtime_names, "mcp__crm__late"], kept)
        self.assertEqual(filter_tools_by_policy(config), ["Read"])

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


_GROWING_SERVER = '''
import json
import sys

TOOL = {"description": "tool", "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}}
tools = [{"name": "echo", **TOOL}]


def send(message):
    sys.stdout.buffer.write(json.dumps(message).encode() + b"\\n")
    sys.stdout.buffer.flush()


for line in sys.stdin.buffer:
    if not line.strip():
        continue
    request = json.loads(line.decode())
    if request.get("id") is None:
        continue
    method = request.get("method")
    result = {}
    if method == "initialize":
        result = {"serverInfo": {"name": "grow-fake"}, "capabilities": {"tools": {"listChanged": True}}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ok"}]}
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
    if method == "tools/call" and len(tools) == 1:
        # The first call grows the catalog and says so.
        tools.append({"name": "late", **TOOL})
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
'''


class LateMcpToolTests(TessCoreTestCase):
    """A tool an MCP server announces after the session started joins a
    session that kept the default tool lists, and no other."""

    def setUp(self):
        host_module._ACTIVE = None

    def _after_the_catalog_grows(self, **extra):
        """Open a session on a server that gains a tool mid-session and return
        (tools sent before, tools sent after, session.tool_names, composed names)."""
        root, home, workspace, previous = self.temp_context()
        try:
            script = Path(workspace) / "grow_mcp.py"
            script.write_text(_GROWING_SERVER, encoding="utf-8")
            server = _server_config({"name": "grow", "updatedAt": "2026-01-01T00:00:00Z"}, "stdio", sys.executable, [str(script)])

            async def scenario():
                seen = []

                async def send(request):
                    seen.append([item.get("name") if isinstance(item, dict) else item.name for item in request.get("tools") or []])
                    return _reply("done")

                runtime = await _start(
                    home, workspace, Listener(),
                    session_options=lambda sid, reason: SessionOptions(mcp_servers=(server,), provider_override={"send": send}, extra=extra),
                )
                try:
                    session = await runtime.sessions.open()
                    await session.run_turn("before")
                    before = seen[-1]
                    await session.mcp_call("grow", "echo", {"value": "x"})

                    def composed():
                        return [tool.name for tool in session._live_session().components["tools"]]

                    for _ in range(100):
                        # A turn applies a pending catalog change as it starts.
                        await session.run_turn("after")
                        if "mcp__grow__late" in composed():
                            break
                        await asyncio.sleep(0.05)
                    return before, seen[-1], session.tool_names, composed()
                finally:
                    await runtime.stop()

            return run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_default_session_is_offered_a_tool_that_arrives_later(self):
        before, after, names, composed = self._after_the_catalog_grows()
        self.assertIn("mcp__grow__echo", before)
        self.assertNotIn("mcp__grow__late", before)
        self.assertIn("mcp__grow__late", composed)
        self.assertIn("mcp__grow__late", after)
        self.assertIn("mcp__grow__late", names)

    def test_a_session_that_only_removed_tools_is_offered_a_tool_that_arrives_later(self):
        _before, after, names, _composed = self._after_the_catalog_grows(disallowedTools=["Bash"])
        self.assertIn("mcp__grow__late", after)
        self.assertNotIn("Bash", after)
        self.assertEqual(names, after)

    def test_a_session_that_chose_its_tools_is_not_offered_one_it_did_not_name(self):
        for extra in ({"tools": ["Read", "mcp__grow__echo"]}, {"allowedTools": ["Read", "mcp__grow__echo"]}):
            with self.subTest(extra=extra):
                before, after, names, composed = self._after_the_catalog_grows(**extra)
                self.assertEqual(sorted(before), ["Read", "mcp__grow__echo"])
                self.assertIn("mcp__grow__late", composed)
                self.assertEqual(sorted(after), ["Read", "mcp__grow__echo"])
                self.assertEqual(names, after)
