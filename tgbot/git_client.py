from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .security import RepoRef, redact_secrets


class GitError(RuntimeError):
    pass


async def _run(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 600,
    env: dict | None = None,
) -> tuple[int, str]:
    merged = os.environ.copy()
    merged["GIT_TERMINAL_PROMPT"] = "0"
    merged["GIT_ASKPASS"] = "echo"
    if env:
        merged.update(env)

    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=merged,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise GitError(f"Command timed out after {timeout}s: {' '.join(args[:2])}") from exc
    return process.returncode or 0, output.decode("utf-8", errors="ignore")


def _authenticated_url(repo: RepoRef, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo.full_name}.git"


async def clone_repo(
    repo: RepoRef,
    dest: Path,
    branch: str | None = None,
    token: str | None = None,
    timeout: int = 600,
) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = _authenticated_url(repo, token) if token else repo.clone_url

    args = ["git", "clone", "--depth", "1", "--single-branch"]
    if branch:
        args += ["--branch", branch]
    args += [url, str(dest)]

    code, output = await _run(args, timeout=timeout)
    if code != 0:
        raise GitError(f"git clone failed:\n{redact_secrets(output.strip())[-2000:]}")

    code, output = await _run(["git", "rev-parse", "HEAD"], cwd=dest, timeout=60)
    commit_sha = output.strip().splitlines()[0] if code == 0 and output.strip() else ""

    code, output = await _run(["git", "remote", "set-url", "origin", repo.clone_url], cwd=dest, timeout=60)
    return commit_sha


async def get_default_branch(repo: RepoRef, token: str | None = None, timeout: int = 60) -> str | None:
    env = None
    if token:
        env = {"GIT_ASKPASS": "echo"}
    code, output = await _run(
        ["git", "ls-remote", "--symref", repo.clone_url, "HEAD"],
        timeout=timeout,
        env=env,
    )
    if code != 0:
        return None
    for line in output.splitlines():
        if line.startswith("ref:") and line.endswith("HEAD"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1].replace("refs/heads/", "")
    return None
