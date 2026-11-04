"""agentcheck.yaml: what to test, how to run the tests, and which agent setups to compare."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PROMPT = """{prompt}

Make this change in the repository in the current directory. Tests will be run afterwards to check your work."""

TEMPLATE = """# agentcheck: compare two agent setups on tasks mined from this repo's history.
# Docs: https://github.com/allyan7/agentcheck

test_cmd: "{test_cmd}"   # {{tests}} becomes the hidden test files for each task
# setup: "pip install -e ."      # runs in each fresh workspace before the agent starts
tasks: .agentcheck/tasks.jsonl   # written by `agentcheck mine`
timeout: 900                     # seconds per agent run
repeats: 1                       # run each task N times per config (agents are noisy; 3 is better)
jobs: 2                          # runs in parallel

configs:
  # The first config is the baseline that others are compared against.
  baseline:
    agent: claude-code           # claude-code | codex | tinyagent | command | oracle | noop
    model: claude-sonnet-5-5
    files:
      CLAUDE.md: null            # null deletes the file: "no CLAUDE.md at all"
  candidate:
    agent: claude-code
    model: claude-sonnet-5-5
    files:
      CLAUDE.md: CLAUDE.md       # copy this file from your working tree into each workspace
"""


@dataclass
class Settings:
    root: Path
    test_cmd: str
    tasks: Path
    configs: dict[str, dict]
    setup: str | None = None
    timeout: int = 900
    test_timeout: int = 300
    repeats: int = 1
    jobs: int = 2
    prompt_template: str = DEFAULT_PROMPT
    out_dir: Path = field(default_factory=lambda: Path(".agentcheck/runs"))


def load(path: Path) -> Settings:
    data = yaml.safe_load(path.read_text()) or {}
    root = (path.parent / data.get("repo", ".")).resolve()
    if not data.get("configs"):
        raise ValueError(f"{path}: needs at least one entry under `configs`")
    if not data.get("test_cmd"):
        raise ValueError(f"{path}: `test_cmd` is required")
    for name, cfg in data["configs"].items():
        if not isinstance(cfg, dict):
            raise ValueError(f"{path}: config {name!r} must be a mapping")
    return Settings(
        root=root,
        test_cmd=data["test_cmd"],
        tasks=root / data.get("tasks", ".agentcheck/tasks.jsonl"),
        configs=data["configs"],
        setup=data.get("setup"),
        timeout=int(data.get("timeout", 900)),
        test_timeout=int(data.get("test_timeout", 300)),
        repeats=int(data.get("repeats", 1)),
        jobs=int(data.get("jobs", 2)),
        prompt_template=data.get("prompt_template", DEFAULT_PROMPT),
        out_dir=root / data.get("out_dir", ".agentcheck/runs"),
    )


def apply_files(cfg: dict, workdir: Path, root: Path) -> None:
    """Overlay a config's files onto a workspace. Values: a path in your working tree,
    null to delete the file, or {text: "..."} for inline content."""
    for dest, src in (cfg.get("files") or {}).items():
        target = workdir / dest
        if src is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(src, dict) and "text" in src:
            target.write_text(src["text"])
        else:
            source = (root / str(src)).resolve()
            if not source.is_file():
                raise FileNotFoundError(f"config file {src!r} not found (looked at {source})")
            target.write_bytes(source.read_bytes())
