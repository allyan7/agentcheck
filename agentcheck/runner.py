"""Run every (task, config, repeat) in its own fresh workspace and grade it with the hidden tests."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import agents
from .config import Settings, apply_files
from .git import export_tree, git, init_snapshot
from .mine import Task, install_hidden_tests, is_test_file, present_tests
from .tests_exec import run_tests, sh


@dataclass
class RunRecord:
    task: str
    config: str
    repeat: int
    passed: bool
    agent_ok: bool
    error: str
    cost_usd: float | None
    input_tokens: int
    output_tokens: int
    turns: int | None
    agent_seconds: float
    lines_changed: int
    test_tail: str
    tests_reverted: list = None
    title: str = ""


PROTECTED = ("conftest.py", "pytest.ini", "tox.ini", "jest.config.js", "jest.config.ts", "vitest.config.ts", "vitest.config.js")


def protect_tests(work: Path, diff: str) -> list[str]:
    """Undo any agent edits to test files and test config before grading, so an agent
    can't pass by deleting, skipping or rewriting tests."""
    changed = [l[6:] for l in diff.splitlines() if l.startswith("+++ b/") or l.startswith("--- a/")]
    reverted = []
    for path in dict.fromkeys(changed):
        if not (is_test_file(path) or path.rsplit("/", 1)[-1] in PROTECTED):
            continue
        if git(work, "ls-tree", "HEAD", "--", path, check=False).strip():
            git(work, "checkout", "HEAD", "--", path, check=False)
        else:
            (work / path).unlink(missing_ok=True)
        reverted.append(path)
    return reverted


def run_one(task: Task, name: str, cfg: dict, rep: int, s: Settings, out: Path) -> RunRecord:
    tmp = Path(tempfile.mkdtemp(prefix=f"agentcheck-{task.id}-"))
    work = tmp / "repo"
    try:
        export_tree(s.root, task.base, work)
        apply_files(cfg, work, s.root)
        init_snapshot(work)  # a one-commit repo: no future history for the agent to peek at
        if s.setup:
            code, log = sh(s.setup, work, s.test_timeout)
            if code != 0:
                return RunRecord(task.id, name, rep, False, False, "setup failed", None, 0, 0, None, 0.0, 0, log[-2000:])
        prompt = s.prompt_template.replace("{prompt}", task.prompt)
        res = agents.run_agent(prompt, work, cfg, s.timeout, s.root, task)

        # Save what the agent changed, then grade with tests it never saw.
        git(work, "add", "-A", check=False)
        diff = git(work, "diff", "--cached", "--", ".", check=False)
        diff_path = out / "diffs" / name / f"{task.id}-{rep}.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff)
        if res.log:
            (out / "logs" / name).mkdir(parents=True, exist_ok=True)
            (out / "logs" / name / f"{task.id}-{rep}.log").write_text(res.log)
        changed = sum(1 for l in diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))

        reverted = protect_tests(work, diff)
        install_hidden_tests(s.root, task, work)
        passed, test_out = run_tests(work, s.test_cmd, present_tests(task, work), None, s.test_timeout)
        return RunRecord(task.id, name, rep, passed, res.ok, res.error, res.cost_usd, res.input_tokens,
                         res.output_tokens, res.turns, res.seconds, changed, test_out[-2000:], reverted, task.prompt.splitlines()[0][:80])
    except Exception as e:
        return RunRecord(task.id, name, rep, False, False, f"{type(e).__name__}: {e}", None, 0, 0, None, 0.0, 0, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_all(
    s: Settings,
    tasks: list[Task],
    config_names: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[Path, list[RunRecord]]:
    names = config_names or list(s.configs)
    for n in names:
        if n not in s.configs:
            raise ValueError(f"no config named {n!r} (have: {', '.join(s.configs)})")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = s.out_dir / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps({
        "configs": {n: s.configs[n] for n in names}, "tasks": [t.id for t in tasks],
        "repeats": s.repeats, "test_cmd": s.test_cmd, "started": stamp,
    }, indent=2))

    jobs = [(t, n, r) for r in range(s.repeats) for t in tasks for n in names]
    records: list[RunRecord] = []
    lock = threading.Lock()
    results_file = out / "results.jsonl"
    with ThreadPoolExecutor(max_workers=max(1, s.jobs)) as pool:
        futs = {pool.submit(run_one, t, n, s.configs[n], r, s, out): (t, n, r) for t, n, r in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            with lock:
                records.append(rec)
                with results_file.open("a") as f:
                    f.write(json.dumps(asdict(rec)) + "\n")
            mark = "PASS" if rec.passed else "fail"
            cost = f"${rec.cost_usd:.3f}" if rec.cost_usd is not None else "$?"
            note = f"  ({rec.error[:80]})" if rec.error else ""
            log(f"[{i}/{len(jobs)}] {mark} {rec.config:<12} {rec.task} {cost} {rec.agent_seconds:.0f}s{note}")
    return out, records


def load_records(run_dir: Path) -> list[RunRecord]:
    lines = (run_dir / "results.jsonl").read_text().splitlines()
    return [RunRecord(**json.loads(l)) for l in lines if l.strip()]
