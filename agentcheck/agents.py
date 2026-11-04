"""Adapters that run one coding agent on one task in one workspace.

Every adapter returns an AgentResult. Cost comes from the agent when it reports it
(Claude Code, tinyagent) and from token counts times `price` otherwise.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .git import show_file

# USD per million tokens (input, output), used when an agent reports tokens but not dollars.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md")


@dataclass
class AgentResult:
    ok: bool = True  # the agent process finished (says nothing about the tests)
    error: str = ""
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int | None = None
    seconds: float = 0.0
    log: str = ""
    extra: dict = field(default_factory=dict)


def _price(cfg: dict, inp: int, out: int, cached: int = 0) -> float | None:
    p = cfg.get("price") or PRICES.get(cfg.get("model") or "")
    if not p:
        return None
    return (inp * p[0] + cached * p[0] * 0.1 + out * p[1]) / 1e6


def _run(cmd: list[str] | str, cwd: Path, timeout: int, env: dict | None = None, stdin: str | None = None) -> tuple[int, str, str, bool]:
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
            shell=isinstance(cmd, str), env={**os.environ, **(env or {})},
        )
        return r.returncode, r.stdout, r.stderr, False
    except subprocess.TimeoutExpired as e:
        dec = lambda b: b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        return 124, dec(e.stdout), dec(e.stderr), True
    except FileNotFoundError as e:
        return 127, "", str(e), False


def context_preamble(workdir: Path, cfg: dict) -> str:
    """For agents that don't read CLAUDE.md/AGENTS.md themselves, put the files in the prompt."""
    names = cfg.get("context_files", CONTEXT_FILES)
    parts = []
    for name in names:
        p = workdir / name
        if p.is_file():
            parts.append(f"<project_instructions file=\"{name}\">\n{p.read_text(errors='replace').strip()}\n</project_instructions>")
    return "\n\n".join(parts)


def _last_json(out: str) -> dict:
    """The whole output as JSON, or else its last line that parses as a JSON object."""
    for chunk in [out.strip(), *reversed(out.strip().splitlines())]:
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


# --- adapters -----------------------------------------------------------------------------------

