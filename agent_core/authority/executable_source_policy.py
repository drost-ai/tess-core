from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any

from .executable_closure import (
    inspectExecutableClosure,
    mapClosureRootToSnapshot,
    snapshotExecutableClosure,
)
from .executable_config import parseInstalledPluginRecord, parseMcpServerConfigList
from .repository_trust import (
    approveRepositoryContent,
    checkRepositoryApproval,
    resolveRepositoryIdentity,
)
from ..storage_store.json_io import resolveMaybeRelative
from ..runtime.secrets import assertNoLiteralMcpSecrets, migrateLegacyMcpServerSecrets
from ..storage_store.settings import loadMcpServers, loadPlugins, saveMcpServers


SETTING_SOURCES = ("user", "project", "local")


class ExecutableSourceTrustError(PermissionError):
    code = "executable_source_trust_required"

    def __init__(self, family: str, scope: str):
        super().__init__(
            f"{scope} {family} configuration is not trusted for this repository. "
            f"Review and explicitly approve the current {family} source before launching it."
        )
        self.family = family
        self.scope = scope


def parseSettingSources(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return list(SETTING_SOURCES)
    parts = (
        [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(raw, str)
        else [str(part).strip() for part in raw if str(part).strip()]
    )
    if not parts:
        raise ValueError("settingSources must select at least one of: user, project, local")
    for part in parts:
        if part not in SETTING_SOURCES:
            raise ValueError(f"Unknown setting source {part!r}; expected user, project, or local")
    return list(dict.fromkeys(parts))


def _stable(value: Any) -> Any:
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, dict):
        return {key: _stable(value[key]) for key in sorted(value) if value[key] is not None}
    return value


def executableConfigDigest(value: Any) -> str:
    payload = json.dumps(
        {"version": 1, "value": _stable(value)},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def selectMcpBootstrapServers(
    policy: dict[str, Any] | None,
    explicitAndSelected: list[dict[str, Any]],
    pluginServers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return (
        list(explicitAndSelected)
        if (policy or {}).get("mode") == "explicit_only"
        else [*explicitAndSelected, *pluginServers]
    )


def _normalized_mcp_records(scope: str, records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError(f"Invalid {scope} MCP configuration: expected an array")
    return [
        {**record, "scope": scope}
        for record in parseMcpServerConfigList(records, f"{scope}.mcp")
    ]


async def _load_scoped_mcp_records(scope: str, cwd: str) -> list[dict[str, Any]]:
    records = _normalized_mcp_records(scope, await loadMcpServers(scope, cwd))
    if scope == "user":
        migration = await migrateLegacyMcpServerSecrets(records)
        if migration["changed"]:
            await saveMcpServers("user", cwd, migration["records"])
        return migration["records"]
    assertNoLiteralMcpSecrets(records, scope)
    return records


async def _mcp_closure(cwd: str, scope: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    repository = await resolveRepositoryIdentity(cwd)
    roots: list[str] = []
    bindings: list[dict[str, Any]] = []
    for record_index, record in enumerate(records):
        if record.get("transport") != "stdio":
            continue
        candidates = [("commandOrUrl", None, str(record["commandOrUrl"]))]
        candidates.extend(("arg", index, value) for index, value in enumerate(record.get("args") or []))
        for field, arg_index, value in candidates:
            if not value or (field == "arg" and value.startswith("-")):
                continue
            candidate = value if os.path.isabs(value) else os.path.abspath(os.path.join(cwd, value))
            if not os.path.lexists(candidate):
                continue
            canonical = os.path.realpath(candidate)
            try:
                contained = os.path.commonpath((repository["canonicalRoot"], canonical)) == repository["canonicalRoot"]
            except ValueError:
                contained = False
            if not contained and field == "commandOrUrl" and os.path.isabs(value):
                continue
            if candidate not in roots:
                roots.append(candidate)
            bindings.append(
                {
                    "recordIndex": record_index,
                    "field": field,
                    "argIndex": arg_index,
                    "sourcePath": canonical,
                }
            )
    return {
        "spec": {
            "cwd": cwd,
            "family": "mcp",
            "scope": scope,
            "descriptorDigest": executableConfigDigest(records),
            "roots": roots,
        },
        "bindings": bindings,
    }


async def inspectMcpSourceTrust(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd, scope = str(args["cwd"]), str(args["scope"])
    records = await _load_scoped_mcp_records(scope, cwd)
    closure = await _mcp_closure(cwd, scope, records)
    inspection = await inspectExecutableClosure(closure["spec"])
    approved = not records or await checkRepositoryApproval(
        cwd=cwd,
        family="mcp",
        scope=scope,
        contentDigest=inspection["digest"],
    )
    return {
        "scope": scope,
        "contentDigest": inspection["digest"],
        "approved": approved,
        "count": len(records),
    }


async def approveMcpSource(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    current = await inspectMcpSourceTrust(args)
    await approveRepositoryContent(
        cwd=args["cwd"],
        family="mcp",
        scope=args["scope"],
        contentDigest=current["contentDigest"],
    )
    return {**current, "approved": True}


async def loadSelectedMcpServers(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    cwd = str(args["cwd"])
    selected: list[dict[str, Any]] = []
    for scope in parseSettingSources(args.get("sources")):
        records = await _load_scoped_mcp_records(scope, cwd)
        if scope != "user" and records:
            closure = await _mcp_closure(cwd, scope, records)
            inspection = await inspectExecutableClosure(closure["spec"])
            if not await checkRepositoryApproval(
                cwd=cwd,
                family="mcp",
                scope=scope,
                contentDigest=inspection["digest"],
            ):
                raise ExecutableSourceTrustError("mcp", scope)
            snapshot = await snapshotExecutableClosure(
                spec=closure["spec"], expectedDigest=inspection["digest"]
            )
            for binding in closure["bindings"]:
                replacement = mapClosureRootToSnapshot(snapshot, binding["sourcePath"])
                if replacement is None:
                    raise RuntimeError("Executable closure snapshot omitted an MCP path binding")
                record = records[binding["recordIndex"]]
                if binding["field"] == "commandOrUrl":
                    record["commandOrUrl"] = replacement
                else:
                    record["args"][binding["argIndex"]] = replacement
        selected.extend(records)
    return selected


def _normalized_plugins(scope: str, records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError(f"Invalid {scope} plugin configuration: expected an array")
    result: list[dict[str, Any]] = []
    for index, value in enumerate(records):
        source = dict(value) if isinstance(value, dict) else value
        if isinstance(source, dict):
            source["scope"] = scope
        result.append(parseInstalledPluginRecord(source, f"{scope}.plugins[{index}]"))
    return result


async def _plugin_closure(cwd: str, scope: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    roots: list[str] = []
    source_paths: list[str | None] = [None] * len(records)
    for index, record in enumerate(records):
        if not record.get("enabled") or str(record.get("source")).startswith(("http://", "https://")):
            continue
        candidate = resolveMaybeRelative(cwd, str(record.get("source")))
        if not os.path.lexists(candidate):
            continue
        roots.append(candidate)
        source_paths[index] = os.path.realpath(candidate)
    return {
        "spec": {
            "cwd": cwd,
            "family": "plugin",
            "scope": scope,
            "descriptorDigest": executableConfigDigest([record for record in records if record.get("enabled")]),
            "roots": roots,
        },
        "sourcePaths": source_paths,
    }


async def inspectPluginSourceTrust(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd, scope = str(args["cwd"]), str(args["scope"])
    records = _normalized_plugins(scope, await loadPlugins(scope, cwd))
    enabled = [record for record in records if record.get("enabled")]
    closure = await _plugin_closure(cwd, scope, records)
    inspection = await inspectExecutableClosure(closure["spec"])
    approved = not enabled or await checkRepositoryApproval(
        cwd=cwd,
        family="plugin",
        scope=scope,
        contentDigest=inspection["digest"],
    )
    return {
        "scope": scope,
        "contentDigest": inspection["digest"],
        "approved": approved,
        "count": len(enabled),
    }


async def approvePluginSource(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    current = await inspectPluginSourceTrust(args)
    await approveRepositoryContent(
        cwd=args["cwd"],
        family="plugin",
        scope=args["scope"],
        contentDigest=current["contentDigest"],
    )
    return {**current, "approved": True}


async def loadSelectedPluginRecords(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    cwd = str(args["cwd"])
    result: list[dict[str, Any]] = []
    for scope in parseSettingSources(args.get("sources")):
        records = _normalized_plugins(scope, await loadPlugins(scope, cwd))
        enabled = [record for record in records if record.get("enabled")]
        if scope == "user" or not enabled:
            result.extend(records)
            continue
        closure = await _plugin_closure(cwd, scope, records)
        inspection = await inspectExecutableClosure(closure["spec"])
        if not await checkRepositoryApproval(
            cwd=cwd,
            family="plugin",
            scope=scope,
            contentDigest=inspection["digest"],
        ):
            raise ExecutableSourceTrustError("plugin", scope)
        snapshot = await snapshotExecutableClosure(
            spec=closure["spec"], expectedDigest=inspection["digest"]
        )
        for index, record in enumerate(records):
            canonical = closure["sourcePaths"][index]
            if not record.get("enabled") or not canonical:
                result.append(record)
                continue
            replacement = mapClosureRootToSnapshot(snapshot, canonical)
            if replacement is None:
                raise RuntimeError("Executable closure snapshot omitted a plugin path binding")
            result.append({**record, "source": replacement})
    return result


parse_setting_sources = parseSettingSources
executable_config_digest = executableConfigDigest
select_mcp_bootstrap_servers = selectMcpBootstrapServers
