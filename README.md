# agentcheck

**Unit tests for your coding agent setup.** Did your new CLAUDE.md actually help? agentcheck turns your repo's own bug-fix history into a test suite, runs your coding agent with setup A and setup B, and tells you which one solves more tasks, for how much money, in how long.

[![ci](https://github.com/allyan7/agentcheck/actions/workflows/ci.yml/badge.svg)](https://github.com/allyan7/agentcheck/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-blue)

```bash
pip install git+https://github.com/allyan7/agentcheck
cd your-repo
agentcheck init        # writes agentcheck.yaml
agentcheck mine        # finds past bug fixes that came with a test
agentcheck run         # baseline vs candidate, then prints the report
```

```
| config     | solved | pass rate | total cost | cost per solve | median time | agent errors |
|------------|--------|-----------|------------|----------------|-------------|--------------|
| baseline   | 9/15   | 60%       | $1.84      | $0.20          | 71s         | 0            |
| candidate  | 12/15  | 80%       | $1.52      | $0.13          | 58s         | 0            |

candidate vs baseline: +20% pass rate, p=0.25 (could be noise), cost -17%
- solves more often: `a91f3c02de` Fix timezone drift in scheduler, ...
```
_(illustrative output; run it on your repo for real numbers)_

## Why

Everyone tweaks CLAUDE.md, AGENTS.md, system prompts, skills and MCP setups, and nobody measures whether the change helped. Public benchmarks don't answer it: they're not your codebase, your conventions or your kind of bug, and the big ones are in every model's training data.

Your git history already has the answer key. Every commit that fixed a bug and added a test is a task with a hidden check: roll the code back to before the fix, hand the agent the commit message, then run the test from the fix.

## How it works

```
 git log ──► commits that change code AND tests ──► verify: test fails before, passes after ──► tasks.jsonl
                                                                                                    │
            ┌────────────────────────────── for each task × config × repeat ◄───────────────────────┘
            ▼
  snapshot of the parent commit (no history) ─► overlay config files (CLAUDE.md, ...) ─► run agent
            │
            ▼
  save diff ─► undo agent edits to tests ─► drop in the fix commit's tests ─► run them ─► pass / fail, $, time
```

The rules that keep the score honest:
- **No peeking at the future.** The workspace is a `git archive` of the parent commit turned into a fresh one-commit repo. `git log --all` shows nothing, so the agent can't find the real fix.
- **Hidden tests.** The fix commit's test files are copied in only after the agent finishes.
- **No cheating by editing tests.** Agent changes to test files, `conftest.py`, `pytest.ini` and similar are reverted before grading (and listed in the results).
- **Every task is verified.** `mine` keeps a commit only if its tests fail on the old code and pass on the fixed code. Tests that pass either way don't test anything.
- **Sanity agents.** `oracle` applies the real fix and should score 100%; `noop` does nothing and should score 0%. If not, a task is broken.
- **Paired stats.** Configs run on the same tasks, so the report counts the tasks where they disagree and gives an exact sign-test p-value. Use `repeats: 3` when you want to trust a small difference.

## Agents

| `agent:` | What runs | Cost comes from | Reads CLAUDE.md / AGENTS.md |
|---|---|---|---|
| `claude-code` | `claude -p --output-format json` | Claude Code's own `total_cost_usd` | yes, natively |
| `codex` | `codex exec --json` | token counts × `price` | AGENTS.md natively |
| `tinyagent` | [tinyagent](https://github.com/allyan7/tinyagent), a 300-line agent (`pip install 'agentcheck[tinyagent]'`) | token counts × model price | added to the prompt |
| `command` | any CLI: `cmd: "aider --yes --message-file {prompt_file}"` | a JSON last line with `cost_usd`, if printed | up to the agent |
| `oracle` / `noop` | the real fix / nothing | free | n/a |

## agentcheck.yaml

```yaml
test_cmd: "python -m pytest -q {tests}"   # {tests} = the task's hidden test files
setup: "pip install -q -e ."              # optional, runs in each fresh workspace before the agent
tasks: .agentcheck/tasks.jsonl
timeout: 900          # seconds per agent run
repeats: 1
jobs: 4               # parallel runs

configs:              # the first one is the baseline
  baseline:
    agent: claude-code
    model: claude-sonnet-5-5
    files:
      CLAUDE.md: null                     # null = delete: test "no CLAUDE.md at all"
  candidate:
    agent: claude-code
    model: claude-sonnet-5-5
    max_turns: 40
    system_prompt_append: "Run the tests before you finish."
    files:
      CLAUDE.md: docs/new-CLAUDE.md       # copied from your working tree into each workspace
      .claude/settings.json: {text: '{"permissions": {"allow": ["Bash"]}}'}
```

Things you can compare: two CLAUDE.md files, CLAUDE.md vs none, two models, effort levels, Claude Code vs Codex vs tinyagent, an MCP server on vs off (via `.mcp.json` in `files`), or a skill on vs off.

## Commands

```bash
agentcheck mine --limit 20 --since 2025-01-01     # --max-lines 200 --max-files 5 --any-commit --no-verify
agentcheck tasks                                   # list what was mined
agentcheck run --configs baseline,candidate        # subset; first is the baseline
agentcheck run --only a91f3c02de --repeats 3
agentcheck run --fail-on-regression --tolerance 0.05 --min-pass-rate 0.5   # CI gate
agentcheck report .agentcheck/runs/20261104-150102 # re-print a past run
```

Each run folder keeps `results.jsonl`, `report.md`, every agent's diff (`diffs/<config>/<task>.diff`) and logs.

## In CI

Fail a PR that makes your agent worse, the same way you fail one that breaks a test. Copy [examples/agentcheck-pr.yml](examples/agentcheck-pr.yml) into `.github/workflows/` and [examples/agentcheck.yaml](examples/agentcheck.yaml) into your repo root. It compares main's CLAUDE.md with the PR's and posts the table to the job summary. Commit `.agentcheck/tasks.jsonl` so CI doesn't re-mine every time.

```yaml
- uses: allyan7/agentcheck@v0
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  with:
    args: --fail-on-regression --tolerance 0.05
```

## Try it in 30 seconds

```bash
python examples/demo_repo.py /tmp/demo && cd /tmp/demo   # a tiny repo with 6 real-looking bug fixes
agentcheck mine                                          # 6 tasks, all verified
agentcheck run --configs noop,oracle                     # 0% vs 100%: the harness works
agentcheck run --configs no-claude-md,with-claude-md     # the real thing (needs Claude Code)
```

## Limits

- Tasks are only as good as your commit messages. "fix" with no detail makes a hard task; agentcheck skips very short subjects, but read `agentcheck tasks` before you trust a score.
- Your test command has to work on old commits. If dependencies changed a lot, use `--since` to stay in a range that installs.
- Workspaces are temp folders, not containers. Agents run with permissions skipped, so run agentcheck in CI or a VM if that worries you.
- Agents are noisy. One run per task can flip on luck; use `repeats` and look at the p-value before you announce a winner.

## Development

```bash
pip install -e ".[dev]"
pytest -q        # offline tests, no API key: builds a git repo with real fix history and runs the pipeline
```

## License

MIT
