#!/usr/bin/env bash
# Run the agentcheck benchmark on a real open-source repo.
#   bash bench/run-benchmark.sh                 # Claude Code: no CLAUDE.md vs with one
#   CONFIGS=no-claude-md,with-claude-md,tinyagent bash bench/run-benchmark.sh
# Needs: git, python3 >= 3.10, Claude Code (`claude`) logged in. tinyagent also needs ANTHROPIC_API_KEY.
# Default is the cheap run: Haiku, 8 tasks x 2 configs x 1 repeat = 16 runs, well under $1 at API prices.
# With a Claude Pro/Max login, Claude Code runs count against your plan's usage instead of billing per token.
# Free run on a local model (LM Studio server running, aider installed: pip install aider-chat):
#   CONFIGS=local-no-claude-md,local-with-claude-md LOCAL_MODEL=qwen2.5-coder-14b-instruct bash bench/run-benchmark.sh
# Bigger run for the writeup: MODEL=claude-sonnet-5-5 TASKS=15 REPEATS=2 bash bench/run-benchmark.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"
WORK="${WORK:-$HOME/agentcheck-bench}"
TASKS="${TASKS:-8}"
MODEL="${MODEL:-claude-haiku-5-5}"
REPEATS="${REPEATS:-1}"
CONFIGS="${CONFIGS:-no-claude-md,with-claude-md}"

mkdir -p "$WORK" && cd "$WORK"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e "$SRC[tinyagent]" pytest
if [[ "$CONFIGS" == *local* ]]; then
  command -v aider >/dev/null || pip install -q aider-chat
  curl -sf http://localhost:1234/v1/models >/dev/null || { echo "LM Studio server isn't running on :1234 (Developer tab > Start Server)"; exit 1; }
  export LOCAL_MODEL="${LOCAL_MODEL:-qwen2.5-coder-14b-instruct}"
fi

[ -d more-itertools ] || git clone -q https://github.com/more-itertools/more-itertools
cd more-itertools
mkdir -p .agentcheck
cp "$HERE/more-itertools/CLAUDE.md" .agentcheck/CLAUDE.md
sed "s/claude-sonnet-5-5/$MODEL/" "$HERE/more-itertools/agentcheck.yaml" > agentcheck.yaml

echo "== 1. mining tasks"
[ -s .agentcheck/tasks.jsonl ] || agentcheck mine --limit "$TASKS" --since 2019-01-01
agentcheck tasks

echo "== 2. sanity check (should be 0% vs 100%)"
agentcheck run --configs noop,oracle --repeats 1

echo "== 3. the real run: $CONFIGS on $MODEL, $REPEATS repeat(s)"
agentcheck run --configs "$CONFIGS" --repeats "$REPEATS"

echo "Done. Reports are in $WORK/more-itertools/.agentcheck/runs/*/report.md"
