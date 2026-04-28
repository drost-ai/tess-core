from __future__ import annotations

import os
from typing import Any

from .._utils import now_iso
from ..compaction import runCompaction
from ..config import (
    ensureProviderForModelSelection,
    getDefaultEffortForProvider,
    getDefaultModelForProvider,
    listModelsForProvider,
    listProviderProfiles,
    listProviders,
    modelSupportsEffort,
    normalizeEffortForModel,
    normalizeEffortLevel,
    normalizeModelName,
    normalizeProviderName,
    resolveProviderForModelSelection,
    normalizeProviderProfileName,
    resolveFallbackProviderProfileName,
)
from ..debug_bundle import createDebugBundle
from ..durable_memory import synchronizeManualProjectMemory
from ..daemon import connectTaskSupervisorDaemonClient, ensureTaskSupervisorDaemon, stopTaskSupervisorDaemon
from ..platform import collectDoctorReport
from ..remote import formatBridgeTail, formatRemoteSessionCandidate, sendBridgeMessage, updateRemoteSession
from ..storage import (
    compactionCheckpointPath,
    compactionManifestPath,
    getProjectMemoryPath,
    getDefaultAutoMode,
    listBridges,
    listChildAgents,
    listPlanApprovals,
    listRemoteSessions,
    listSessionMonitors,
    listSessionTaskRecords,
    listSessions,
    loadAutoMode,
    loadBridge,
    loadCompactionManifest,
    loadDaemonState,
    loadRemoteSession,
    loadSession,
    loadSettings,
    loadTaskRecord,
    saveBridge,
    saveAutoMode,
    saveSettings,
    saveMonitor,
)
from ..transcript import buildTranscriptSnapshot

AVAILABLE_TUI_THEMES = ["tess", "copper", "ocean", "forest"]
PERMISSION_MODE_ORDER = ["acceptEdits", "bypassPermissions", "default", "dontAsk", "plan", "auto"]
PERMISSION_MODES = set(PERMISSION_MODE_ORDER)
