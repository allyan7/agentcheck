"""Mine a repo's git history for tasks: commits that fixed something and added or changed a test.

For each candidate commit C with parent P:
  - the workspace is the tree at P (the bug is still there)
  - the prompt is C's commit message
  - the hidden check is C's version of the test files it touched, run with the repo's test command
A task is kept only if those tests FAIL at P and PASS at C (unless verification is skipped).
"""
from __future__ import annotations

import fnmatch
import json
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .git import export_tree, git, show_file
from .tests_exec import run_tests

TEST_PATTERNS = [
    "test_*.py", "*_test.py", "tests/*", "*/tests/*", "test/*", "*/test/*", "testing/*",
    "__tests__/*", "*/__tests__/*", "*.test.[jt]s", "*.test.[jt]sx", "*.spec.[jt]s", "*.spec.[jt]sx",
    "*_test.go", "*/src/test/*", "src/test/*", "spec/*", "*_spec.rb",
]
CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".swift", ".scala", ".ex", ".exs",
}
FIX_WORDS = re.compile(r"\b(fix(es|ed)?|bug|regression|crash|error|handle|correct|broken|issue|wrong)\b", re.I)


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in TEST_PATTERNS)


def is_code_file(path: str) -> bool:
    return Path(path).suffix in CODE_EXT


@dataclass
class Task:
    id: str
    commit: str
    base: str
    prompt: str
    test_files: list[str]
    source_files: list[str]
    lines_changed: int
    verified: bool = False
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**d)


@dataclass
class Candidate:
    commit: str
    parent: str
    subject: str
    body: str
    files: list[tuple[str, int, int]]  # (path, added, deleted)


def iter_candidates(repo: Path, rev: str = "HEAD", max_commits: int = 2000, since: str | None = None) -> Iterable[Candidate]:
    sep, end = "\x1f", "\x1e"
    args = ["log", rev, "--no-merges", f"-n{max_commits}", "--numstat", f"--format={end}%H{sep}%P{sep}%s{sep}%b{sep}"]
    if since:
        args.append(f"--since={since}")
    out = git(repo, *args)
    for chunk in out.split(end)[1:]:
        head, _, numstat = chunk.rpartition(sep)
        parts = head.split(sep)
        if len(parts) < 4:
            continue
        sha, parents, subject, body = parts[0], parts[1].split(), parts[2], parts[3]
        if len(parents) != 1:
            continue  # root commit
        files = []
        for line in numstat.strip().splitlines():
            cols = line.split("\t")
            if len(cols) == 3:
                a, d, p = cols
                if "=>" in p:  # renames make the base/fix mapping ambiguous; skip the file
                    continue
                files.append((p, int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0))
        yield Candidate(sha, parents[0], subject.strip(), body.strip(), files)


def select(c: Candidate, max_files: int, max_lines: int, require_fix_words: bool) -> Task | None:
    tests = [p for p, _, _ in c.files if is_test_file(p)]
    src = [p for p, _, _ in c.files if not is_test_file(p) and is_code_file(p)]
    if not tests or not src:
        return None
    src_lines = sum(a + d for p, a, d in c.files if p in src)
    if len(src) > max_files or src_lines > max_lines or src_lines == 0:
        return None
    message = (c.subject + "\n\n" + c.body).strip()
    if require_fix_words and not FIX_WORDS.search(message):
        return None
    if len(c.subject) < 8:
        return None
    return Task(
        id=c.commit[:10], commit=c.commit, base=c.parent, prompt=message,
        test_files=tests, source_files=src, lines_changed=src_lines,
    )


def install_hidden_tests(repo: Path, task: Task, workdir: Path) -> None:
    """Overwrite the workspace's test files with the versions from the fix commit."""
    for path in task.test_files:
        data = show_file(repo, task.commit, path)
        target = workdir / path
        if data is None:  # the fix commit deleted this test file
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def present_tests(task: Task, workdir: Path) -> list[str]:
    return [t for t in task.test_files if (workdir / t).exists()]


def verify(repo: Path, task: Task, test_cmd: str, setup: str | None, timeout: int) -> tuple[bool, str]:
    """Hidden tests must fail on the base tree and pass on the fix tree."""
    with tempfile.TemporaryDirectory(prefix="agentcheck-verify-") as tmp:
        base, fixed = Path(tmp) / "base", Path(tmp) / "fixed"
        export_tree(repo, task.base, base)
        install_hidden_tests(repo, task, base)
        export_tree(repo, task.commit, fixed)
        tests = present_tests(task, fixed)
        if not tests:
            return False, "fix commit has no test files left"
        ok_fixed, out_fixed = run_tests(fixed, test_cmd, tests, setup, timeout)
        if not ok_fixed:
            return False, "tests fail even with the fix applied (env problem or flaky):\n" + out_fixed[-1500:]
        ok_base, _ = run_tests(base, test_cmd, tests, setup, timeout)
        if ok_base:
            return False, "tests already pass before the fix, so they don't test it"
        return True, "ok"


def mine(
    repo: Path,
    test_cmd: str,
    setup: str | None = None,
    limit: int = 20,
    rev: str = "HEAD",
    max_commits: int = 2000,
    since: str | None = None,
    max_files: int = 5,
    max_lines: int = 200,
    require_fix_words: bool = True,
    do_verify: bool = True,
    timeout: int = 300,
    log: Callable[[str], None] = print,
) -> list[Task]:
    tasks: list[Task] = []
    for c in iter_candidates(repo, rev, max_commits, since):
        task = select(c, max_files, max_lines, require_fix_words)
        if task is None:
            continue
        if do_verify:
            ok, why = verify(repo, task, test_cmd, setup, timeout)
            first = why.splitlines()[0] if why else ""
            log(f"  {'keep' if ok else 'skip'} {task.id} {c.subject[:60]!r}" + ("" if ok else f" ({first})"))
            if not ok:
                continue
            task.verified = True
        else:
            log(f"  keep {task.id} {c.subject[:60]!r} (unverified)")
        tasks.append(task)
        if len(tasks) >= limit:
            break
    return tasks


def save_tasks(tasks: list[Task], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(t.to_json() + "\n" for t in tasks))


def load_tasks(path: Path) -> list[Task]:
    return [Task.from_dict(json.loads(l)) for l in path.read_text().splitlines() if l.strip()]


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
