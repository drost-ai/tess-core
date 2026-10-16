import asyncio
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from agent_core import ExecutionBudget, ExecutionBudgetExceeded
from agent_core.runtime.execution_budget import active_execution_budget, budget_provider_payload
from agent_core.runtime.session_turns import _narrowed_tools
from agent_core.storage.message_origin import turn_request_message, get_message_origin, is_genuine_user_request


class ExecutionBudgetTests(unittest.TestCase):
    def test_sticky_wrapup(self):
        with patch('agent_core.runtime.execution_budget.time.monotonic',return_value=100) as clock:
            b=ExecutionBudget(max_seconds=100)
            clock.return_value=181
            self.assertEqual(b.snapshot()['phase'],'wrapup')
            clock.return_value=110
            self.assertEqual(b.snapshot()['phase'],'wrapup')
            clock.return_value=201
            with self.assertRaises(ExecutionBudgetExceeded):b.consume_tool_call()

    def test_tightened_deadline_drives_context_without_resetting_usage(self):
        with patch('agent_core.runtime.execution_budget.time.monotonic', return_value=100) as clock:
            b = ExecutionBudget(max_seconds=1800)
            b.consume_provider_call()
            b.consume_tool_call()
            clock.return_value = 350
            b.tighten_deadline(400)
            state = b.snapshot()
            self.assertEqual(state['elapsed_seconds'], 250)
            self.assertEqual(state['remaining_seconds'], 50)
            self.assertEqual(state['provider_calls'], 1)
            self.assertEqual(state['tool_calls'], 1)
            self.assertEqual(state['phase'], 'wrapup')
            self.assertIn('Wrap up now', b.context())
            self.assertIn('"remaining_seconds": 50', b.context())
            clock.return_value = 400
            for consume in (b.consume_provider_call, b.consume_tool_call):
                with self.assertRaises(ExecutionBudgetExceeded):
                    consume()

    def test_deadline_cannot_extend_or_reset_sticky_phase(self):
        with patch('agent_core.runtime.execution_budget.time.monotonic', return_value=100) as clock:
            b = ExecutionBudget(max_seconds=100)
            b.tighten_deadline(300)
            self.assertEqual(b.snapshot()['remaining_seconds'], 100)
            b.tighten_deadline(150)
            clock.return_value = 140
            self.assertEqual(b.snapshot()['phase'], 'wrapup')
            b.tighten_deadline(190)
            b.tighten_deadline(150)
            self.assertEqual(b.snapshot()['remaining_seconds'], 10)
            self.assertEqual(b.snapshot()['phase'], 'wrapup')
            clock.return_value = 150
            self.assertEqual(b.snapshot()['phase'], 'exhausted')
            b.tighten_deadline(300)
            self.assertEqual(b.snapshot()['phase'], 'exhausted')
            self.assertEqual(b.snapshot()['remaining_seconds'], 0)

    def test_expired_and_invalid_deadlines(self):
        with patch('agent_core.runtime.execution_budget.time.monotonic', return_value=100):
            for deadline in (100, 99, -1):
                b = ExecutionBudget()
                b.tighten_deadline(deadline)
                self.assertEqual(b.snapshot()['remaining_seconds'], 0)
                with self.assertRaises(ExecutionBudgetExceeded):
                    b.consume_tool_call()
            b = ExecutionBudget()
            before = b.snapshot()
            for deadline in (float('nan'), float('inf'), -float('inf'), None, '400', True):
                with self.assertRaises(ValueError):
                    b.tighten_deadline(deadline)
                self.assertEqual(b.snapshot(), before)

    def test_provider_payload_not_mutated(self):
        b=ExecutionBudget(max_provider_calls=2); original={'system':'base','messages':[]}
        token=active_execution_budget.set(b)
        try:
            self.assertIn('"provider_calls": 1',budget_provider_payload(original,'canonical')['system'])
            self.assertIn('"provider_calls": 2',budget_provider_payload(original,'canonical')['system'])
            with self.assertRaises(ExecutionBudgetExceeded):budget_provider_payload(original,'canonical')
            self.assertEqual(original,{'system':'base','messages':[]})
        finally:active_execution_budget.reset(token)

    def test_parallel_individual_tools(self):
        b=ExecutionBudget(max_tool_calls=2)
        async def attempt():
            try:
                b.consume_tool_call();await asyncio.sleep(0);return True
            except ExecutionBudgetExceeded:return False
        async def run():return await asyncio.gather(*(attempt() for _ in range(5)))
        self.assertEqual(sum(asyncio.run(run())),2)

    def test_refresh_narrowing(self):
        a=SimpleNamespace(name='Allowed');f=SimpleNamespace(name='Forbidden')
        async def refresh():return (a,f)
        narrowed=_narrowed_tools(SimpleNamespace(bindings={'tools':(a,f),'refreshMcpToolRegistry':refresh}),['Allowed'])
        self.assertEqual(narrowed['tools'],[a])
        self.assertEqual(asyncio.run(narrowed['refreshMcpToolRegistry']()),[a])

    def test_synthetic_origins(self):
        for origin,actor in [('worker','host'),('scheduled','task')]:
            m=turn_request_message('go',origin=origin)
            self.assertEqual(get_message_origin(m)['actor'],actor)
            self.assertFalse(is_genuine_user_request(m))
        self.assertTrue(is_genuine_user_request(turn_request_message('go')))

    def test_transport_retries_each_consume_budget(self):
        from tests.test_openai_retry_policy import _OverloadedSdkError, _request
        from tests.support.agent_core_case import testing
        sent=[]
        async def create(params):
            sent.append(params);raise _OverloadedSdkError()
        async def scenario():
            b=ExecutionBudget(max_provider_calls=2);token=active_execution_budget.set(b)
            try:
                with self.assertRaises(ExecutionBudgetExceeded):
                    await testing.send_openai_chat_completions_request(token='test',baseUrl='https://api.openai.com/v1',request=_request(),runtime={},retryDelay=lambda attempt:0,clientFactory=lambda options:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
                self.assertEqual(b.snapshot()['provider_calls'],2)
            finally:active_execution_budget.reset(token)
        asyncio.run(scenario())
        self.assertEqual(len(sent),2)
