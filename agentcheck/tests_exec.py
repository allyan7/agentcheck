"""Run a repo's test command against a set of test files."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def detect_test_cmd(repo: Path) -> str | None:
    if any((repo / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")):
        return "python -m pytest -q -x -p no:cacheprovider {tests}"
    if (repo / "package.json").exists():
        return "npm test -- {tests}"
    if (repo / "go.mod").exists():
        return "go test ./..."
    if (repo / "Cargo.toml").exists():
        return "cargo test"
    return None


def format_cmd(test_cmd: str, tests: list[str]) -> str:
    joined = " ".join(shlex.quote(t) for t in tests)
    return test_cmd.replace("{tests}", joined) if "{tests}" in test_cmd else test_cmd


def sh(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\n[timed out after {timeout}s]"


def run_tests(workdir: Path, test_cmd: str, tests: list[str], setup: str | None, timeout: int) -> tuple[bool, str]:
    log = ""
    if setup:
        code, out = sh(setup, workdir, timeout)
        log += out
        if code != 0:
            return False, log + f"\n[setup failed with exit code {code}]"
    code, out = sh(format_cmd(test_cmd, tests), workdir, timeout)
    return code == 0, log + out
