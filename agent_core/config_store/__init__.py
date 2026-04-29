
from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'defaults', 'providers', 'prompts', 'parsing', 'resolution']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

_PUBLIC_EXPORTS = ['Any', 'BUILTIN_AGENTS', 'BUILTIN_PROVIDER_PROFILES', 'DEFAULT_ASSISTANT_DESCRIPTION', 'DEFAULT_ASSISTANT_NAME', 'DEFAULT_EFFORT', 'DEFAULT_MODEL', 'DEFAULT_OPENAI_API_MODE', 'DEFAULT_OPENAI_CODEX_AUTH_PATH', 'DEFAULT_PROVIDER', 'DEFAULT_PROVIDER_PROFILE_NAME', 'DEFAULT_TUI_SCREEN_MODE', 'PROVIDER_DEFAULT_EFFORT', 'PROVIDER_DEFAULT_MODEL', 'PROVIDER_MODELS', 'VALID_EFFORT_LEVELS', 'annotations', 'buildDefaultSystemPrompt', 'buildEffectiveSystemPrompt', 'buildPlanModeSystemPrompt', 'buildSystemPromptForAgent', 'clone', 'configureRuntimeHome', 'defaultToolList', 'ensureBaseLayout', 'ensureProviderForModelSelection', 'fileOrJson', 'filterToolsByPolicy', 'getDefaultAssistantDescription', 'getDefaultAssistantName', 'getDefaultEffort', 'getDefaultEffortForProvider', 'getDefaultModel', 'getDefaultModelForProvider', 'getDefaultProvider', 'getDefaultSystemPrompt', 'getDefaultToolList', 'getPlansDir', 'inferProviderForModel', 'isCodexBaseUrl', 'isPlanRevisionPrompt', 'json', 'listEffortLevels', 'listModelsForProvider', 'listProviderProfiles', 'listProviderProfilesForKind', 'listProviders', 'loadAuth', 'loadCodexAuthToken', 'loadMcpServers', 'loadSettingsBySources', 'modelSupportsEffort', 'modelSupportsMaxEffort', 'modelSupportsXhighEffort', 'normalizeEffortForModel', 'normalizeEffortLevel', 'normalizeModelName', 'normalizeOpenAiApiModeSetting', 'normalizeProviderName', 'normalizeProviderProfileName', 'normalizeTuiScreenMode', 'os', 're', 'resolveAgentDefinition', 'resolveConfig', 'resolveFallbackProviderProfileName', 'resolveMaybeRelative', 'resolveModelForAgent', 'resolveOpenAiApiMode', 'resolveProviderForModelSelection', 'resolveProviderProfile', 'resolveProviderProfiles', 'resolveRuntimeHome', 'resolveSelectedProviderProfileName', 'resolve_config', 'subprocess']
__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _ConfigStoreModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _ConfigStoreModule
