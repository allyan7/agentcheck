"""Offline tests: build a tiny git repo with a real bug-fix history and run the whole pipeline on it."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentcheck import cli
from agentcheck.config import load
from agentcheck.mine import Candidate, is_test_file, iter_candidates, load_tasks, mine, select
from agentcheck.report import compare, gate, markdown, sign_test, summarize
from agentcheck.runner import RunRecord, run_all

PY = sys.executable
TEST_CMD = f"{PY} -m pytest -q -p no:cacheprovider {{tests}}"


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def commit(repo: Path, msg: str, files: dict[str, str]) -> str:
    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh(repo, "add", "-A")
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return sh(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    sh(r, "init", "-q")
    commit(r, "Initial calculator", {
        "calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
        "tests/test_calc.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        "pyproject.toml": "[project]\nname='calc'\nversion='0'\n",
    })
    commit(r, "Fix add returning the difference instead of the sum", {
        "calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        "tests/test_calc.py": "from calc import add, mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    })
    commit(r, "Update readme", {"README.md": "calc\n"})
    # A "fix" whose test already passes before the change: verification must drop it.
    commit(r, "Fix mul docstring and add a test", {
        "calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    \"\"\"Multiply.\"\"\"\n    return a * b\n",
        "tests/test_calc.py": "from calc import add, mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_mul_zero():\n    assert mul(0, 9) == 0\n",
    })
    (r / "AGENTS.md").write_text("Always run the tests.\n")
    return r


def write_config(repo: Path, configs: dict) -> Path:
    import yaml
    cfg = {"test_cmd": TEST_CMD, "tasks": ".agentcheck/tasks.jsonl", "jobs": 2, "timeout": 60, "configs": configs}
    p = repo / "agentcheck.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return p


# --- mining ---------------------------------------------------------------------------------------

def test_is_test_file():
    for p in ["tests/test_x.py", "pkg/tests/util.py", "test_a.py", "a_test.go", "src/x.test.ts", "web/__tests__/a.js", "spec/a_spec.rb"]:
        assert is_test_file(p), p
    for p in ["calc.py", "src/testing_utils_impl.go", "contest.py", "src/attest.ts"]:
        assert not is_test_file(p), p


def test_select_rules():
    c = Candidate("a" * 40, "b" * 40, "Fix crash on empty input", "", [("lib.py", 3, 1), ("tests/test_lib.py", 5, 0)])
    t = select(c, max_files=5, max_lines=200, require_fix_words=True)
    assert t and t.test_files == ["tests/test_lib.py"] and t.source_files == ["lib.py"] and t.lines_changed == 4
    no_test = Candidate("a" * 40, "b" * 40, "Fix crash on empty input", "", [("lib.py", 3, 1)])
    assert select(no_test, 5, 200, True) is None
    no_fix_word = Candidate("a" * 40, "b" * 40, "Add new parser module", "", [("lib.py", 3, 1), ("tests/test_lib.py", 5, 0)])
    assert select(no_fix_word, 5, 200, True) is None
    assert select(no_fix_word, 5, 200, False) is not None
    too_big = Candidate("a" * 40, "b" * 40, "Fix everything at once", "", [("lib.py", 500, 10), ("tests/test_lib.py", 5, 0)])
    assert select(too_big, 5, 200, True) is None


def test_iter_candidates(repo):
    subjects = [c.subject for c in iter_candidates(repo)]
    assert subjects == ["Fix mul docstring and add a test", "Update readme", "Fix add returning the difference instead of the sum"]


def test_mine_keeps_only_verified_fix(repo):
    tasks = mine(repo, TEST_CMD, log=lambda s: None)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.prompt.startswith("Fix add returning")
    assert t.verified and t.test_files == ["tests/test_calc.py"] and t.source_files == ["calc.py"]


def test_mine_no_verify_keeps_both(repo):
    tasks = mine(repo, TEST_CMD, do_verify=False, log=lambda s: None)
    assert len(tasks) == 2 and not any(t.verified for t in tasks)


# --- running --------------------------------------------------------------------------------------

def _mine_to_config(repo, configs):
    p = write_config(repo, configs)
    assert cli.main(["-c", str(p), "mine"]) == 0
    return p


def test_oracle_passes_noop_fails(repo):
    p = _mine_to_config(repo, {"noop": {"agent": "noop"}, "oracle": {"agent": "oracle"}})
    s = load(p)
    out, recs = run_all(s, load_tasks(s.tasks), log=lambda s: None)
    by = {r.config: r for r in recs}
    assert by["oracle"].passed and not by["noop"].passed
    assert (out / "results.jsonl").exists() and (out / "diffs" / "oracle").is_dir()
    md = markdown(recs, ["noop", "oracle"])
    assert "| **oracle** | 1/1 | 100%" in md and "| **noop** | 0/1 | 0%" in md


def test_cli_gate_fails_on_regression(repo, capsys):
    p = _mine_to_config(repo, {"oracle": {"agent": "oracle"}, "noop": {"agent": "noop"}})
    assert cli.main(["-c", str(p), "run", "--fail-on-regression"]) == 1
    assert "FAIL: noop pass rate 0%" in capsys.readouterr().out
    assert cli.main(["-c", str(p), "run", "--configs", "noop,oracle", "--fail-on-regression"]) == 0


def test_workspace_has_no_future_history_and_gets_config_files(repo):
    # The command agent dumps what it can see; the fix commit must not be reachable.
    probe = "git log --all --oneline | wc -l > ../seen.txt; ls >> ../seen.txt; cat AGENTS.md >> ../seen.txt 2>/dev/null; true"
    p = _mine_to_config(repo, {"probe": {"agent": "command", "cmd": probe, "files": {"AGENTS.md": {"text": "be careful"}}}})
    s = load(p)
    s.jobs = 1

    seen = {}
    import agentcheck.runner as runner
    orig = runner.shutil.rmtree

    def keep(path, *a, **k):
        f = Path(path) / "seen.txt"
        if f.exists():
            seen["text"] = f.read_text()
        return orig(path, *a, **k)

    runner.shutil.rmtree = keep
    try:
        run_all(s, load_tasks(s.tasks), log=lambda s: None)
    finally:
        runner.shutil.rmtree = orig
    lines = seen["text"].split()
    assert lines[0] == "1"  # one commit: the snapshot, nothing from the real history
    assert "be careful" in seen["text"]


def test_agent_cannot_pass_by_editing_tests(repo):
    # Cheater replaces the test file with a passing stub and doesn't touch calc.py.
    cheat = "printf 'def test_add():\\n    assert True\\n' > tests/test_calc.py; printf 'import pytest\\n' > conftest.py"
    p = _mine_to_config(repo, {"cheat": {"agent": "command", "cmd": cheat}})
    s = load(p)
    _, recs = run_all(s, load_tasks(s.tasks), log=lambda s: None)
    assert not recs[0].passed
    assert set(recs[0].tests_reverted) == {"tests/test_calc.py", "conftest.py"}


def test_command_agent_fix_and_cost_parsing(repo):
    fix = "sed -i 's/a - b/a + b/' calc.py; echo '{\"cost_usd\": 0.25, \"input_tokens\": 100, \"output_tokens\": 7}'"
    p = _mine_to_config(repo, {"fixer": {"agent": "command", "cmd": fix}})
    s = load(p)
    _, recs = run_all(s, load_tasks(s.tasks), log=lambda s: None)
    r = recs[0]
    assert r.passed and r.cost_usd == 0.25 and r.input_tokens == 100 and r.lines_changed == 2


def test_claude_code_adapter_parses_json(repo, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nsed -i 's/a - b/a + b/' calc.py\n"
                    "echo '{\"type\":\"result\",\"is_error\":false,\"total_cost_usd\":0.12,\"num_turns\":4,"
                    "\"usage\":{\"input_tokens\":10,\"cache_read_input_tokens\":90,\"output_tokens\":5}}'\n")
    fake.chmod(0o755)
    p = _mine_to_config(repo, {"cc": {"agent": "claude-code", "bin": str(fake), "model": "claude-sonnet-5-5"}})
    s = load(p)
    _, recs = run_all(s, load_tasks(s.tasks), log=lambda s: None)
    r = recs[0]
    assert r.passed and r.cost_usd == 0.12 and r.turns == 4 and r.input_tokens == 100


def test_codex_adapter_prices_tokens(repo, tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\nsed -i 's/a - b/a + b/' calc.py\n"
                    "echo '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":1000000,\"cached_input_tokens\":0,\"output_tokens\":0}}'\n")
    fake.chmod(0o755)
    p = _mine_to_config(repo, {"cx": {"agent": "codex", "bin": str(fake), "price": [1.5, 6.0]}})
    s = load(p)
    _, recs = run_all(s, load_tasks(s.tasks), log=lambda s: None)
    assert recs[0].passed and recs[0].cost_usd == pytest.approx(1.5)


def test_tinyagent_missing_is_reported(repo, monkeypatch):
    p = _mine_to_config(repo, {"ta": {"agent": "tinyagent"}})
    s = load(p)
    monkeypatch.setenv("PYTHONPATH", "")
    _, recs = run_all(s, load_tasks(s.tasks), log=lambda s: None)
    r = recs[0]
    assert not r.passed
    try:
        import tinyagent  # noqa: F401
    except ImportError:
        assert "tinyagent is not installed" in r.error


# --- reporting ------------------------------------------------------------------------------------

def rec(task, config, passed, cost=0.1, rep=0):
    return RunRecord(task, config, rep, passed, True, "", cost, 0, 0, None, 10.0, 0, "", [])


def test_sign_test():
    assert sign_test(0, 0) == 1.0
    assert sign_test(5, 0) == pytest.approx(0.0625)
    assert sign_test(8, 0) < 0.01
    assert sign_test(3, 3) == 1.0


def test_compare_and_report_with_repeats():
    recs = [rec("t1", "A", False), rec("t1", "B", True), rec("t2", "A", True), rec("t2", "B", True),
            rec("t3", "A", True, rep=0), rec("t3", "A", False, rep=1), rec("t3", "B", True, rep=0), rec("t3", "B", True, rep=1),
            rec("t1", "A", False, rep=1), rec("t1", "B", True, rep=1), rec("t2", "A", True, rep=1), rec("t2", "B", False, cost=0.3, rep=1)]
    c = compare(recs, "A", "B")
    assert c.gained == ["t1", "t3"] and c.lost == ["t2"]
    s = {x.config: x for x in summarize(recs, ["A", "B"])}
    assert s["A"].passes == 3 and s["B"].passes == 5 and s["A"].tasks_solved == 1.5
    md = markdown(recs, ["A", "B"])
    assert "**B vs A:**" in md and "`t1`" in md and "50%" in md
    assert gate(recs, ["A", "B"]) == []
    assert gate(recs, ["B", "A"]) and gate(recs, ["A", "B"], min_pass_rate=0.9)


def test_unknown_cost_shows_na():
    md = markdown([rec("t1", "A", True, cost=None)])
    assert "n/a" in md


def test_init_writes_template(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    cfg = tmp_path / "agentcheck.yaml"
    assert cli.main(["-c", str(cfg), "init"]) == 0
    s = load(cfg)
    assert "pytest" in s.test_cmd and list(s.configs) == ["baseline", "candidate"]
    assert cli.main(["-c", str(cfg), "init"]) == 1


def test_demo_repo_mines_six_verified_tasks(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("demo_repo", Path(__file__).parent.parent / "examples" / "demo_repo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    demo.main(str(tmp_path / "demo"))
    tasks = mine(tmp_path / "demo", TEST_CMD, log=lambda s: None)
    assert len(tasks) == 6 and all(t.verified for t in tasks)
