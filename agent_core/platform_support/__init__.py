"""Host-environment surface: install/doctor, tmux, IDE, and Chrome companion.

The facade re-exports the public names its submodules define, plus the
platform-entrypoint names of docs/public-api-contract.md whose implementations
live in other planes (source trust, executable config parsing, runtime secrets).
"""
from __future__ import annotations

from ..authority.executable_config import (
    parse_installed_plugin_record,
    parse_marketplace_manifest,
    parse_mcp_server_config,
    parse_mcp_server_config_list,
    parse_plugin_manifest_v1,
)
from ..authority.executable_source_policy import (
    approve_mcp_source,
    approve_plugin_source,
    inspect_mcp_source_trust,
    inspect_plugin_source_trust,
    parse_setting_sources,
)
from ..authority.repository_trust import get_repository_trust_status, revoke_repository_approval
from ..runtime.secrets import authorize_runtime_secret_write, is_runtime_secret_ref, store_runtime_secret
from .chrome import (
    build_chrome_native_host_manifest,
    detect_chrome_companion_status,
    get_chrome_native_host_manifest_path,
    handle_chrome_companion_rpc_payload,
    inspect_chrome_native_host_manifest,
    install_chrome_native_host_manifest,
    resolve_chrome_companion_server,
    run_chrome_companion_mcp_server,
    run_chrome_native_host,
)
from .commands import (
    build_shell_command,
    exec_text,
    find_command_in_path,
    get_npm_command,
    newest_mtime_in_tree,
    path_exists,
    quote_shell_arg,
    spawn_detached,
)
from .doctor import collect_doctor_report
from .ide import detect_ide_targets, maybe_auto_connect_ide
from .install import inspect_install_state, run_repo_install, run_repo_update
from .integrations import apply_platform_integration_metadata
from .session_hygiene import build_session_hygiene_report, repair_session_timestamps
from .shared import (
    DEFAULT_CHROME_EXTENSION_ORIGIN,
    DEFAULT_CHROME_NATIVE_HOST_NAME,
    REPO_ROOT,
)
from .tmux import (
    detect_tmux_availability,
    maybe_exec_into_tmux,
    resolve_tmux_launch_context_from_env,
)

__all__ = [
    "DEFAULT_CHROME_EXTENSION_ORIGIN",
    "DEFAULT_CHROME_NATIVE_HOST_NAME",
    "REPO_ROOT",
    "apply_platform_integration_metadata",
    "build_chrome_native_host_manifest",
    "build_session_hygiene_report",
    "build_shell_command",
    "collect_doctor_report",
    "detect_chrome_companion_status",
    "detect_ide_targets",
    "detect_tmux_availability",
    "exec_text",
    "find_command_in_path",
    "get_chrome_native_host_manifest_path",
    "get_npm_command",
    "handle_chrome_companion_rpc_payload",
    "inspect_chrome_native_host_manifest",
    "inspect_install_state",
    "install_chrome_native_host_manifest",
    "maybe_auto_connect_ide",
    "maybe_exec_into_tmux",
    "newest_mtime_in_tree",
    "path_exists",
    "quote_shell_arg",
    "repair_session_timestamps",
    "resolve_chrome_companion_server",
    "resolve_tmux_launch_context_from_env",
    "run_chrome_companion_mcp_server",
    "run_chrome_native_host",
    "run_repo_install",
    "run_repo_update",
    "spawn_detached",
]
