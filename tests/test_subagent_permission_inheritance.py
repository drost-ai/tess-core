"""Spawn-to-pinned-permission regressions using disposable session state."""
from tests.support.agent_core_case import *
from agent_core.subagents import prepare_subagent
from agent_core.tools.permissions import _ensure_tool_permission
from agent_core.authority.agent import is_no_more_permissive


class SubagentPermissionInheritanceTests(AgentCoreTestCase):
    def exercise(self, parent_mode, requested, expected):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                await core.save_settings('project', str(workspace), {'permissions': {'deny': ['Bash(git push:*)']}})
                config = await core.resolve_config({'provider': 'openai', 'runtimeHome': str(home), 'permissionMode': parent_mode}, str(workspace))
                parent = core.create_empty_session(cwd=str(workspace), provider=config['provider'], model=config['model'], systemPrompt=config['systemPrompt'], permissionMode=parent_mode)
                context = {'session': parent, 'config': config, 'interactive': False, 'cwd': str(workspace), 'persist_session': lambda: None, 'outputJson': lambda *_: None}
                prepared = await prepare_subagent({'context': context, 'description': 'permission regression', 'prompt': 'Disposable test only', **({'mode': requested} if requested is not None else {})})
                child = prepared['childSession']
                authority = child['metadata']['effectiveAgentAuthority']
                self.assertEqual(child['permissionMode'], expected)
                self.assertEqual(prepared['childConfig']['permissionMode'], expected)
                self.assertEqual(authority['mode'], expected)
                self.assertTrue(is_no_more_permissive(authority, parent['metadata']['effectiveAgentAuthority']))
                child_context = {**context, 'session': child, 'config': prepared['childConfig']}
                with self.assertRaisesRegex(PermissionError, 'Blocked by permission rule'):
                    await _ensure_tool_permission(child_context, 'Bash', 'shell', 'denied command', command='git push origin main')
                if expected == 'bypassPermissions':
                    bash = next(t for t in core.create_builtin_tools() if t.name == 'Bash')
                    await bash.handler({'command': 'printf marker > marker.txt', 'timeout': 1000}, child_context)
                    self.assertEqual((workspace / 'marker.txt').read_text(), 'marker')
                    await _ensure_tool_permission(child_context, 'Edit', 'edit', 'edit disposable file', path=str(workspace / 'marker.txt'))
                elif expected == 'acceptEdits':
                    await _ensure_tool_permission(child_context, 'Edit', 'edit', 'edit disposable file', path=str(workspace / 'marker.txt'))
                    with self.assertRaisesRegex(PermissionError, 'detached child'):
                        await _ensure_tool_permission(child_context, 'Bash', 'shell', 'write marker', command='printf marker > marker.txt')
                else:
                    with self.assertRaisesRegex(PermissionError, 'detached child'):
                        await _ensure_tool_permission(child_context, 'Bash', 'shell', 'write marker', command='printf marker > marker.txt')
            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_explicit_default_inherits_bypass_before_pinning(self):
        self.exercise('bypassPermissions', 'default', 'bypassPermissions')

    def test_omitted_mode_inherits_bypass(self):
        self.exercise('bypassPermissions', None, 'bypassPermissions')

    def test_default_inherits_accept_edits_but_not_shell_approval(self):
        self.exercise('acceptEdits', 'default', 'acceptEdits')

    def test_requested_bypass_cannot_escalate_default_parent(self):
        self.exercise('default', 'bypassPermissions', 'default')
