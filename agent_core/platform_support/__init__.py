
from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'common', 'commands', 'integrations', 'tmux', 'ide', 'chrome', 'install', 'session_hygiene', 'doctor', 'health_checks', 'bridge_recovery']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

_PUBLIC_EXPORTS = ['Any', 'BUNDLED_PACKAGE_JSON_PATH', 'DEFAULT_CHROME_EXTENSION_ORIGIN', 'DEFAULT_CHROME_NATIVE_HOST_NAME', 'DIST_ENTRY_PATH', 'Path', 'REPO_PACKAGE_JSON_PATH', 'REPO_ROOT', 'annotations', 'applyPlatformIntegrationMetadata', 'asyncio', 'authorizeHealthCheckRevalidation', 'buildChromeNativeHostManifest', 'buildSessionHygieneReport', 'buildShellCommand', 'captureWorkspaceSnapshot', 'collectDoctorReport', 'detectChromeCompanionStatus', 'detectIdeTargets', 'detectTmuxAvailability', 'execText', 'findCommandInPath', 'getChromeNativeHostManifestPath', 'getNpmCommand', 'getProjectRoot', 'getStateRoot', 'handleChromeCompanionRpcPayload', 'inspectChromeNativeHostManifest', 'inspectHealthCheckRevalidation', 'inspectInstallState', 'installChromeNativeHostManifest', 'json', 'loadAuth', 'loadDaemonState', 'loadMcpServers', 'loadPlugins', 'maybeAutoConnectIde', 'maybeExecIntoTmux', 'newestMtimeInTree', 'now_iso', 'os', 'pathExists', 'quoteShellArg', 're', 'repairSessionTimestamps', 'resolveChromeCompanionServer', 'resolveTmuxLaunchContextFromEnv', 'revalidateHealthCheck', 'runChromeCompanionMcpServer', 'runChromeNativeHost', 'runRepoInstall', 'runRepoUpdate', 'shutil', 'spawnDetached', 'stat', 'subprocess', 'summarizeWorkspaceCapabilities', 'sys']
_PUBLIC_EXPORTS += [
    'BridgeRecoveryBlockedError',
    'inspectBridgeRecovery',
    'resolveBridgeRecovery',
]
__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _PlatformSupportModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _PlatformSupportModule
