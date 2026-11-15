"""The reconciliation log on disk: one JSON record per line, rewritten whole."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from ..storage.io_executor import run_storage_io as _run_storage_io


def _read_records_sync(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


async def _read_records(path: str) -> list[dict[str, Any]]:
    return await _run_storage_io(_read_records_sync, path)


def _write_records_sync(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
    os.replace(tmp, path)


async def _write_records(path: str, records: list[dict[str, Any]]) -> None:
    await _run_storage_io(_write_records_sync, path, records)
