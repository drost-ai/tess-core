"""Remote plane: remote-session records and the durable bridge transport.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .bridge_admission import admitBridgeMessage, sendBridgeMessage
from .bridge_diagnostics import formatBridgeTail, getBridgeDiagnostics, listActiveBridgeRows
from .bridge_generation import closeCurrentBridgeGeneration, isCurrentBridgeGeneration
from .bridge_recovery import (
    BridgeRecoveryBlockedError,
    activateBridgeGeneration,
    inspectBridgeRecovery,
    resolveBridgeRecovery,
)
from .bridge_runtime import (
    BridgeRuntimeController,
    LifecycleClosedError,
    createBridgeRuntime,
    createBridgeRuntimeController,
)
from .sessions import (
    createRemoteSession,
    findMatchingRemoteSessions,
    formatRemoteSessionCandidate,
    updateRemoteSession,
)

__all__ = [
    "BridgeRecoveryBlockedError",
    "BridgeRuntimeController",
    "LifecycleClosedError",
    "activateBridgeGeneration",
    "admitBridgeMessage",
    "closeCurrentBridgeGeneration",
    "createBridgeRuntime",
    "createBridgeRuntimeController",
    "createRemoteSession",
    "findMatchingRemoteSessions",
    "formatBridgeTail",
    "formatRemoteSessionCandidate",
    "getBridgeDiagnostics",
    "inspectBridgeRecovery",
    "isCurrentBridgeGeneration",
    "listActiveBridgeRows",
    "resolveBridgeRecovery",
    "sendBridgeMessage",
    "updateRemoteSession",
]
