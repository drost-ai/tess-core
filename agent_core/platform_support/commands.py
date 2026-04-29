from __future__ import annotations

from .shared import *

async def pathExists(targetPath: str) -> bool:
    return os.path.exists(targetPath)

async def execText(command: str, args: list[str] | None = None, cwd: str | None = None) -> str:
    result = subprocess.run([command, *(args or [])], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.strip()

async def findCommandInPath(command: str) -> str | None:
    return shutil.which(command)

def getNpmCommand() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"

def quoteShellArg(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+=,-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"

def buildShellCommand(command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> str:
    prefix = ""
    if env:
        env_items = [(key, value) for key, value in env.items() if isinstance(value, str) and value]
        prefix = (" ".join(f"{key}={quoteShellArg(value)}" for key, value in env_items) + " ") if env_items else ""
    return prefix + " ".join([quoteShellArg(command), *[quoteShellArg(arg) for arg in (args or [])]])

async def spawnDetached(command: str, args: list[str] | None = None, cwd: str | None = None) -> None:
    proc = subprocess.Popen([command, *(args or [])], cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.poll()
    return None

async def newestMtimeInTree(rootPath: str) -> float | None:
    if not os.path.exists(rootPath):
        return None
    newest = None
    for root, _dirs, files in os.walk(rootPath):
        _dirs[:] = [directory for directory in _dirs if directory not in {"node_modules", ".git"}]
        for file in files:
            try:
                mtime = os.path.getmtime(os.path.join(root, file))
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return newest

async def _runner_which(runner: dict[str, Any], command: str) -> str | None:
    fn = runner.get("which") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command)
        if hasattr(result, "__await__"):
            result = await result
        return result
    return shutil.which(command)

async def _runner_exec(runner: dict[str, Any], command: str, args: list[str], cwd: str | None = None) -> str:
    fn = runner.get("exec") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command, args, cwd)
        if hasattr(result, "__await__"):
            result = await result
        return result or ""
    return await execText(command, args, cwd)

async def _runner_exec_text(runner: dict[str, Any], command: str, args: list[str]) -> str:
    fn = runner.get("execText") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command, args)
        if hasattr(result, "__await__"):
            result = await result
        return result or ""
    return await _runner_exec(runner, command, args)

async def _runner_spawn_detached(runner: dict[str, Any], command: str, args: list[str], cwd: str | None = None) -> int:
    fn = runner.get("spawnDetached") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command, args, cwd)
        if hasattr(result, "__await__"):
            result = await result
        return int(result or 0)
    return await spawnDetached(command, args, cwd)

def _safe_realpath(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.realpath(str(path))
    except Exception:
        return str(path)

def _parse_json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
