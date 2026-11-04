#!/usr/bin/env bash
# Run the agentcheck benchmark on a real open-source repo.
#   bash bench/run-benchmark.sh                 # Claude Code: no CLAUDE.md vs with one
#   CONFIGS=no-claude-md,with-claude-md,tinyagent bash bench/run-benchmark.sh
# Needs: git, python3 >= 3.10, Claude Code (`claude`) logged in. tinyagent also needs ANTHROPIC_API_KEY.
# Rough cost with Sonnet: 15 tasks x 2 configs x 2 repeats = 60 runs, about $10 to $30.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"
WORK="${WORK:-$HOME/agentcheck-bench}"
TASKS="${TASKS:-15}"
CONFIGS="${CONFIGS:-no-claude-md,with-claude-md}"

mkdir -p "$WORK" && cd "$WORK"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e "$SRC[tinyagent]" pytest

[ -d more-itertools ] || git clone -q https://github.com/more-itertools/more-itertools
cd more-itertools
mkdir -p .agentcheck
cp "$HERE/more-itertools/CLAUDE.md" .agentcheck/CLAUDE.md
cp "$HERE/more-itertools/agentcheck.yaml" agentcheck.yaml

echo "== 1. mining tasks"
[ -s .agentcheck/tasks.jsonl ] || agentcheck mine --limit "$TASKS" --since 2019-01-01
agentcheck tasks

echo "== 2. sanity check (should be 0% vs 100%)"
agentcheck run --configs noop,oracle --repeats 1

echo "== 3. the real run: $CONFIGS"
agentcheck run --configs "$CONFIGS"

echo "Done. Reports are in $WORK/more-itertools/.agentcheck/runs/*/report.md"
