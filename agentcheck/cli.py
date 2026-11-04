"""agentcheck: unit tests for your coding agent setup, mined from your git history."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import TEMPLATE, load
from .mine import load_tasks, mine, save_tasks
from .report import gate, markdown
from .runner import load_records, run_all
from .tests_exec import detect_test_cmd


def cmd_init(a) -> int:
    path = Path(a.config)
    if path.exists() and not a.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    test_cmd = detect_test_cmd(path.parent.resolve()) or "make test"
    path.write_text(TEMPLATE.format(test_cmd=test_cmd))
    print(f"wrote {path}. Next: agentcheck mine")
    return 0


def cmd_mine(a) -> int:
    s_path = Path(a.config)
    if s_path.exists():
        s = load(s_path)
        repo, test_cmd, setup, out = s.root, a.test_cmd or s.test_cmd, s.setup, s.tasks
    else:
        repo = Path(a.repo).resolve()
        test_cmd = a.test_cmd or detect_test_cmd(repo)
        setup, out = a.setup, repo / ".agentcheck/tasks.jsonl"
        if not test_cmd:
            print("couldn't detect a test command; pass --test-cmd or run `agentcheck init`")
            return 1
    if a.output:
        out = Path(a.output)
    print(f"mining {repo} (test command: {test_cmd})")
    tasks = mine(repo, test_cmd, setup=setup, limit=a.limit, rev=a.rev, max_commits=a.max_commits,
                 since=a.since, max_files=a.max_files, max_lines=a.max_lines,
                 require_fix_words=not a.any_commit, do_verify=not a.no_verify)
    save_tasks(tasks, out)
    print(f"\n{len(tasks)} tasks written to {out}")
    return 0 if tasks else 1


def cmd_tasks(a) -> int:
    s = load(Path(a.config))
    for t in load_tasks(s.tasks):
        first = t.prompt.splitlines()[0]
        print(f"{t.id}  {'✓' if t.verified else '?'}  {t.lines_changed:>4} lines  {first[:70]}")
    return 0


def _emit(md: str, run_dir: Path) -> None:
    (run_dir / "report.md").write_text(md)
    print("\n" + md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(md + "\n")


def _gate(a, records, order) -> int:
    if not (a.fail_on_regression or a.min_pass_rate is not None):
        return 0
    problems = gate(records, order, tolerance=a.tolerance if a.fail_on_regression else 1.0,
                    min_pass_rate=a.min_pass_rate)
    for p in problems:
        print(f"FAIL: {p}")
    return 1 if problems else 0


def cmd_run(a) -> int:
    s = load(Path(a.config))
    if a.repeats:
        s.repeats = a.repeats
    if a.jobs:
        s.jobs = a.jobs
    if not s.tasks.exists():
        print(f"no tasks at {s.tasks}; run `agentcheck mine` first")
        return 1
    tasks = load_tasks(s.tasks)
    if a.only:
        want = set(a.only.split(","))
        tasks = [t for t in tasks if t.id in want or any(t.id.startswith(w) for w in want)]
    if a.limit:
        tasks = tasks[: a.limit]
    names = a.configs.split(",") if a.configs else list(s.configs)
    print(f"{len(tasks)} tasks x {len(names)} configs x {s.repeats} repeats = {len(tasks) * len(names) * s.repeats} runs")
    run_dir, records = run_all(s, tasks, names)
    _emit(markdown(records, names), run_dir)
    print(f"full results in {run_dir}")
    return _gate(a, records, names)


def cmd_report(a) -> int:
    run_dir = Path(a.run_dir)
    records = load_records(run_dir)
    order = list(json.loads((run_dir / "meta.json").read_text())["configs"]) if (run_dir / "meta.json").exists() else None
    _emit(markdown(records, order), run_dir)
    return _gate(a, records, order or list(dict.fromkeys(r.config for r in records)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentcheck", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-c", "--config", default="agentcheck.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write a starter agentcheck.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("mine", help="find bug-fix commits with tests and turn them into tasks")
    p.add_argument("--repo", default=".")
    p.add_argument("--test-cmd", help="e.g. 'pytest -q {tests}'")
    p.add_argument("--setup", help="command to install deps in each workspace")
    p.add_argument("--limit", type=int, default=20, help="stop after this many tasks")
    p.add_argument("--rev", default="HEAD")
    p.add_argument("--since", help="only commits after this date, e.g. 2025-01-01")
    p.add_argument("--max-commits", type=int, default=2000)
    p.add_argument("--max-files", type=int, default=5, help="max source files changed by the fix")
    p.add_argument("--max-lines", type=int, default=200, help="max source lines changed by the fix")
    p.add_argument("--any-commit", action="store_true", help="don't require fix/bug words in the message")
    p.add_argument("--no-verify", action="store_true", help="skip the fail-before/pass-after check")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("tasks", help="list mined tasks")
    p.set_defaults(fn=cmd_tasks)

    for name, fn, helptext in (("run", cmd_run, "run every config on every task and report"),
                               ("report", cmd_report, "re-print the report for a past run")):
        p = sub.add_parser(name, help=helptext)
        if name == "run":
            p.add_argument("--configs", help="comma-separated subset of configs; the first is the baseline")
            p.add_argument("--only", help="comma-separated task ids")
            p.add_argument("--limit", type=int)
            p.add_argument("--repeats", type=int)
            p.add_argument("-j", "--jobs", type=int)
        else:
            p.add_argument("run_dir")
        p.add_argument("--fail-on-regression", action="store_true", help="exit 1 if a config does worse than the baseline")
        p.add_argument("--tolerance", type=float, default=0.0, help="allowed pass-rate drop, e.g. 0.05")
        p.add_argument("--min-pass-rate", type=float, help="exit 1 if a candidate is under this pass rate")
        p.set_defaults(fn=fn)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
