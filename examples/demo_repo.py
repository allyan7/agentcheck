"""Build a small demo repo with a realistic bug-fix history, so you can try agentcheck in 30 seconds.

    python examples/demo_repo.py /tmp/demo && cd /tmp/demo
    agentcheck mine
    agentcheck run --configs noop,oracle     # sanity check: 0% vs 100%
    agentcheck run                           # the real comparison (needs claude or an API key)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CONFIG = """test_cmd: "python -m pytest -q -p no:cacheprovider {tests}"
tasks: .agentcheck/tasks.jsonl
timeout: 600
repeats: 1
jobs: 3

configs:
  no-claude-md:
    agent: claude-code
    model: claude-haiku-5-5
    files:
      CLAUDE.md: null
  with-claude-md:
    agent: claude-code
    model: claude-haiku-5-5
    files:
      CLAUDE.md: CLAUDE.md
  tinyagent:
    agent: tinyagent
    model: claude-haiku-5-5
  oracle:
    agent: oracle
  noop:
    agent: noop
"""

CLAUDE_MD = """# textkit

- Library code is in `textkit/`, tests in `tests/`. Run `python -m pytest -q` before you finish.
- Keep fixes minimal. Don't change public function signatures.
- Add or update a test for every bug you fix.
"""

V0 = {
    "pyproject.toml": "[project]\nname = 'textkit'\nversion = '0.1.0'\n",
    "textkit/__init__.py": "",
    "textkit/strings.py": '''def slugify(text):
    return text.lower().replace(" ", "-")


def truncate(text, n):
    """Cut text to n characters, adding '...' if it was cut."""
    if len(text) < n:
        return text
    return text[:n] + "..."


def title_case(text):
    return " ".join(w.capitalize() for w in text.split(" "))
''',
    "textkit/numbers.py": '''def mean(xs):
    return sum(xs) / len(xs)


def clamp(x, lo, hi):
    return max(lo, min(x, lo))


def percent(part, whole):
    return round(part / whole * 100)
''',
    "textkit/parse.py": '''def parse_kv(line):
    """Parse 'a=1, b=2' into {'a': '1', 'b': '2'}."""
    out = {}
    for pair in line.split(","):
        k, v = pair.split("=")
        out[k] = v
    return out


def parse_bool(s):
    return s.lower() in ("true", "yes")
''',
    "tests/test_strings.py": '''from textkit.strings import slugify, truncate


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_truncate_short():
    assert truncate("hi", 10) == "hi"
''',
    "tests/test_numbers.py": '''from textkit.numbers import mean


def test_mean():
    assert mean([1, 2, 3]) == 2
''',
    "tests/test_parse.py": '''from textkit.parse import parse_kv


def test_parse_kv_simple():
    assert parse_kv("a=1") == {"a": "1"}
''',
}

# (message, {path: (old, new)}, {test_path: appended_test})
FIXES = [
    ("Fix clamp ignoring the upper bound\n\nclamp(15, 0, 10) returned 0 because min() compared against lo.",
     {"textkit/numbers.py": ("max(lo, min(x, lo))", "max(lo, min(x, hi))")},
     {"tests/test_numbers.py": "\n\ndef test_clamp():\n    from textkit.numbers import clamp\n    assert clamp(15, 0, 10) == 10\n    assert clamp(-5, 0, 10) == 0\n    assert clamp(5, 0, 10) == 5\n"}),
    ("Fix truncate at exact length\n\ntruncate('hello', 5) added '...' even though nothing was cut.",
     {"textkit/strings.py": ("if len(text) < n:", "if len(text) <= n:")},
     {"tests/test_strings.py": "\n\ndef test_truncate_exact_length():\n    assert truncate('hello', 5) == 'hello'\n    assert truncate('hello!', 5) == 'hello...'\n"}),
    ("Handle spaces around keys and values in parse_kv\n\n'a = 1, b= 2' should parse the same as 'a=1,b=2'.",
     {"textkit/parse.py": ("        k, v = pair.split(\"=\")\n        out[k] = v", "        k, v = pair.split(\"=\", 1)\n        out[k.strip()] = v.strip()")},
     {"tests/test_parse.py": "\n\ndef test_parse_kv_spaces():\n    assert parse_kv('a = 1, b= 2') == {'a': '1', 'b': '2'}\n\n\ndef test_parse_kv_equals_in_value():\n    assert parse_kv('url=a=b') == {'url': 'a=b'}\n"}),
    ("Fix mean crashing on an empty list\n\nReturn 0.0 instead of raising ZeroDivisionError.",
     {"textkit/numbers.py": ("    return sum(xs) / len(xs)", "    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)")},
     {"tests/test_numbers.py": "\n\ndef test_mean_empty():\n    assert mean([]) == 0.0\n"}),
    ("Fix slugify keeping punctuation and repeated dashes\n\nslugify('Hello,  World!') should be 'hello-world'.",
     {"textkit/strings.py": ('def slugify(text):\n    return text.lower().replace(" ", "-")',
                            'import re\n\n\ndef slugify(text):\n    text = re.sub(r"[^a-z0-9]+", "-", text.lower())\n    return text.strip("-")')},
     {"tests/test_strings.py": "\n\ndef test_slugify_punctuation():\n    assert slugify('Hello,  World!') == 'hello-world'\n    assert slugify('  a--b  ') == 'a-b'\n"}),
    ("Fix parse_bool for '1' and 'on'",
     {"textkit/parse.py": ('return s.lower() in ("true", "yes")', 'return s.strip().lower() in ("true", "yes", "1", "on")')},
     {"tests/test_parse.py": "\n\ndef test_parse_bool():\n    from textkit.parse import parse_bool\n    assert parse_bool('1') and parse_bool(' On ') and not parse_bool('no')\n"}),
]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=demo", "-c", "user.email=demo@example.com", *args],
                   check=True, capture_output=True)


def main(dest: str) -> None:
    repo = Path(dest)
    if repo.exists() and any(repo.iterdir()):
        sys.exit(f"{repo} is not empty")
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    for path, text in V0.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Initial textkit")
    for msg, src, tests in FIXES:
        for path, (old, new) in src.items():
            p = repo / path
            text = p.read_text()
            assert old in text, (path, old)
            p.write_text(text.replace(old, new, 1))
        for path, extra in tests.items():
            (repo / path).write_text((repo / path).read_text() + extra)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)
    (repo / "CLAUDE.md").write_text(CLAUDE_MD)
    (repo / "agentcheck.yaml").write_text(CONFIG)
    (repo / ".gitignore").write_text(".agentcheck/runs/\n")
    print(f"demo repo with {len(FIXES)} bug fixes at {repo}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "agentcheck-demo")
