from __future__ import annotations

import json
import re
from pathlib import Path


EVIDENCE_PATH = (
    Path(__file__).parents[1]
    / "product-planning"
    / "32-anthropic-prompt-cache-parity"
    / "evidence"
    / "official-canary-2026-07-17.json"
)


def test_package32_official_canary_evidence_closes_paid_rows():
    raw = EVIDENCE_PATH.read_bytes()
    evidence = json.loads(raw)
    assert len(raw) <= 128 * 1024
    assert evidence["schemaVersion"] == 1
    assert evidence["package"] == 32
    assert evidence["status"] == "passed"
    assert evidence["routeClass"] == "official"
    assert re.fullmatch(r"[a-f0-9]{64}", evidence["modelHash"])
    assert evidence["requestCounts"] == {"json5m": 5, "stream1h": 3}
    assert evidence["exactInputTokens"] > 1_024
    assert 0 < evidence["actualCostUsd"] <= evidence["maximumCostUsd"]
    assert evidence["estimatedMaximumCostUsd"] <= evidence["maximumCostUsd"]
    assert all(evidence["assertions"].values())

    five_minute, one_hour = evidence["scenarios"]
    assert five_minute["stateRevisions"] == [1, 2, 3, 4, 5]
    assert five_minute["cacheCreationInputTokens"][0] > 0
    assert any(value > 0 for value in five_minute["cacheReadInputTokens"][1:])
    assert five_minute["providerReasons"] == [
        "warmup_no_comparison",
        "no_divergence",
        "no_divergence",
        "no_divergence",
        "system_changed",
    ]
    assert five_minute["cacheCreationInputTokens"] == five_minute[
        "cacheCreation5mInputTokens"
    ]

    assert one_hour["stateRevisions"] == [1, 2, 3]
    assert one_hour["cacheCreationInputTokens"] == one_hour[
        "cacheCreation1hInputTokens"
    ]
    assert one_hour["cacheCreationInputTokens"][0] > 0
    assert any(value > 0 for value in one_hour["cacheReadInputTokens"][1:])
    assert one_hour["tailBOmittedReasons"] == [None, "budget", "budget"]

    for scenario in evidence["scenarios"]:
        assert all(scenario["ttlDetailComplete"])
        assert all(value <= 4 for value in scenario["usedBreakpoints"])
        assert all(len(placements) <= 4 for placements in scenario["placements"])

    retained = raw.decode("utf-8").lower()
    for forbidden in (
        "private_canary",
        "cache-prefix",
        "response-id",
        "authorization",
        "api-key",
        "auth-token",
        "claude-opus-4-8",
    ):
        assert forbidden not in retained
