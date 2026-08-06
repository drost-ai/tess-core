from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ..._utils import maybe_await, now_iso
from ...process_identity import (
    ProcessIdentityError,
    processIdentityFromRecord,
    signalOwnedProcess,
    verifyOwnedProcessIdentity,
)
from ...persisted_record_scanner import getPersistedRecordIssue, listPersistedRecordIssues
from ...storage import (
    ensureBaseLayout,
    hasPersistableSessionMessages,
    listTaskRecords,
    listChildAgents,
    listHealthCheckTasks,
    listSessionMonitors,
    loadBridge,
    loadChildAgent,
    loadHealthCheckTask,
    loadRemoteSession,
    loadSession,
    loadTaskRecord,
    mutateChildAgent,
    mutateHealthCheckTask,
    mutateSessionRecord,
    mutateTaskMeta,
    readTaskMeta,
    saveHealthCheckTask,
    saveChildAgent,
    saveSession,
    saveTaskRecord,
    getStateRoot,
    taskMetaPath,
    taskOutputPath,
    writeTaskMeta,
)
from ...subagents.reconciliation import createSubagentReconciliationForTerminalChild
from ...task_communication import read_task_questions


_PROCESS_REGISTRY: dict[str, subprocess.Popen[Any]] = {}
_WATCHERS: set[threading.Thread | asyncio.Task[Any]] = set()
