from __future__ import annotations

from .shared import *

async def readJsonFile(filePath: str, fallback: Any) -> Any:
    try:
        with open(filePath, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback

async def writeJsonFile(filePath: str, value: Any) -> None:
    os.makedirs(os.path.dirname(filePath), exist_ok=True)
    tmp = f"{filePath}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, filePath)

async def appendJsonLine(filePath: str, value: Any) -> None:
    os.makedirs(os.path.dirname(filePath), exist_ok=True)
    with open(filePath, "a", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.write("\n")

def parseJsonString(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)

def fileOrJson(value: str | None) -> Any:
    if not value:
        return None
    trimmed = value.strip()
    if trimmed.startswith("{") or trimmed.startswith("["):
        return json.loads(trimmed)
    with open(trimmed, "r", encoding="utf-8") as handle:
        return json.load(handle)

def resolveMaybeRelative(baseDir: str, candidate: str) -> str:
    expanded = expandHome(candidate)
    return os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(baseDir, expanded))

async def loadAuth() -> dict[str, Any] | None:
    return await readJsonFile(os.path.join(getStateRoot(), "auth.json"), None)

async def loadCodexAuthToken(authPath: str) -> str | None:
    try:
        with open(authPath, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        api_key = str(payload.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            return api_key
        token = str((payload.get("tokens") or {}).get("access_token") or "").strip()
        return token or None
    except Exception:
        return None

async def saveAuth(auth: dict[str, Any]) -> None:
    await writeJsonFile(os.path.join(getStateRoot(), "auth.json"), auth)

async def clearAuth() -> None:
    try:
        os.remove(os.path.join(getStateRoot(), "auth.json"))
    except FileNotFoundError:
        pass

read_json_file = readJsonFile
write_json_file = writeJsonFile
append_json_line = appendJsonLine
resolve_maybe_relative = resolveMaybeRelative
load_auth = loadAuth
load_codex_auth_token = loadCodexAuthToken