def run_claude_code(prompt: str, workdir: Path, cfg: dict, timeout: int, **_) -> AgentResult:
    cmd = [cfg.get("bin", "claude"), "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
    # Load only this repo's settings so your personal ~/.claude config doesn't skew the comparison.
    cmd += ["--setting-sources", cfg.get("setting_sources", "project,local")]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if cfg.get("max_turns"):
        cmd += ["--max-turns", str(cfg["max_turns"])]
    if cfg.get("system_prompt_append"):
        cmd += ["--append-system-prompt", cfg["system_prompt_append"]]
    cmd += [str(a) for a in cfg.get("args", [])]
    code, out, err, timed_out = _run(cmd, workdir, timeout, cfg.get("env"))
    res = AgentResult(ok=code == 0 and not timed_out, log=(out + err)[-20000:])
    if timed_out:
        res.error = f"timed out after {timeout}s"
    data = _last_json(out)
    if data:
        res.cost_usd = data.get("total_cost_usd")
        res.turns = data.get("num_turns")
        u = data.get("usage") or {}
        res.input_tokens = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        res.output_tokens = u.get("output_tokens") or 0
        if data.get("is_error"):
            res.ok, res.error = False, res.error or str(data.get("subtype") or data.get("result", ""))[:300]
    elif code != 0 and not res.error:
        res.error = (err or out).strip()[-300:] or f"exit code {code}"
    return res


def run_codex(prompt: str, workdir: Path, cfg: dict, timeout: int, **_) -> AgentResult:
    cmd = [cfg.get("bin", "codex"), "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    cmd += [str(a) for a in cfg.get("args", [])] + [prompt]
    code, out, err, timed_out = _run(cmd, workdir, timeout, cfg.get("env"))
    res = AgentResult(ok=code == 0 and not timed_out, log=(out + err)[-20000:])
    inp = cached = outp = turns = 0
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = ev.get("usage") if isinstance(ev, dict) else None
        if isinstance(u, dict):
            inp += u.get("input_tokens") or 0
            cached += u.get("cached_input_tokens") or 0
            outp += u.get("output_tokens") or 0
            turns += 1
    res.input_tokens, res.output_tokens, res.turns = inp, outp, turns or None
    res.cost_usd = _price(cfg, max(inp - cached, 0), outp, cached)
    if timed_out:
        res.error = f"timed out after {timeout}s"
    elif code != 0:
        res.error = (err or out).strip()[-300:] or f"exit code {code}"
    return res


def run_tinyagent(prompt: str, workdir: Path, cfg: dict, timeout: int, **_) -> AgentResult:
    """tinyagent runs in a child process so a hung run can be killed by the timeout."""
    pre = context_preamble(workdir, cfg)
    full = f"{pre}\n\n{prompt}" if pre else prompt
    job = {"prompt": full, "workdir": str(workdir), "model": cfg.get("model"),
           "effort": cfg.get("effort", "medium"), "max_steps": cfg.get("max_steps", 30)}
    code, out, err, timed_out = _run([sys.executable, *(["-P"] if sys.version_info >= (3, 11) else []), "-m", "agentcheck._tinyagent_runner"], workdir, timeout,
                                     cfg.get("env"), stdin=json.dumps(job))
    res = AgentResult(ok=code == 0 and not timed_out, log=err[-20000:])
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        data = {}
    if data.get("error"):
        res.ok, res.error = False, data["error"]
    res.cost_usd = data.get("cost_usd")
    res.input_tokens = data.get("input_tokens", 0)
    res.output_tokens = data.get("output_tokens", 0)
    res.turns = data.get("steps")
    res.extra = {"stop": data.get("stop")}
    if timed_out:
        res.error = f"timed out after {timeout}s"
    elif code != 0 and not res.error:
        res.error = err.strip()[-300:] or f"exit code {code}"
    return res


def run_command(prompt: str, workdir: Path, cfg: dict, timeout: int, **_) -> AgentResult:
    """Any CLI agent. The command gets the prompt as {prompt} / {prompt_file} or $AGENTCHECK_PROMPT.
    If its last stdout line is JSON with cost_usd / input_tokens / output_tokens, those are recorded."""
    prompt_file = workdir.parent / f".agentcheck-prompt-{workdir.name}.txt"
    prompt_file.write_text(prompt)
    cmd = cfg["cmd"].replace("{prompt_file}", shlex.quote(str(prompt_file))).replace("{prompt}", shlex.quote(prompt))
    env = {"AGENTCHECK_PROMPT": prompt, "AGENTCHECK_PROMPT_FILE": str(prompt_file), **(cfg.get("env") or {})}
    code, out, err, timed_out = _run(cmd, workdir, timeout, env)
    prompt_file.unlink(missing_ok=True)
    res = AgentResult(ok=code == 0 and not timed_out, log=(out + err)[-20000:])
    res.cost_usd = _price(cfg, 0, 0)  # e.g. price: [0, 0] for a local model
    try:
        data = json.loads(out.strip().splitlines()[-1])
        if isinstance(data, dict):
            res.input_tokens = data.get("input_tokens", 0)
            res.output_tokens = data.get("output_tokens", 0)
            res.cost_usd = data.get("cost_usd", _price(cfg, res.input_tokens, res.output_tokens))
    except (json.JSONDecodeError, IndexError):
        pass
    if timed_out:
        res.error = f"timed out after {timeout}s"
    elif code != 0:
        res.error = (err or out).strip()[-300:] or f"exit code {code}"
    return res


def run_oracle(prompt: str, workdir: Path, cfg: dict, timeout: int, repo: Path, task, **_) -> AgentResult:
    """Applies the real fix. Use it to check the harness: oracle should score 100%."""
    for path in task.source_files:
        data = show_file(repo, task.commit, path)
        target = workdir / path
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return AgentResult(cost_usd=0.0)


def run_noop(prompt: str, workdir: Path, cfg: dict, timeout: int, **_) -> AgentResult:
    """Does nothing. Every task should fail; if one passes, the task is broken."""
    return AgentResult(cost_usd=0.0)


ADAPTERS = {
    "claude-code": run_claude_code,
    "codex": run_codex,
    "tinyagent": run_tinyagent,
    "command": run_command,
    "oracle": run_oracle,
    "noop": run_noop,
}


def run_agent(prompt: str, workdir: Path, cfg: dict, timeout: int, repo: Path, task) -> AgentResult:
    kind = cfg.get("agent", "tinyagent")
    if kind not in ADAPTERS:
        raise ValueError(f"unknown agent {kind!r}; pick one of {', '.join(ADAPTERS)}")
    t0 = time.monotonic()
    res = ADAPTERS[kind](prompt, workdir, cfg, timeout, repo=repo, task=task)
    res.seconds = round(time.monotonic() - t0, 2)
    return res
