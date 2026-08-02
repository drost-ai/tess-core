from __future__ import annotations

from tests.support.agent_core_case import *
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_core_harness.cache_economics import runCacheEconomicLane
from agent_core_harness.event_corpus import CORPUS_SCHEMA, DEFAULT_CORPUS_DIR, verifyEventCorpus
from agent_core_harness.fault_contract import PROVIDER_FAMILIES, buildFaultContract
from agent_core_harness.fault_matrix import runProviderFaultMatrix
from agent_core_harness.performance_lane import BUDGETS, _evaluate
from agent_core_harness.paths import REPO_ROOT
from agent_core_harness.suite_speed import (
    CRITICAL_TIMING_FILES,
    PROCESS_FILES,
    TIMING_FILES,
    _merge_outcomes,
    _parse_junit,
    _test_weight,
)


class Package47BFaultMatrixTests(AgentCoreTestCase):
    def test_every_declared_cell_has_an_explicit_verdict(self):
        for family in PROVIDER_FAMILIES:
            with self.subTest(family=family):
                cells = buildFaultContract(family)
                self.assertEqual(len(cells), 140)
                self.assertEqual(len({(cell["state"], cell["fault"]) for cell in cells}), 140)
                self.assertTrue(all(cell["expectedVerdict"]["classification"] for cell in cells))
                self.assertFalse(any(cell["disposition"] == "unknown" for cell in cells))

    def test_dual_provider_matrix_is_complete_and_adapter_probes_match(self):
        report = runProviderFaultMatrix()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["coverage"]["declaredCells"], 280)
        self.assertEqual(report["coverage"]["classifiedCells"], 280)
        self.assertEqual(report["coverage"]["evaluatedCells"], 280)
        self.assertEqual(report["coverage"]["matchedCells"], 280)
        self.assertEqual(
            report["coverage"]["executedCases"],
            report["coverage"]["executableCells"],
        )
        self.assertEqual(report["coverage"]["crashExecutedCells"], 20)
        self.assertEqual(
            report["coverage"]["classifiedImpossibleCells"],
            report["coverage"]["impossibleCells"],
        )
        self.assertEqual(report["coverage"]["unknownCells"], 0)
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual([family["providerFamily"] for family in report["families"]], list(PROVIDER_FAMILIES))
        self.assertTrue(
            all(
                probe["verdict"] == "matched"
                for family in report["families"]
                for probe in family["probes"]
            )
        )
        self.assertTrue(
            all(
                evaluation["verdict"] == "matched"
                for family in report["families"]
                for evaluation in family["evaluations"]
            )
        )
        self.assertTrue(
            all(
                evaluation["evidence"]["session"]["persisted"]
                for family in report["families"]
                for evaluation in family["evaluations"]
                if evaluation["executed"]
            )
        )
        self.assertTrue(
            all(
                evaluation["evidence"]["session"]["settledThrough"]
                == "runAgentPrompt"
                and evaluation["actual"]["eventSequence"]
                for family in report["families"]
                for evaluation in family["evaluations"]
                if evaluation["disposition"] == "adapter_probe"
            )
        )
        self.assertTrue(
            all(
                evaluation["evidence"]["session"]["idempotentReload"]
                and evaluation["evidence"]["session"]["revision"] >= 1
                and evaluation["evidence"]["session"]["archivedMessageCount"] == 1
                and evaluation["evidence"]["volatileState"]["state"]
                == evaluation["state"]
                and evaluation["evidence"]["volatileState"][
                    "volatileHadAssistantOutput"
                ]
                == evaluation["actual"]["hadAssistantOutput"]
                and evaluation["evidence"]["volatileState"]["volatileHadToolUse"]
                == evaluation["actual"]["hadToolUse"]
                for family in report["families"]
                for evaluation in family["evaluations"]
                if evaluation["disposition"] == "crash_lane"
            )
        )
        self.assertFalse(
            any(
                evaluation["validationSource"]
                in {"production_failure_classifier", "restart_recovery_contract"}
                for family in report["families"]
                for evaluation in family["evaluations"]
            )
        )
        self.assertEqual(report["families"][0]["connectionModeParity"]["status"], "passed")

    def test_matrix_is_deterministic_across_two_independent_runs(self):
        projections = []
        for _ in range(2):
            report = runProviderFaultMatrix()
            projections.append(
                [
                    (probe["id"], probe["actual"], probe["verdict"])
                    for family in report["families"]
                    for probe in family["probes"]
                ]
            )
        self.assertTrue(all(projection == projections[0] for projection in projections[1:]))


