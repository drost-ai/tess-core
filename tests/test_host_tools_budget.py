from tests.support.tess_core_case import *
from tests.test_session_turns import Listener, _start, _reply
from tess_core import SessionOptions, ToolDefinition, ExecutionBudget
from tess_core.runtime import host as host_module
from tess_core.storage.message_origin import get_message_origin


class HostToolsBudgetTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def test_injection_budget_and_origin(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                budget = ExecutionBudget(max_provider_calls=4,max_tool_calls=4)
                seen, calls = [], []
                async def handler(args,context):
                    calls.append(args['n'])
                    return {'content':str(args['n'])}
                tool = ToolDefinition('HostEcho','Echo',{'type':'object','properties':{'n':{'type':'integer'}},'required':['n']},handler,concurrency='parallel')
                async def send(request):
                    seen.append(request)
                    if len(seen)==1:
                        return {'model':'gpt-5.4','stopReason':'tool_use','usage':{},'content':[{'type':'tool_use','id':f'c{i}','name':'HostEcho','input':{'n':i}} for i in range(2)]}
                    return _reply('done')
                runtime = await _start(home,workspace,Listener(),session_options=lambda sid,reason:SessionOptions(host_tools=(tool,),execution_budget=budget,provider_override={'send':send}))
                try:
                    session = await runtime.sessions.open()
                    self.assertIn('HostEcho',session.tool_names)
                    result = await session.run_turn('go',origin='worker',tools=['HostEcho'])
                    self.assertEqual(result['text'],'done')
                    self.assertEqual(sorted(calls),[0,1],str(seen[-1]))
                    self.assertEqual(budget.snapshot()['provider_calls'],2)
                    self.assertEqual(budget.snapshot()['tool_calls'],2)
                    self.assertIn('"tool_calls": 2',seen[-1]['system'])
                    record = session._live_session().record
                    self.assertEqual(get_message_origin(record['messages'][0])['kind'],'worker_request')
                finally:
                    await runtime.stop()
            run(scenario())
        finally:
            self.restore_home(root,previous)
