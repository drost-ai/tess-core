from __future__ import annotations

import os
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
    normalizeProviderProfileName,
    resolveFallbackProviderProfileName,
    resolveProviderForModelSelection,
)
from ..storage import (
    get_default_auto_mode,
    load_auto_mode,
    load_session,
    load_settings,
    save_auto_mode,
    save_settings,
)
from .common import (
    _call,
    _error,
    _info,
    _persist,
    _short_id,
)
from .shared import PERMISSION_MODES, PERMISSION_MODE_ORDER
from typing import Any
from ..payloads.todo_policy import summarizeTodoState as _summarize_todo_state


def buildStatusReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    provider = session.get("provider") or config.get("provider")
    compaction_state = metadata.get("compactionState") or {}
    token_measurement = compaction_state.get("workingContextTokenMeasurement") or {}
    full_log = metadata.get("fullTranscriptLog") or {}
    return "\n".join(
        [
            f"session      {session.get('id', 'n/a')}",
            f"cwd          {session.get('cwd') or config.get('cwd') or 'n/a'}",
            f"profile      {session.get('providerProfileName') or config.get('providerProfileName') or 'n/a'}",
            f"provider     {provider or 'n/a'}",
            f"model        {session.get('model') or config.get('model') or 'n/a'}",
            f"mode         {config.get('openaiApiMode') if provider == 'openai' else 'messages'}",
            f"base url     {(config.get('openaiBaseUrl') or 'default') if provider == 'openai' else 'default'}",
            f"effort       {session.get('effort') or config.get('effort') or 'n/a'}",
            f"permissions  {session.get('permissionMode') or config.get('permissionMode') or 'n/a'}",
            f"messages     {len(session.get('messages') or [])}",
            f"todos        {len(session.get('todos') or [])}",
            f"monitors     {len(metadata.get('monitorIds') or [])}",
            f"usage        {formatUsageSummary(metadata.get('usage'))}",
            f"compactions  {metadata.get('compactions', 0)}",
            f"working ctx  {compaction_state.get('workingContextTokenEstimate', 'n/a')}",
            f"ctx source   {token_measurement.get('source', 'n/a')}",
            f"preflight    {formatPreflightTokenSummary(session)}",
            f"compacted to {compaction_state.get('compactedThroughMessageIndex', 'n/a')}",
            f"archived     {full_log.get('archivedMessageCount', 0)}",
            f"archive log  {full_log.get('path', 'n/a')}",
        ]
    )


def buildProviderProfilesReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    profiles = config.get("providerProfiles") or {}
    active = session.get("providerProfileName") or config.get("providerProfileName")
    sections = []
    for name in listProviderProfiles(profiles):
        profile = profiles.get(name, {})
        if not profile:
            continue
        kind = profile.get("kind") or ""
        lines = [f"{name}{'  (active)' if name == active else ''}", f"  kind       {kind}"]
        if kind == "openai":
            lines.append(f"  mode       {profile.get('apiMode') or 'responses'}")
            lines.append(f"  base url   {profile.get('baseUrl') or 'default'}")
        lines.append(f"  model      {profile.get('defaultModel') or getDefaultModelForProvider(kind)}")
        auth = profile.get("auth") or {}
        auth_text = str(auth.get("kind") or "")
        if auth.get("envVar"):
            auth_text = f"{auth_text} via {auth.get('envVar')}"
        lines.append(f"  auth       {auth_text}")
        if profile.get("description"):
            lines.append(f"  about      {profile.get('description')}")
        if any(
            profile.get(key) is not None
            for key in (
                "contextReliefThresholdTokens",
                "contextReliefTargetTokens",
                "inlineToolResultTurnBytes",
            )
        ):
            lines.append(
                "  retired    interactive relief and turn budget settings are ignored"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if sections else "No provider profiles configured."


async def buildPermissionsReport(session: dict[str, Any], config: dict[str, Any]) -> str:
    current = session.get("permissionMode") or config.get("permissionMode") or "default"
    metadata = session.get("metadata") or {}
    parent_session_id = metadata.get("parentSessionId") or metadata.get("leaderSessionId")
    lines = [f"current  {current}"]
    if parent_session_id:
        parent = await load_session(parent_session_id)
        parent_mode = (parent or {}).get("permissionMode")
        if parent_mode:
            lines.append(f"inherited {parent_mode} from {_short_id(parent_session_id)}")
            if parent_mode != current:
                lines.append(f"effective {parent_mode} while running as an interactive child")
        else:
            lines.append(f"inherited unknown from {_short_id(parent_session_id)}")
    lines.append(f"modes    {', '.join(PERMISSION_MODE_ORDER)}")
    return "\n".join(lines)


async def _persist_shell_settings(
    config: dict[str, Any],
    session: dict[str, Any],
    updates: dict[str, Any],
    hooks: dict[str, Any],
) -> None:
    clean_updates = {key: value for key, value in updates.items() if value is not None}
    if not clean_updates:
        return
    cwd = session.get("cwd") or config.get("cwd") or os.getcwd()
    scope = config.get("settingsWriteScope") or config.get("settingsScope") or "project"
    if scope not in {"user", "project", "local"}:
        scope = "project"
    settings = await load_settings(scope, cwd)
    await save_settings(scope, cwd, {**settings, **clean_updates})
    persist_choice = hooks.get("persistConfigChoice") if isinstance(hooks, dict) else None
    if callable(persist_choice):
        outcome = persist_choice({"scope": scope, "cwd": cwd, "updates": clean_updates})
        if hasattr(outcome, "__await__"):
            await outcome


async def _handle_provider(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        provider = session.get("provider") or config.get("provider")
        return _info(
            hooks,
            "Provider",
            "\n".join(
                [
                    f"profile   {session.get('providerProfileName') or config.get('providerProfileName')}",
                    f"kind      {provider}",
                    f"mode      {config.get('openaiApiMode') if provider == 'openai' else 'messages'}",
                    f"base url  {(config.get('openaiBaseUrl') or 'default') if provider == 'openai' else 'default'}",
                ]
            ),
        )
    requested = normalizeProviderProfileName(arg_text)
    profiles = config.get("providerProfiles") or {}
    if not requested or requested not in profiles:
        return _error(hooks, f"Unknown provider profile: {arg_text}")
    profile = profiles[requested]
    session["providerProfileName"] = requested
    config["providerProfileName"] = requested
    config["providerProfile"] = profile
    session["provider"] = config["provider"] = config["providerKind"] = profile.get("kind")
    if profile.get("kind") == "openai":
        config["openaiApiMode"] = profile.get("apiMode") or config.get("openaiApiMode")
        config["openaiBaseUrl"] = profile.get("baseUrl")
        config.pop("anthropicBaseUrl", None)
    else:
        config["anthropicBaseUrl"] = profile.get("baseUrl")
        config.pop("openaiBaseUrl", None)
    if profile.get("defaultModel"):
        session["model"] = config["model"] = normalizeModelName(profile["defaultModel"], profile.get("kind"))
        session["effort"] = config["effort"] = normalizeEffortForModel(profile.get("kind"), session.get("model"), session.get("effort") or config.get("effort")) or getDefaultEffortForProvider(profile.get("kind"))
    await _persist_shell_settings(
        config,
        session,
        {
            "provider": requested,
            "model": session.get("model") or config.get("model"),
            "effortLevel": session.get("effort") or config.get("effort"),
        },
        hooks,
    )
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Provider", f"Provider profile set to {requested}")


async def _handle_model(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    provider = session.get("provider") or config.get("provider") or "anthropic"
    if not arg_text:
        lines = [
            f"profile   {session.get('providerProfileName') or config.get('providerProfileName')}",
            f"provider  {provider}",
            f"current   {session.get('model') or config.get('model')}",
            "",
        ]
        for name in listProviders():
            lines.append(f"{name}{'  (active)' if name == provider else ''}")
            lines.append(f"  {', '.join(listModelsForProvider(name))}")
        lines.extend(["", "tip       /model openai:gpt-5.4 or /model anthropic:claude-opus-4-6"])
        return _info(hooks, "Model", "\n".join(lines))
    selection_provider, model = _parse_model_selection(arg_text, provider)
    switched_provider = selection_provider != provider
    session["provider"] = config["provider"] = config["providerKind"] = selection_provider
    session["model"] = config["model"] = normalizeModelName(model, selection_provider)
    profile_name = resolveFallbackProviderProfileName(
        selection_provider,
        config.get("providerProfiles") or {},
        None if switched_provider else session.get("providerProfileName") or config.get("providerProfileName"),
    )
    session["providerProfileName"] = config["providerProfileName"] = profile_name
    profile = (config.get("providerProfiles") or {}).get(profile_name) or config.get("providerProfile") or {}
    config["providerProfile"] = profile
    if profile.get("kind") == "openai":
        config["openaiApiMode"] = profile.get("apiMode") or config.get("openaiApiMode")
        config["openaiBaseUrl"] = profile.get("baseUrl")
        config.pop("anthropicBaseUrl", None)
    elif profile:
        config["anthropicBaseUrl"] = profile.get("baseUrl")
        config.pop("openaiBaseUrl", None)
    next_effort = getDefaultEffortForProvider(selection_provider) if switched_provider else session.get("effort") or config.get("effort")
    session["effort"] = config["effort"] = normalizeEffortForModel(selection_provider, session["model"], next_effort) or getDefaultEffortForProvider(selection_provider)
    await _persist_shell_settings(
        config,
        session,
        {
            "provider": profile_name or selection_provider,
            "model": session.get("model"),
            "effortLevel": session.get("effort"),
        },
        hooks,
    )
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Model", f"Model set to {selection_provider}:{session['model']}")


async def _handle_effort(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        return _info(hooks, "Effort", f"current  {session.get('effort') or config.get('effort')}\nlevels   low, medium, high, xhigh, max")
    effort = normalizeEffortLevel(arg_text)
    if not effort:
        return _error(hooks, f"Unknown effort level: {arg_text}. Expected one of: low, medium, high, xhigh, max")
    provider = session.get("provider") or config.get("provider") or "anthropic"
    model = session.get("model") or config.get("model") or ""
    if not modelSupportsEffort(provider, model):
        return _error(hooks, f"The active model does not support configurable reasoning effort: {model}")
    normalized = normalizeEffortForModel(provider, model, effort)
    if not normalized:
        return _error(hooks, f"Effort {effort} is not supported for {provider}:{model}")
    session["effort"] = config["effort"] = normalized
    await _persist_shell_settings(config, session, {"effortLevel": normalized}, hooks)
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Effort", f"Effort set to {normalized}")


async def _handle_permissions(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if not arg_text:
        return _info(hooks, "Permissions", await buildPermissionsReport(session, config))
    if arg_text.split(None, 1)[0] in {"policy", "allow", "deny", "reset"}:
        return await _handle_auto_mode_policy(arg_text, session, config, hooks)
    if arg_text not in PERMISSION_MODES:
        return _error(hooks, f"Unknown permission mode: {arg_text}. Expected one of: {', '.join(PERMISSION_MODE_ORDER)}")
    session["permissionMode"] = config["permissionMode"] = arg_text
    await _persist_shell_settings(config, session, {"permissionMode": arg_text}, hooks)
    await _persist(hooks)
    _call(hooks, "refresh")
    return _info(hooks, "Permissions", f"Permission mode set to {arg_text}")


async def _handle_auto_mode_policy(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    cwd = session.get("cwd") or config.get("cwd") or os.getcwd()
    parts = arg_text.split(None, 2)
    command = parts[0]
    policy = await load_auto_mode(cwd)
    if command == "policy":
        return _info(hooks, "Permissions", formatAutoModePolicy(policy))
    if command == "reset":
        policy = get_default_auto_mode()
        await save_auto_mode(cwd, policy)
        await _persist(hooks)
        return _info(hooks, "Permissions", "Auto-mode policy reset.\n\n" + formatAutoModePolicy(policy))
    if len(parts) < 3 or parts[1] not in {"add", "remove"}:
        return _error(hooks, "Usage: /permissions [policy|allow add TEXT|allow remove TEXT|deny add TEXT|deny remove TEXT|reset]")
    bucket = command
    action = parts[1]
    value = parts[2].strip()
    if not value:
        return _error(hooks, "Policy entry cannot be empty.")
    entries = [str(item) for item in policy.get(bucket, []) if str(item).strip()]
    if action == "add" and value not in entries:
        entries.append(value)
    if action == "remove":
        entries = [item for item in entries if item != value]
    policy[bucket] = entries
    await save_auto_mode(cwd, policy)
    await _persist(hooks)
    verb = "Added" if action == "add" else "Removed"
    return _info(hooks, "Permissions", f"{verb} auto-mode {bucket} entry: {value}\n\n{formatAutoModePolicy(policy)}")


def formatAutoModePolicy(policy: dict[str, Any]) -> str:
    allow = policy.get("allow") or []
    deny = policy.get("deny") or []
    environment = policy.get("environment") or {}
    return "\n".join(
        [
            "auto allow",
            *[f"  - {item}" for item in allow[:12]],
            "auto deny",
            *[f"  - {item}" for item in deny[:12]],
            f"auto env    {len(environment)} entries",
        ]
    )


def compactModelName(model: str) -> str:
    return model.replace("claude-", "").replace("-2025-08-07", "")


def normalizeModel(inputModel: str) -> str:
    return normalizeModelName(inputModel)


def formatUsageSummary(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "no tracked usage"
    summary = f"{usage.get('inputTokens', 0)} in · {usage.get('outputTokens', 0)} out · {usage.get('toolCalls', 0)} tools · {usage.get('turns', 0)} turns"
    if usage.get("cacheCreationTtlDetailComplete") is True:
        summary += f" · written: 5m {usage.get('cacheCreation5mInputTokens', 0)}, 1h {usage.get('cacheCreation1hInputTokens', 0)}"
    return summary


def formatUsageBreakdown(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "No tracked usage for this session yet."
    lines = [
            f"turns                    {usage.get('turns', 0)}",
            f"tool calls               {usage.get('toolCalls', 0)}",
            f"input tokens             {usage.get('inputTokens', 0)}",
            f"output tokens            {usage.get('outputTokens', 0)}",
            f"cache creation tokens    {usage.get('cacheCreationInputTokens', 0)}",
            f"cache read tokens        {usage.get('cacheReadInputTokens', 0)}",
        ]
    if usage.get("cacheCreationTtlDetailComplete") is True:
        lines.append(
            f"cache written by TTL     5m {usage.get('cacheCreation5mInputTokens', 0)}, 1h {usage.get('cacheCreation1hInputTokens', 0)}"
        )
    return "\n".join(lines)


def formatPreflightTokenSummary(session: dict[str, Any]) -> str:
    measurement = ((session.get("metadata") or {}).get("tokenTracking") or {}).get("lastPreflight")
    if not isinstance(measurement, dict):
        return "n/a"
    margin = f" (+{measurement.get('safetyMarginTokens')})" if measurement.get("safetyMarginTokens") else ""
    return f"{measurement.get('inputTokens')}{margin} via {measurement.get('source')}"


def buildTodosReport(session: dict[str, Any]) -> str:
    summary = _summarize_todo_state(session.get("todos") or [])
    if summary["originalItemCount"] == 0:
        return "No todos."
    lines = [
        f"{todo['position']}. [{todo['status']}] {todo['content']}"
        for todo in summary["todos"]
    ]
    if summary["itemsTruncated"]:
        lines.append(
            f"… {summary['omittedItemCount']} omitted "
            f"({summary['returnedItemCount']} of {summary['originalItemCount']} shown)."
        )
    else:
        lines.append(f"{summary['originalItemCount']} total.")
    return "\n".join(lines)


def _parse_command(raw: str) -> tuple[str, str]:
    parts = raw[1:].split(None, 1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "")


def _parse_model_selection(value: str, fallback_provider: str) -> tuple[str, str]:
    raw = value.strip()
    provider_only = normalizeProviderName(raw)
    if provider_only:
        return provider_only, getDefaultModelForProvider(provider_only)
    if ":" in raw:
        prefix, model = raw.split(":", 1)
        explicit_model = model.strip()
        provider = resolveProviderForModelSelection(normalizeProviderName(prefix), explicit_model) or fallback_provider
        return provider, explicit_model or getDefaultModelForProvider(provider)
    provider = resolveProviderForModelSelection(None, raw) or fallback_provider
    return provider, raw or getDefaultModelForProvider(provider)


def _sync_effective_provider(session: dict[str, Any], config: dict[str, Any]) -> None:
    model = session.get("model") or config.get("model")
    effective_provider = ensureProviderForModelSelection(
        session.get("provider") or config.get("provider"),
        model,
        config.get("provider") or "anthropic",
    )
    if effective_provider != session.get("provider"):
        session["provider"] = effective_provider
    if effective_provider != config.get("provider"):
        config["provider"] = effective_provider
        config["providerKind"] = effective_provider
    profile_name = resolveFallbackProviderProfileName(
        effective_provider,
        config.get("providerProfiles") or {},
        session.get("providerProfileName") or config.get("providerProfileName"),
    )
    session["providerProfileName"] = profile_name
    config["providerProfileName"] = profile_name


def _find_runtime_command(name: str, commands: list[Any]) -> Any:
    for command in commands:
        if isinstance(command, dict) and command.get("name", "").lower() == name.lower():
            return command
    return None
