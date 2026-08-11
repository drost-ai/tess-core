from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from ..runtime.secrets import is_runtime_secret_ref


LEGACY_UPDATED_AT = "1970-01-01T00:00:00.000Z"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TIMEOUT_FIELDS = (
    "startupTimeoutMs",
    "requestTimeoutMs",
    "connectTimeoutMs",
    "headersTimeoutMs",
    "bodyIdleTimeoutMs",
)


class ExecutableConfigValidationError(ValueError):
    code = "invalid_executable_config"

    def __init__(self, field: str, reason: str):
        super().__init__(f"Invalid executable configuration at {field}: {reason}")
        self.field = field


def _fail(field: str, reason: str) -> Any:
    raise ExecutableConfigValidationError(field, reason)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _fail(field, "expected an object")
    return dict(value)


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        return _object(value, field)
    try:
        return _object(json.loads(value), field)
    except ExecutableConfigValidationError:
        raise
    except Exception:
        return _fail(field, "malformed JSON")


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return _fail(field, "expected a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    return None if value is None else _required_string(value, field)


def _identifier(value: Any, field: str) -> str:
    result = _required_string(value, field)
    if not _ID_PATTERN.fullmatch(result):
        return _fail(
            field,
            "expected a stable identifier using letters, numbers, dot, underscore, or dash",
        )
    return result


def _string_array(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return _fail(field, "expected an array of strings")
    result: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            return _fail(f"{field}[{index}]", "expected a string")
        result.append(entry)
    return result


def _secret_value(value: Any, field: str) -> Any:
    if isinstance(value, str) or is_runtime_secret_ref(value):
        return copy.deepcopy(value)
    return _fail(field, "expected a string or runtime secret reference")


def _secret_record(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    source = _object(value, field)
    result: dict[str, Any] = {}
    for key, entry in source.items():
        if not isinstance(key, str) or not key.strip():
            _fail(f"{field}.<empty>", "expected a non-empty key")
        result[key] = _secret_value(entry, f"{field}.{key}")
    return result


def _updated_at(value: Any, field: str) -> str:
    if value is None:
        return LEGACY_UPDATED_AT
    result = _required_string(value, field)
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError:
        return _fail(field, "expected an ISO-compatible timestamp")
    return result


def _timeout(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 600_000:
        return _fail(field, "expected an integer from 1 through 600000 milliseconds")
    return value


def _oauth(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    source = _object(value, field)
    redirect = None
    if source.get("redirect") is not None:
        raw = _object(source["redirect"], f"{field}.redirect")
        if raw.get("mode") != "loopback":
            _fail(f"{field}.redirect.mode", "expected loopback")
        port = raw.get("port")
        if port is not None and (
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535
        ):
            _fail(f"{field}.redirect.port", "expected an integer from 1 through 65535")
        timeout = raw.get("timeoutSeconds")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            _fail(f"{field}.redirect.timeoutSeconds", "expected a positive finite number")
        redirect = {"mode": "loopback"}
        if port is not None:
            redirect["port"] = port
        if timeout is not None:
            redirect["timeoutSeconds"] = timeout
    result: dict[str, Any] = {}
    client_id = _optional_string(source.get("clientId"), f"{field}.clientId")
    if client_id is not None:
        result["clientId"] = client_id
    if source.get("clientSecret") is not None:
        result["clientSecret"] = _secret_value(source["clientSecret"], f"{field}.clientSecret")
    scopes = _string_array(source.get("scopes"), f"{field}.scopes")
    if scopes is not None:
        result["scopes"] = scopes
    if redirect is not None:
        result["redirect"] = redirect
    return result


def _canonical_http_url(value: Any, field: str) -> str:
    raw = _required_string(value, field)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return _fail(field, "expected an absolute HTTP(S) URL")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return _fail(field, "expected an HTTP(S) URL without embedded credentials")
    if parsed.fragment:
        return _fail(field, "URL fragments are not allowed")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def parseMcpServerConfig(value: Any, field: str = "mcp") -> dict[str, Any]:
    source = _json_object(value, field)
    name = _identifier(source.get("name"), f"{field}.name")
    transport = source.get("transport")
    if transport not in {"stdio", "http", "sse"}:
        return _fail(f"{field}.transport", "expected stdio, http, or sse")
    target = (
        _required_string(source.get("commandOrUrl"), f"{field}.commandOrUrl")
        if transport == "stdio"
        else _canonical_http_url(source.get("commandOrUrl"), f"{field}.commandOrUrl")
    )
    args = _string_array(source.get("args"), f"{field}.args")
    env = _secret_record(source.get("env"), f"{field}.env")
    headers = _secret_record(source.get("headers"), f"{field}.headers")
    oauth = _oauth(source.get("oauth"), f"{field}.oauth")
    if transport == "stdio" and (headers is not None or oauth is not None):
        return _fail(field, "stdio records cannot define HTTP headers or OAuth")
    if transport != "stdio" and (args is not None or env is not None):
        return _fail(field, "HTTP/SSE records cannot define stdio args or env")
    scope = source.get("scope")
    if scope is not None and scope not in {"user", "project", "local"}:
        return _fail(f"{field}.scope", "expected user, project, or local")
    json_value = None if source.get("json") is None else _object(source["json"], f"{field}.json")
    result: dict[str, Any] = {
        "name": name,
        "transport": transport,
        "commandOrUrl": target,
        "updatedAt": _updated_at(source.get("updatedAt"), f"{field}.updatedAt"),
    }
    for key, value_value in (
        ("args", args),
        ("env", env),
        ("headers", headers),
        ("scope", scope),
        ("json", json_value),
        ("oauth", oauth),
    ):
        if value_value is not None:
            result[key] = value_value
    for key in _TIMEOUT_FIELDS:
        timeout = _timeout(source.get(key), f"{field}.{key}")
        if timeout is not None:
            result[key] = timeout
    return result


def parseMcpServerConfigList(value: Any, field: str = "mcp") -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    result = [parseMcpServerConfig(entry, f"{field}[{index}]") for index, entry in enumerate(values)]
    names: set[str] = set()
    for server in result:
        key = server["name"].lower()
        if key in names:
            _fail(field, f"duplicate MCP server name {server['name']}")
        names.add(key)
    return result


def _capabilities(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    source = _object(value, field)
    result: dict[str, Any] = {}
    for key in ("tools", "commands", "mcpServers", "network", "backgroundTasks"):
        if source.get(key) is not None and not isinstance(source[key], bool):
            _fail(f"{field}.{key}", "expected a boolean")
        if source.get(key) is not None:
            result[key] = source[key]
    filesystem = source.get("filesystem")
    if filesystem is not None and filesystem not in {"none", "workspace", "extended"}:
        _fail(f"{field}.filesystem", "expected none, workspace, or extended")
    if filesystem is not None:
        result["filesystem"] = filesystem
    return result


def _commands(value: Any, field: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return _fail(field, "expected an array")
    names: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        item_field = f"{field}[{index}]"
        source = _object(entry, item_field)
        name = _identifier(source.get("name"), f"{item_field}.name")
        if name.lower() in names:
            _fail(field, f"duplicate command name {name}")
        names.add(name.lower())
        command: dict[str, Any] = {
            "name": name,
            "command": _required_string(source.get("command"), f"{item_field}.command"),
        }
        description = _optional_string(source.get("description"), f"{item_field}.description")
        if description is not None:
            command["description"] = description
        result.append(command)
    return result


def parsePluginManifestV1(value: Any, field: str = "plugin") -> dict[str, Any]:
    source = _json_object(value, field)
    plugin_id = _identifier(source.get("id", source.get("name")), f"{field}.id")
    version = _required_string(source.get("version"), f"{field}.version")
    entry = _optional_string(source.get("entry"), f"{field}.entry")
    if entry and (os.path.isabs(entry) or ".." in re.split(r"[\\/]", entry)):
        return _fail(f"{field}.entry", "expected a relative path contained by the plugin root")
    result: dict[str, Any] = {"id": plugin_id, "version": version}
    for key in ("name", "description", "marketplace"):
        value_value = _optional_string(source.get(key), f"{field}.{key}")
        if value_value is not None:
            result[key] = value_value
    if entry is not None:
        result["entry"] = entry
    capabilities = _capabilities(source.get("capabilities"), f"{field}.capabilities")
    if capabilities is not None:
        result["capabilities"] = capabilities
    if source.get("mcpServers") is not None:
        result["mcpServers"] = parseMcpServerConfigList(source["mcpServers"], f"{field}.mcpServers")
    commands = _commands(source.get("commands"), f"{field}.commands")
    if commands is not None:
        result["commands"] = commands
    return result


def parseInstalledPluginRecord(value: Any, field: str = "installedPlugin") -> dict[str, Any]:
    source = _object(value, field)
    if source.get("enabled") not in {True, False}:
        _fail(f"{field}.enabled", "expected a boolean")
    if source.get("scope") not in {"user", "project", "local"}:
        _fail(f"{field}.scope", "expected user, project, or local")
    return {
        "manifest": parsePluginManifestV1(source.get("manifest"), f"{field}.manifest"),
        "enabled": source["enabled"],
        "scope": source["scope"],
        "installedAt": _updated_at(source.get("installedAt"), f"{field}.installedAt"),
        "source": _required_string(source.get("source"), f"{field}.source"),
    }


def parseMarketplaceManifest(value: Any, field: str = "marketplace") -> dict[str, Any]:
    source = _json_object(value, field)
    plugins = source.get("plugins")
    if not isinstance(plugins, list):
        return _fail(f"{field}.plugins", "expected an array")
    parsed = [parsePluginManifestV1(entry, f"{field}.plugins[{index}]") for index, entry in enumerate(plugins)]
    ids: set[str] = set()
    for plugin in parsed:
        key = plugin["id"].lower()
        if key in ids:
            _fail(f"{field}.plugins", f"duplicate plugin id {plugin['id']}")
        ids.add(key)
    return {
        "name": _identifier(source.get("name"), f"{field}.name"),
        "source": _required_string(source.get("source"), f"{field}.source"),
        "updatedAt": _updated_at(source.get("updatedAt"), f"{field}.updatedAt"),
        "plugins": parsed,
    }


