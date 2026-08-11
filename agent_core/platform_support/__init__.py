"""Host-environment surface: install/doctor, tmux, IDE, and Chrome companion.

The facade re-exports the public names its submodules define, plus the
platform-entrypoint names of docs/public-api-contract.md whose implementations
live in other planes (source trust, executable config parsing, runtime secrets).
"""
from __future__ import annotations

from ..authority.executable_config import (
    parseInstalledPluginRecord,
    parseMarketplaceManifest,
    parseMcpServerConfig,
    parseMcpServerConfigList,
    parsePluginManifestV1,
)
from ..authority.executable_source_policy import (
    approveMcpSource,
    approvePluginSource,
    inspectMcpSourceTrust,
    inspectPluginSourceTrust,
    parseSettingSources,
)
from ..authority.repository_trust import getRepositoryTrustStatus, revokeRepositoryApproval
from ..runtime.secrets import authorize_runtime_secret_write, is_runtime_secret_ref, store_runtime_secret
from .chrome import (
    buildChromeNativeHostManifest,
    detectChromeCompanionStatus,
    getChromeNativeHostManifestPath,
    handleChromeCompanionRpcPayload,
    inspectChromeNativeHostManifest,
    installChromeNativeHostManifest,
    resolveChromeCompanionServer,
    runChromeCompanionMcpServer,
    runChromeNativeHost,
)
from .commands import (
    buildShellCommand,
    execText,
    findCommandInPath,
    getNpmCommand,
    newestMtimeInTree,
    pathExists,
    quoteShellArg,
    spawnDetached,
)
from .doctor import collectDoctorReport
from .ide import detectIdeTargets, maybeAutoConnectIde
from .install import inspectInstallState, runRepoInstall, runRepoUpdate
from .integrations import applyPlatformIntegrationMetadata
from .session_hygiene import buildSessionHygieneReport, repairSessionTimestamps
from .shared import (
    DEFAULT_CHROME_EXTENSION_ORIGIN,
    DEFAULT_CHROME_NATIVE_HOST_NAME,
    REPO_ROOT,
)
from .tmux import (
    detectTmuxAvailability,
    maybeExecIntoTmux,
    resolveTmuxLaunchContextFromEnv,
)

__all__ = [
    "DEFAULT_CHROME_EXTENSION_ORIGIN",
    "DEFAULT_CHROME_NATIVE_HOST_NAME",
    "REPO_ROOT",
    "applyPlatformIntegrationMetadata",
    "buildChromeNativeHostManifest",
    "buildSessionHygieneReport",
    "buildShellCommand",
    "collectDoctorReport",
    "detectChromeCompanionStatus",
    "detectIdeTargets",
    "detectTmuxAvailability",
    "execText",
    "findCommandInPath",
    "getChromeNativeHostManifestPath",
    "getNpmCommand",
    "handleChromeCompanionRpcPayload",
    "inspectChromeNativeHostManifest",
    "inspectInstallState",
    "installChromeNativeHostManifest",
    "maybeAutoConnectIde",
    "maybeExecIntoTmux",
    "newestMtimeInTree",
    "pathExists",
    "quoteShellArg",
    "repairSessionTimestamps",
    "resolveChromeCompanionServer",
    "resolveTmuxLaunchContextFromEnv",
    "runChromeCompanionMcpServer",
    "runChromeNativeHost",
    "runRepoInstall",
    "runRepoUpdate",
    "spawnDetached",
]
