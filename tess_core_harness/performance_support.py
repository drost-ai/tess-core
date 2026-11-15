from __future__ import annotations

import os
import platform
import statistics
from collections.abc import Iterable
from typing import Any


def validate_sample(sample: dict[str, Any], expected_calls: int) -> None:
    if sample["persistCallCount"] != expected_calls:
        raise RuntimeError(
            f"Persistence observer saw {sample['persistCallCount']} calls; expected {expected_calls}"
        )
    if sample["persistFinishedCount"] != expected_calls:
        raise RuntimeError("A measured persistence call did not finish")
    if sample["persistOutcomes"] != {"persisted": expected_calls}:
        raise RuntimeError(f"Unexpected persistence outcomes: {sample['persistOutcomes']}")
    if sample["storageWriteCount"] != expected_calls or sample["storageBytes"] <= 0:
        raise RuntimeError("Measured persistence calls did not produce the expected physical writes")


def median_sample(samples: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "persistCallCount",
        "persistFinishedCount",
        "storageWriteCount",
        "storageAppendCount",
        "storageBytes",
        "storageElapsedNs",
        "persistenceElapsedNs",
        "wallElapsedNs",
    )
    return {
        key: int(statistics.median([int(sample[key]) for sample in samples]))
        for key in keys
    }


def validated_counts(values: Iterable[int]) -> list[int]:
    counts = [int(value) for value in values]
    if not counts or any(value < 1 for value in counts):
        raise ValueError("message_counts must contain positive integers")
    if len(set(counts)) != len(counts):
        raise ValueError("message_counts must not contain duplicates")
    return counts


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpuCount": os.cpu_count(),
    }
