"""Portable peer selection; no external TypeScript execution."""
import os
from pathlib import Path
from unittest.mock import patch

from tests.support.agent_core_case import AgentCoreTestCase
from agent_core_harness.ci_runner import detect_capabilities
from agent_core_harness.parity_runner import _run_typescript_driver


class PortablePeerTests(AgentCoreTestCase):
    def test_unconfigured_peer_is_unavailable_without_inspection(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            'agent_core_harness.parity_runner.inspect_typescript_peer'
        ) as inspect:
            self.assertFalse(detect_capabilities()['typescript_repo'])
            result = _run_typescript_driver([], {}, None)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error']['type'], 'ParityReferenceError')
            inspect.assert_not_called()

    def test_explicit_peer_precedes_environment(self):
        with patch.dict(os.environ, {'AGENT_CORE_TYPESCRIPT_REPO': '/opt/environment-peer'}), patch(
            'agent_core_harness.parity_runner.inspect_typescript_peer', return_value={}
        ) as inspect, patch(
            'agent_core_harness.parity_runner.provenance_error', return_value='synthetic rejection'
        ):
            result = _run_typescript_driver([], {}, '/opt/explicit-peer')
            self.assertEqual(result['status'], 'failed')
            inspect.assert_called_once_with('/opt/explicit-peer')

    def test_environment_peer_retains_provenance_gate(self):
        with patch.dict(os.environ, {'AGENT_CORE_TYPESCRIPT_REPO': '/opt/environment-peer'}), patch(
            'agent_core_harness.parity_runner.inspect_typescript_peer', return_value={}
        ) as inspect, patch(
            'agent_core_harness.parity_runner.provenance_error', return_value='synthetic rejection'
        ):
            self.assertEqual(_run_typescript_driver([], {}, None)['status'], 'failed')
            inspect.assert_called_once_with('/opt/environment-peer')

    def test_capability_uses_explicit_then_environment_peer(self):
        with patch.dict(os.environ, {'AGENT_CORE_TYPESCRIPT_REPO': '/opt/environment-peer'}), patch.object(
            Path, 'exists', return_value=True
        ) as exists:
            self.assertTrue(detect_capabilities('/opt/explicit-peer')['typescript_repo'])
            self.assertTrue(detect_capabilities()['typescript_repo'])
            self.assertEqual(exists.call_count, 4)
