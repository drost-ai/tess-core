from __future__ import annotations

from tests.support.agent_core_case import *


class PublicApiTests(AgentCoreTestCase):
    def test_public_entrypoints(self):
        self.assertTrue(callable(core.resolveConfig))
        self.assertTrue(callable(core.runAgentPrompt))
        self.assertTrue(callable(core.bootstrapRuntime))
        self.assertTrue(callable(core.createEmptySession))
        self.assertTrue(callable(core.ensureTaskSupervisorDaemon))