class Package47BEventCorpusTests(AgentCoreTestCase):
    def test_committed_event_corpus_regenerates_byte_identically(self):
        report = verifyEventCorpus()
        self.assertEqual(report["schemaVersion"], CORPUS_SCHEMA)
        self.assertEqual(report["status"], "passed", report["mismatches"])
        self.assertTrue(report["byteIdentical"])
        self.assertEqual(report["contractCount"], 6)

    def test_every_fixture_is_typed_and_turn_lifecycles_are_possible(self):
        fixtures = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in DEFAULT_CORPUS_DIR.glob("*.json")
            if path.name != "manifest.json"
        ]
        self.assertTrue(
            all(
                event["typedKind"] not in {None, "runtime.unknown"}
                for fixture in fixtures
                for event in fixture["events"]
            )
        )
        turn = next(item for item in fixtures if item["id"] == "turn-sessions")
        by_scenario = {}
        for event in turn["events"]:
            by_scenario.setdefault(event["scenario"], []).append(event["event"])
        self.assertEqual(
            by_scenario,
            {
                "cancelled-turn": ["turn.started", "turn.cancelling", "turn.cancelled"],
                "completed-turn": ["turn.started", "turn.completed"],
            },
        )
        manifest = json.loads((DEFAULT_CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["consumerAcknowledgements"], [])
        self.assertEqual(manifest["consumerStatus"], "unacknowledged")


class Package47BCacheEconomicTests(AgentCoreTestCase):
    def test_both_ttls_hold_recorded_cache_economics(self):
        report = runCacheEconomicLane()
        self.assertEqual(report["status"], "passed", report["regressions"])
        self.assertEqual([item["ttl"] for item in report["ttls"]], ["5m", "1h"])
        for ttl in report["ttls"]:
            self.assertTrue(all(item["usedBreakpoints"] <= 4 for item in ttl["observations"]))
            self.assertEqual(ttl["providerCalls"], 6)
            self.assertTrue(all(item["runtimeLifecycle"] for item in ttl["observations"]))
            self.assertTrue(
                all(
                    item["usage"]["cache_creation_ttl_detail_complete"] is True
                    for item in ttl["observations"]
                )
            )

    def test_seeded_wasted_slot_turns_lane_red(self):
        report = runCacheEconomicLane(seed_wasted_slot=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["regressions"]), 2)
        self.assertTrue(all("relief" in item["metric"] for item in report["regressions"]))


class Package47BPerformanceLaneTests(AgentCoreTestCase):
    def test_quick_lane_exercises_real_measurements_and_hard_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "performance.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_core_harness",
                    "performance-lane",
                    "--quick",
                    "--output",
                    str(output),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "passed", report["regressions"])
        self.assertTrue(all(gate["status"] == "passed" for gate in report["functionalGates"]))
        economics = report["persistenceEconomics"]
        self.assertEqual(economics["benchmark"], "representative_iteration_and_production_v2")
        self.assertEqual(economics["measurementPolicy"]["schemaStatus"], "production session schema v2")
        self.assertIn("v2Production", economics["schemaV2Economics"])
        self.assertIn("v2HydratedFallback", economics["schemaV2Economics"])
        self.assertLessEqual(
            economics["schemaV2Economics"]["comparison"][
                "fastPathVsHydratedCpuRatio"
            ],
            0.60,
        )
        self.assertTrue(all(economics["decisionGates"].values()))

    def test_two_x_injection_turns_every_metric_red(self):
        for metric, budget in BUDGETS.items():
            measurements = {name: 0.0 for name in BUDGETS}
            measurements[metric] = budget["maximum"] * 0.51
            self.assertEqual(_evaluate(measurements), [])
            doubled = {name: value * 2 for name, value in measurements.items()}
            regressions = _evaluate(doubled)
            self.assertEqual([item["metric"] for item in regressions], [metric])


class Package47BSuiteSpeedTests(AgentCoreTestCase):
    def test_explicit_process_and_timing_partitions_are_disjoint_and_exist(self):
        listed = PROCESS_FILES + TIMING_FILES + CRITICAL_TIMING_FILES
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(
            [name for name in listed if not (REPO_ROOT / "tests" / name).is_file()],
            [],
        )

    def test_zero_collection_aggregators_are_not_scheduled(self):
        self.assertEqual(_test_weight(REPO_ROOT / "tests" / "test_agent_core.py"), 0)

    def test_junit_identity_and_outcome_equivalence_detects_loss_and_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.xml"
            report.write_text(
                '<testsuite tests="2"><testcase classname="C" name="a" file="tests/a.py"/>'
                '<testcase classname="C" name="b" file="tests/a.py"><skipped/></testcase></testsuite>',
                encoding="utf-8",
            )
            parsed = _parse_junit(report)
        self.assertEqual(parsed["tests"], 2)
        self.assertEqual(list(parsed["_outcomes"].values()), ["passed", "skipped"])
        merged, duplicates = _merge_outcomes(
            [
                {"_outcomes": parsed["_outcomes"]},
                {"_outcomes": {next(iter(parsed["_outcomes"])): "failed"}},
            ]
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(duplicates), 1)
