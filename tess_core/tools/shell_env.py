"""The environment a shell command started by the agent runs in.

The host process holds the model provider's credentials in its environment
(``OPENAI_API_KEY``, or whatever a profile's ``auth.envVar`` names). A command
the model writes has no use for them, and ``env`` or ``echo $OPENAI_API_KEY``
would hand them to the model. They are removed from the command's environment.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

STANDARD_CREDENTIAL_VARIABLES = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "OPENAI_API_KEY"})
# Names of further credential variables, passed from a host to the processes it starts.
CREDENTIAL_NAMES_VARIABLE = "TESS_CORE_CREDENTIAL_ENV"


def profile_credential_variables(profiles: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(profiles, Mapping):
        for profile in profiles.values():
            env_var = ((profile.get("auth") if isinstance(profile, Mapping) else None) or {}).get("envVar")
            if isinstance(env_var, str) and env_var:
                names.add(env_var)
    return names


def credential_variables(config: Mapping[str, Any] | None = None, environment: Mapping[str, str] | None = None) -> set[str]:
    """Every environment variable that holds a provider credential for this runtime."""
    env = os.environ if environment is None else environment
    names = set(STANDARD_CREDENTIAL_VARIABLES)
    names.update(name for name in (env.get(CREDENTIAL_NAMES_VARIABLE) or "").split(",") if name)
    if config:
        names |= profile_credential_variables(config.get("providerProfiles"))
        registry = config.get("providerRegistry")
        if registry is not None:
            names |= profile_credential_variables(getattr(registry, "profiles", None))
    return names


def shell_environment(config: Mapping[str, Any] | None = None, environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """The process environment without provider credentials."""
    env = os.environ if environment is None else environment
    hidden = credential_variables(config, env)
    return {key: value for key, value in env.items() if key not in hidden}
