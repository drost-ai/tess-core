from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.cache_economics import runCacheEconomicLane
from agent_core_harness.event_corpus import CORPUS_SCHEMA, verifyEventCorpus
from agent_core_harness.fault_contract import PROVIDER_FAMILIES, buildFaultContract
from agent_core_harness.fault_matrix import runProviderFaultMatrix
from agent_core_harness.performance_lane import BUDGETS, _evaluate, runPerformanceLane
from agent_core_harness.paths import REPO_ROOT
from agent_core_harness.suite_speed import (
    CRITICAL_TIMING_FILES,
    PROCESS_FILES,
    TIMING_FILES,
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
        self.assertEqual(report["families"][0]["connectionModeParity"]["status"], "passed")

    def test_matrix_is_deterministic_across_five_runs(self):
        projections = []
        for _ in range(5):
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


class Package47BCacheEconomicTests(AgentCoreTestCase):
    def test_both_ttls_hold_recorded_cache_economics(self):
        report = runCacheEconomicLane()
        self.assertEqual(report["status"], "passed", report["regressions"])
        self.assertEqual([item["ttl"] for item in report["ttls"]], ["5m", "1h"])
        for ttl in report["ttls"]:
            self.assertTrue(all(item["usedBreakpoints"] <= 4 for item in ttl["observations"]))
            self.assertGreater(ttl["observations"][-1]["cacheReadRatio"], 0.6)

    def test_seeded_wasted_slot_turns_lane_red(self):
        report = runCacheEconomicLane(seed_wasted_slot=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["regressions"]), 2)
        self.assertTrue(all("relief" in item["metric"] for item in report["regressions"]))


class Package47BPerformanceLaneTests(AgentCoreTestCase):
    def test_quick_lane_exercises_real_measurements_and_hard_gates(self):
        report = runPerformanceLane(quick=True)
        self.assertEqual(report["status"], "passed", report["regressions"])
        self.assertTrue(all(gate["status"] == "passed" for gate in report["functionalGates"]))

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
