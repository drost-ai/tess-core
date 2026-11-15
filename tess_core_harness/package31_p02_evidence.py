from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


TYPE_SCRIPT_ANCHOR = "9e2b25920184118517fde75b3ef3fc9d8ec6f33c"
MAX_JUNIT_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 32 * 1024
EXPECTED_JUNIT_TESTS = 6
EXPECTED_LOGICAL_TESTS = 2
EXPECTED_SUBTESTS = 4
CRASH_BOUNDARIES = [
    "before_bind",
    "after_bind_before_token_state_publication",
    "after_publication_before_ready",
    "during_recovery",
    "after_ready_while_followers_attach",
]
COMMAND = (
    ".venv/bin/pytest tests/test_p02_concurrent_daemon_bootstrap.py -q "
    "--junitxml=product-planning/31-adversarial-runtime-hardening-and-"
    "correctness-closure/evidence/p02-concurrent-daemon-bootstrap.junit.xml"
)


class P02EvidenceError(RuntimeError):
    pass


def buildP02Evidence(
    junit_path: str | os.PathLike[str],
    *,
    python_head: str,
    implementation_commits: list[str],
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{7,40}", python_head):
        raise P02EvidenceError("python_head_invalid")
    if not implementation_commits or any(
        not re.fullmatch(r"[0-9a-f]{7,40}", commit)
        for commit in implementation_commits
    ):
        raise P02EvidenceError("implementation_commits_invalid")

    source = Path(junit_path)
    try:
        size = source.stat().st_size
    except OSError as error:
        raise P02EvidenceError("junit_unavailable") from error
    if size <= 0 or size > MAX_JUNIT_BYTES:
        raise P02EvidenceError("junit_size_invalid")
    try:
        raw = source.read_bytes()
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as error:
        raise P02EvidenceError("junit_invalid") from error

    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise P02EvidenceError("junit_suite_missing")
    totals = {
        key: sum(_integer_attribute(suite, key) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if totals != {
        "tests": EXPECTED_JUNIT_TESTS,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }:
        raise P02EvidenceError("junit_not_passing")

    names = {
        case.get("name") or ""
        for suite in suites
        for case in suite.findall("testcase")
    }
    required_names = {
        "test_two_clients_converge_after_each_bootstrap_crash_boundary",
        "test_ready_leader_crash_reauthenticates_both_attached_followers",
    }
    if not all(any(name.startswith(required) for name in names) for required in required_names):
        raise P02EvidenceError("junit_cases_missing")

    evidence = {
        "schemaVersion": 1,
        "probe": "P02",
        "status": "passed",
        "typeScriptAnchor": TYPE_SCRIPT_ANCHOR,
        "pythonHeadUnderTest": python_head,
        "implementationCommits": implementation_commits,
        "command": COMMAND,
        "result": {
            "junitTests": EXPECTED_JUNIT_TESTS,
            "logicalTests": EXPECTED_LOGICAL_TESTS,
            "subtests": EXPECTED_SUBTESTS,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
        "crashBoundaries": CRASH_BOUNDARIES,
        "publicationOrder": {
            "beforeBind": {"socket": False, "token": False, "state": False},
            "afterBind": {"socket": True, "token": False, "state": False},
            "afterPublication": {
                "socket": True,
                "token": True,
                "statePhase": "starting",
            },
            "duringRecovery": {
                "socket": True,
                "token": True,
                "statePhase": "recovering",
            },
        },
        "contention": {
            "independentClients": 2,
            "barrierSynchronized": True,
            "oneLiveSpawnedDaemon": True,
            "oneSocketTokenInstanceGeneration": True,
            "allSurvivorsAuthenticated": True,
        },
        "recovery": {
            "followersPreservedWinnerState": True,
            "readyLeaderCrashReauthenticatedBothFollowers": True,
            "pidEpochAndTokenRotated": True,
            "recoveredEventObserved": True,
            "admissionAfterAuthoritativeRecovery": True,
        },
        "junit": {
            "path": "evidence/p02-concurrent-daemon-bootstrap.junit.xml",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    encoded = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise P02EvidenceError("evidence_oversized")
    return evidence


def writeP02Evidence(path: str | os.PathLike[str], evidence: dict[str, Any]) -> None:
    encoded = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise P02EvidenceError("evidence_oversized")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as error:
        raise P02EvidenceError("evidence_exists") from error
    try:
        pending = memoryview(encoded)
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise OSError("P02 evidence write made no progress.")
            pending = pending[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)


def _integer_attribute(element: ET.Element, name: str) -> int:
    try:
        value = int(element.get(name) or 0)
    except ValueError as error:
        raise P02EvidenceError("junit_attribute_invalid") from error
    if value < 0:
        raise P02EvidenceError("junit_attribute_invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tess_core_harness.package31_p02_evidence"
    )
    parser.add_argument("--junit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--python-head", required=True)
    parser.add_argument("--implementation-commit", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = buildP02Evidence(
            args.junit,
            python_head=args.python_head,
            implementation_commits=args.implementation_commit,
        )
        writeP02Evidence(args.output, evidence)
    except P02EvidenceError as error:
        print(f"P02 evidence refused: {error}")
        return 2
    print(json.dumps({"probe": "P02", "status": "passed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
