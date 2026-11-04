"""Small wrappers around the git CLI."""
from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, input: bytes | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, input=input)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout.decode(errors="replace")


def git_bytes(repo: Path, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def export_tree(repo: Path, rev: str, dest: Path) -> None:
    """Write the files of `rev` into `dest`, with no git history attached.

    The agent must never see the commit that fixed the bug, so we export a plain
    snapshot instead of checking out a worktree that shares the object store.
    """
    data = git_bytes(repo, "archive", "--format=tar", rev)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def show_file(repo: Path, rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def init_snapshot(dest: Path) -> None:
    """Turn an exported tree into a fresh one-commit repo, so agents that use git still work
    and we can diff what the agent changed."""
    env_args = ["-c", "user.name=agentcheck", "-c", "user.email=agentcheck@localhost", "-c", "commit.gpgsign=false"]
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, *env_args, "commit", "-q", "--allow-empty", "-m", "agentcheck: task start")
