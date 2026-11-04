"""Child process that runs tinyagent on one job (JSON on stdin) and prints a JSON result line."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    job = json.loads(sys.stdin.read())
    try:
        from tinyagent import DEFAULT_MODEL, run_agent
    except ImportError:
        print(json.dumps({"error": "tinyagent is not installed: pip install 'agentcheck[tinyagent]'"}))
        return 2
    model = job.get("model") or DEFAULT_MODEL
    try:
        res = run_agent(
            job["prompt"], Path(job["workdir"]), model=model, effort=job.get("effort", "medium"),
            max_steps=job.get("max_steps", 30), confirm=None,
            on_event=lambda kind, text: print(f"[{kind}] {text}", file=sys.stderr),
        )
    except Exception as e:  # API errors, auth, etc.
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps({
        "stop": res.stop, "steps": res.steps, "cost_usd": res.usage.cost(model),
        "input_tokens": res.usage.input_tokens + res.usage.cache_read_tokens,
        "output_tokens": res.usage.output_tokens,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
