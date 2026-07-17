from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase

from agent_core_harness.package31_p02_evidence import (
    P02EvidenceError,
    buildP02Evidence,
    writeP02Evidence,
)


class Package31P02EvidenceTests(AgentCoreTestCase):
    def test_passing_exact_junit_builds_bounded_hashed_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            junit = Path(root, "p02.xml")
            raw = self._junit()
            junit.write_bytes(raw)
            evidence = buildP02Evidence(
                junit,
                python_head="a" * 40,
                implementation_commits=["b" * 7],
            )
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(evidence["result"]["junitTests"], 6)
            self.assertEqual(evidence["result"]["subtests"], 4)
            self.assertEqual(evidence["junit"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(len(evidence["crashBoundaries"]), 5)

    def test_failure_or_wrong_case_set_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            junit = Path(root, "p02.xml")
            junit.write_bytes(self._junit(failures=1))
            with self.assertRaises(P02EvidenceError) as failed:
                buildP02Evidence(
                    junit,
                    python_head="a" * 7,
                    implementation_commits=["b" * 7],
                )
            self.assertEqual(str(failed.exception), "junit_not_passing")

            junit.write_bytes(self._junit(first_name="wrong_case"))
            with self.assertRaises(P02EvidenceError) as missing:
                buildP02Evidence(
                    junit,
                    python_head="a" * 7,
                    implementation_commits=["b" * 7],
                )
            self.assertEqual(str(missing.exception), "junit_cases_missing")

    def test_output_is_exclusive_and_failed_write_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "evidence", "p02.json")
            evidence = {"probe": "P02", "status": "passed"}
            writeP02Evidence(target, evidence)
            original = target.read_bytes()
            with self.assertRaises(P02EvidenceError) as raised:
                writeP02Evidence(target, {"different": True})
            self.assertEqual(str(raised.exception), "evidence_exists")
            self.assertEqual(target.read_bytes(), original)

    @staticmethod
    def _junit(*, failures: int = 0, first_name: str | None = None) -> bytes:
        first = first_name or "test_two_clients_converge_after_each_bootstrap_crash_boundary"
        cases = [
            first,
            f"{first}[pre_bind]",
            f"{first}[post_bind]",
            f"{first}[post_publish]",
            f"{first}[during_recovery]",
            "test_ready_leader_crash_reauthenticates_both_attached_followers",
        ]
        rendered = "".join(
            f'<testcase classname="tests.test_p02.P02" name="{name}" />'
            for name in cases
        )
        return (
            '<testsuites name="pytest tests">'
            f'<testsuite tests="6" failures="{failures}" errors="0" skipped="0">'
            f"{rendered}</testsuite></testsuites>"
        ).encode("utf-8")


if __name__ == "__main__":
    import unittest

    unittest.main()
