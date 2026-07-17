
from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'paths', 'json_io', 'settings', 'sessions', 'session_archive', 'session_transactions', 'records', 'remote', 'daemon', 'commands']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

_PUBLIC_EXPORTS = ['Any', 'DEFAULT_AUTO_MODE', 'DEFAULT_RUNTIME_HOME', 'Literal', 'PLAN_ADJECTIVES', 'PLAN_NOUNS', 'Path', 'Scope', 'annotations', 'appendBridgeEvent', 'appendBridgeInboxMessage', 'appendDaemonEvent', 'appendJsonLine', 'appendSessionTranscriptLogEntries', 'append_json_line', 'asyncio', 'bridgeEventsPath', 'bridgeInboxPath', 'bridgePath', 'childAgentPath', 'clearAuth', 'clearDaemonState', 'clone', 'compactionCheckpointPath', 'compactionManifestPath', 'configureRuntimeHome', 'configure_runtime_home', 'createEmptySession', 'daemonEventsPath', 'daemonLogsDir', 'daemonPidPath', 'daemonSocketPath', 'daemonStatePath', 'ensureBaseLayout', 'ensure_base_layout', 'expandHome', 'expand_home', 'fileOrJson', 'findGitRoot', 'find_git_root', 'getCompactionsDir', 'getDaemonDir', 'getDefaultAutoMode', 'getLocalStateDir', 'getPlansDir', 'getProjectMemoryPath', 'getProjectMemoryRecordsPath', 'getProjectRoot', 'getProjectStateDir', 'getSessionCompactionsDir', 'getSessionToolArtifactsDir', 'getStateRoot', 'getToolArtifactsDir', 'get_local_state_dir', 'get_project_root', 'get_project_state_dir', 'get_state_root', 'hasPersistableSessionMessages', 'healthCheckMetaPath', 'json', 'listBridges', 'listChildAgents', 'listHealthCheckTasks', 'listMonitors', 'listPlanApprovals', 'listRemoteSessions', 'listSessionMonitors', 'listSessionTaskRecords', 'listSessions', 'listTaskRecords', 'loadAuth', 'loadAutoMode', 'loadBridge', 'loadBridgeEvents', 'loadBridgeInbox', 'loadChildAgent', 'loadCodexAuthToken', 'loadCompactionManifest', 'loadDaemonEvents', 'loadDaemonState', 'loadHealthCheckTask', 'loadMarketplaces', 'loadMcpServers', 'loadMonitor', 'loadPlanApproval', 'loadPlugins', 'loadProjectMemoryRecords', 'loadRemoteSession', 'loadSession', 'loadSettings', 'loadSettingsBySources', 'loadTaskRecord', 'load_auth', 'load_codex_auth_token', 'monitorPath', 'now_iso', 'os', 'parseJsonString', 'planApprovalPath', 'random', 're', 'readJsonFile', 'readTaskMeta', 'read_json_file', 'remoteSessionPath', 'resetConfiguredRuntimeHome', 'reset_configured_runtime_home', 'resolveMaybeRelative', 'resolveRuntimeHome', 'resolveScope', 'resolve_maybe_relative', 'resolve_runtime_home', 'runCommand', 'saveAuth', 'saveAutoMode', 'saveBridge', 'saveChildAgent', 'saveCompactionCheckpoint', 'saveCompactionManifest', 'saveDaemonState', 'saveHealthCheckTask', 'saveMarketplaces', 'saveMcpServers', 'saveMonitor', 'savePlanApproval', 'savePlugins', 'saveProjectMemoryRecords', 'saveRemoteSession', 'saveSession', 'saveSettings', 'saveTaskRecord', 'sessionArchiveDir', 'sessionPath', 'sessionTranscriptLogPath', 'shutil', 'subprocess', 'taskDir', 'taskMetaPath', 'taskOutputPath', 'taskRecordPath', 'toolArtifactDataPath', 'toolArtifactMetaPath', 'uuid', 'writeJsonFile', 'writePlanFile', 'writeTaskMeta', 'write_json_file']
_PUBLIC_EXPORTS += ['RuntimeHomeScope', 'getContextRuntimeHome', 'get_context_runtime_home', 'scopedRuntimeHome', 'scoped_runtime_home']
_PUBLIC_EXPORTS += ['RuntimePaths', 'getActiveRuntimePaths', 'resolveRuntimePaths']
_PUBLIC_EXPORTS += ['STORAGE_LOCK_TIMEOUT_MS', 'StorageTransactionError', 'fileLock', 'file_lock', 'mutateJsonFile', 'mutate_json_file', 'mutateJsonRecord', 'mutate_json_record', 'registerStorageLockObserver', 'transactionLockPath', 'transaction_lock_path', 'withRecordTransaction', 'with_record_transaction']
_PUBLIC_EXPORTS += ['mutateChildAgent', 'mutateHealthCheckTask', 'mutateSessionRecord', 'mutateTaskMeta']
_PUBLIC_EXPORTS += ['SessionRecordNotFoundError', 'mutateSessionRecordTransaction', 'mutate_session_record_transaction', 'normalizeSessionRevision', 'normalize_session_revision', 'updateSessionRecord', 'update_session_record']
_PUBLIC_EXPORTS += ['mutateAutoMode', 'mutateMarketplaces', 'mutateMcpServers', 'mutatePlugins', 'mutateProjectMemoryRecords', 'mutateSettings']
_PUBLIC_EXPORTS += ['MAX_SHALLOW_SESSION_BYTES', 'createSessionListCache', 'loadSessionRecordShallow', 'loadSessionWithReconciliation', 'parseLatestArchivedTimestamp']
_PUBLIC_EXPORTS += ['DAEMON_EVENTS_ROTATE_BYTES', 'DAEMON_EVENTS_TAIL_BYTES', 'MAX_DAEMON_SOCKET_PATH_BYTES', 'readLastDaemonEventCursor', 'rotateDaemonEventsIfOversized']
_PUBLIC_EXPORTS = sorted(set(_PUBLIC_EXPORTS))
__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _StorageStoreModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _StorageStoreModule
