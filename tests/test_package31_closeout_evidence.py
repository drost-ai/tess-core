from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


PACKAGE_DIR = (
    Path(__file__).parents[1]
    / "product-planning"
    / "31-adversarial-runtime-hardening-and-correctness-closure"
)
EXPECTED_ROWS = {
    *(f"F{index:02d}" for index in range(1, 23)),
    "F24",
    *(f"F{index:02d}" for index in range(28, 33)),
}
EXPECTED_SHARED = {"F02", "F10", "F22", "F24", "F28"}


def test_package31_machine_closeout_matches_the_planning_ledger():
    closeout_path = PACKAGE_DIR / "evidence" / "package31-closeout.json"
    raw = closeout_path.read_bytes()
    assert len(raw) <= 128 * 1024
    closeout = json.loads(raw)

    assert closeout["schemaVersion"] == 1
    assert closeout["package"] == 31
    assert closeout["status"] == "closed"
    assert set(closeout["sharedRows"]) == EXPECTED_SHARED
    assert len(closeout["sharedRows"]) == len(EXPECTED_SHARED)

    rows = closeout["rows"]
    assert len(rows) == 28
    assert {row["id"] for row in rows} == EXPECTED_ROWS
    assert all(row["state"] == "closed" for row in rows)
    assert all(row["commits"] and row["tests"] for row in rows)
    for row in rows:
        for test_path in row["tests"]:
            if not test_path.startswith("host:"):
                assert (Path(__file__).parents[1] / test_path).is_file()

    progress = (PACKAGE_DIR / "12-implementation-progress.md").read_text(
        encoding="utf-8"
    )
    progress_rows = {
        match.group("id"): match.group("state")
        for match in re.finditer(
            r"^\| `(?P<id>F\d{2})` \| (?P<state>[a-z_]+) \|",
            progress,
            re.MULTILINE,
        )
    }
    assert set(progress_rows) == EXPECTED_ROWS
    assert set(progress_rows.values()) == {"closed"}

    for probe in closeout["probes"].values():
        artifact = PACKAGE_DIR / probe["path"]
        assert probe["status"] == "passed"
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == probe["sha256"]

    assert closeout["validation"]["core"]["scenarioPacksFailed"] == 0
    assert closeout["validation"]["consumer"]["fullPytestPassed"] == 952
    assert closeout["migration"]["durableSchemaMigrationRequired"] is False
    assert closeout["fixedSparkDivergence"] == {
        "preserved": True,
        "provider": "openai",
        "model": "gpt-5.3-codex-spark",
        "effort": "high",
        "selectionSource": "fixed",
    }
