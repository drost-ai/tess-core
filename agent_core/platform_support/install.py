from __future__ import annotations

from .shared import *

async def inspectInstallState(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    repo_root = Path(args.get("repoRoot") or REPO_ROOT)
    pyproject = repo_root / "pyproject.toml"
    version = _read_pyproject_version(pyproject)
    bundled_version = _read_optional_pyproject_version(Path(args.get("bundledPyprojectPath") or repo_root / "package" / "pyproject.toml"))
    package_dir = Path(args.get("distPath") or repo_root / "agent_core")
    source_dir = Path(args.get("sourcePath") or repo_root / "agent_core")
    dist_exists = package_dir.exists()
    dist_stale = await _is_dist_stale(source_dir, package_dir) if dist_exists else True
    global_bin = args.get("globalBinPath") or await _runner_which(runner, "agent-core") or await _runner_which(runner, "tess")
    repo_real = _safe_realpath(repo_root)
    global_package_path = args.get("globalPackagePath") or await _resolve_global_python_package_path(args, runner, repo_root, global_bin)
    global_package_real = _safe_realpath(global_package_path) if global_package_path else None
    mode = "repo-only" if dist_exists else "uninstalled"
    if global_package_real and repo_real and os.path.normcase(global_package_real) == os.path.normcase(repo_real):
        mode = "linked-global"
    elif global_package_path:
        mode = "global-copy"
    return {
        "repoRoot": str(repo_root),
        "repoVersion": version,
        "bundledReleasedVersion": bundled_version,
        "distExists": dist_exists,
        "distStale": dist_stale,
        "distPath": str(package_dir),
        "sourcePath": str(source_dir),
        "globalPackagePath": global_package_path,
        "globalPackageRealPath": global_package_real,
        "globalBinPath": global_bin,
        "mode": mode,
        "installed": dist_exists,
        "python": True,
    }

async def runRepoInstall(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    repo_root = str(Path(args.get("repoRoot") or REPO_ROOT))
    target = args.get("target") or "latest"
    if target not in {"latest", "stable", "0.1.0"}:
        raise RuntimeError(f'This repo-backed install flow does not support target "{target}". Use "latest" or "stable".')
    state = await inspectInstallState(args)
    steps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    python_command = args.get("python") or sys.executable
    should_link = bool(args.get("force")) or state.get("mode") != "linked-global"
    if should_link:
        command_args = ["-m", "pip", "install", "-e", repo_root]
        await _runner_exec(runner, python_command, command_args, repo_root)
        steps.append({"step": "link", "status": "run", "detail": "Installed this checkout in editable mode as the active Python package."})
        actions.append({"type": "exec", "command": python_command, "args": command_args, "cwd": repo_root})
    else:
        steps.append({"step": "link", "status": "skipped", "detail": "`agent-core` is already linked to this checkout."})
    return {"ok": True, "installState": await inspectInstallState(args), "steps": steps, "actions": actions}

async def runRepoUpdate(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    repo_root = str(Path(args.get("repoRoot") or REPO_ROOT))
    state_before = await inspectInstallState(args)
    steps = []
    upstream_status = "skipped"
    upstream_detail = "No upstream git update was attempted."
    git_binary = await _runner_which(runner, "git")
    if git_binary and await _is_git_repo(runner, git_binary, repo_root):
        dirty = await _git_is_dirty(runner, git_binary, repo_root)
        if dirty:
            upstream_detail = "Working tree is dirty, so `git pull --ff-only` was skipped."
        elif await _has_git_upstream(runner, git_binary, repo_root):
            before_head = (await _runner_exec(runner, git_binary, ["rev-parse", "HEAD"], repo_root)).strip()
            await _runner_exec(runner, git_binary, ["pull", "--ff-only"], repo_root)
            after_head = (await _runner_exec(runner, git_binary, ["rev-parse", "HEAD"], repo_root)).strip()
            if before_head == after_head:
                upstream_status = "up-to-date"
                upstream_detail = "Git upstream was already up to date."
            else:
                upstream_status = "updated"
                upstream_detail = "Pulled the latest fast-forward changes from git upstream."
                steps.append({"step": "git_pull", "status": "run", "detail": upstream_detail})
        else:
            upstream_detail = "No git upstream is configured for this checkout."
    else:
        upstream_detail = "This checkout is not a git repo with an available git binary."
    install = await runRepoInstall({**args, "force": False, "target": "latest"})
    steps.extend(install.get("steps") or [])
    state_after = await inspectInstallState(args)
    return {"ok": True, "installStateBefore": state_before, "installStateAfter": state_after, "steps": steps, "upstreamStatus": upstream_status, "upstreamDetail": upstream_detail, "actions": []}

async def _is_dist_stale(source_path: Path, dist_path: Path) -> bool:
    dist_mtime = await newestMtimeInTree(str(dist_path))
    if not dist_mtime:
        return True
    source_mtime = await newestMtimeInTree(str(source_path))
    if not source_mtime:
        return False
    return source_mtime > dist_mtime

async def _resolve_global_python_package_path(
    args: dict[str, Any],
    runner: dict[str, Any],
    repo_root: Path,
    global_bin: str | None,
) -> str | None:
    if not global_bin and not args.get("discoverGlobalPackage"):
        return None
    python_command = args.get("python") or sys.executable
    package_name = args.get("packageName") or "agent-core-python"
    try:
        output = await _runner_exec(runner, python_command, ["-m", "pip", "show", package_name], str(repo_root))
    except Exception:
        return None
    fields = _parse_pip_show(output)
    direct_url = fields.get("editable-project-location") or fields.get("location")
    return direct_url or None

def _parse_pip_show(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in str(output or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip()
    return fields

async def _is_git_repo(runner: dict[str, Any], git_binary: str, cwd: str) -> bool:
    try:
        await _runner_exec(runner, git_binary, ["rev-parse", "--show-toplevel"], cwd)
        return True
    except Exception:
        return False

async def _has_git_upstream(runner: dict[str, Any], git_binary: str, cwd: str) -> bool:
    try:
        await _runner_exec(runner, git_binary, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
        return True
    except Exception:
        return False

async def _git_is_dirty(runner: dict[str, Any], git_binary: str, cwd: str) -> bool:
    porcelain = await _runner_exec(runner, git_binary, ["status", "--porcelain"], cwd)
    return bool(str(porcelain).strip())

def _read_pyproject_version(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "0.1.0"
    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.1.0"

def _read_optional_pyproject_version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None
