"""Turn run records into a summary table, a head-to-head comparison, and a CI verdict."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from math import comb

from .runner import RunRecord


@dataclass
class Summary:
    config: str
    runs: int
    passes: int
    tasks: int
    tasks_solved: float  # mean over repeats, so 3 repeats can give 4.33
    cost: float | None
    median_seconds: float
    errors: int

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    @property
    def cost_per_solved(self) -> float | None:
        if self.cost is None or not self.passes:
            return None
        return self.cost / self.passes


def summarize(records: list[RunRecord], order: list[str] | None = None) -> list[Summary]:
    names = order or list(dict.fromkeys(r.config for r in records))
    out = []
    for n in names:
        rs = [r for r in records if r.config == n]
        if not rs:
            continue
        costs = [r.cost_usd for r in rs]
        repeats = len({r.repeat for r in rs}) or 1
        out.append(Summary(
            config=n,
            runs=len(rs),
            passes=sum(r.passed for r in rs),
            tasks=len({r.task for r in rs}),
            tasks_solved=sum(r.passed for r in rs) / repeats,
            cost=None if any(c is None for c in costs) else sum(costs),
            median_seconds=statistics.median(r.agent_seconds for r in rs),
            errors=sum(1 for r in rs if not r.agent_ok),
        ))
    return out


def task_pass_rates(records: list[RunRecord], config: str) -> dict[str, float]:
    by: dict[str, list[bool]] = {}
    for r in records:
        if r.config == config:
            by.setdefault(r.task, []).append(r.passed)
    return {t: sum(v) / len(v) for t, v in by.items()}


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test on tasks where the configs disagree. Small p = real difference."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    p = sum(comb(n, i) for i in range(k + 1)) / 2 ** n * 2
    return min(1.0, p)


@dataclass
class Comparison:
    baseline: str
    candidate: str
    gained: list[str]  # tasks the candidate solves more often
    lost: list[str]
    p_value: float
    delta_pass_rate: float
    cost_change: float | None  # fraction, e.g. -0.2 is 20% cheaper


def compare(records: list[RunRecord], baseline: str, candidate: str) -> Comparison:
    a, b = task_pass_rates(records, baseline), task_pass_rates(records, candidate)
    common = sorted(set(a) & set(b))
    gained = [t for t in common if b[t] > a[t]]
    lost = [t for t in common if b[t] < a[t]]
    sa, sb = summarize(records, [baseline, candidate])
    cost_change = None
    if sa.cost and sb.cost is not None:
        cost_change = sb.cost / sa.cost - 1
    return Comparison(baseline, candidate, gained, lost, sign_test(len(gained), len(lost)),
                      sb.pass_rate - sa.pass_rate, cost_change)


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:.2f}" if x >= 0.995 else f"${x:.3f}"


def _verdict(c: Comparison) -> str:
    n_disc = len(c.gained) + len(c.lost)
    if n_disc == 0:
        return "no difference on any task"
    sig = "likely real" if c.p_value < 0.05 else "could be noise"
    return f"{c.delta_pass_rate:+.0%} pass rate, p={c.p_value:.2f} ({sig})"


def markdown(records: list[RunRecord], order: list[str] | None = None, title: str = "agentcheck results") -> str:
    sums = summarize(records, order)
    if not sums:
        return f"## {title}\n\nNo runs."
    lines = [f"## {title}", ""]
    lines += ["| config | solved | pass rate | total cost | cost per solve | median time | agent errors |",
              "|---|---|---|---|---|---|---|"]
    for s in sums:
        solved = f"{s.tasks_solved:g}/{s.tasks}"
        lines.append(f"| **{s.config}** | {solved} | {s.pass_rate:.0%} | {_money(s.cost)} | "
                     f"{_money(s.cost_per_solved)} | {s.median_seconds:.0f}s | {s.errors} |")

    base = sums[0].config
    for s in sums[1:]:
        c = compare(records, base, s.config)
        lines += ["", f"**{s.config} vs {base}:** {_verdict(c)}"]
        if c.cost_change is not None:
            lines[-1] += f", cost {c.cost_change:+.0%}"
        if c.gained:
            lines.append(f"- solves more often: {', '.join('`' + t + '`' for t in c.gained)}")
        if c.lost:
            lines.append(f"- solves less often: {', '.join('`' + t + '`' for t in c.lost)}")

    names = [s.config for s in sums]
    rates = {n: task_pass_rates(records, n) for n in names}
    titles = {r.task: r.title for r in records if getattr(r, "title", "")}
    tasks = sorted({r.task for r in records})
    lines += ["", "<details><summary>Per task</summary>", "", "| task | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
    for t in tasks:
        cells = []
        for n in names:
            v = rates[n].get(t)
            cells.append("" if v is None else "✅" if v == 1 else "❌" if v == 0 else f"{v:.0%}")
        label = f"`{t}` {titles[t]}" if t in titles else f"`{t}`"
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def gate(records: list[RunRecord], order: list[str], tolerance: float = 0.0, min_pass_rate: float | None = None) -> list[str]:
    """Reasons to fail CI. Empty list means pass."""
    sums = summarize(records, order)
    problems = []
    if not sums:
        return ["no runs"]
    base = sums[0]
    for s in sums[1:]:
        if s.pass_rate < base.pass_rate - tolerance:
            problems.append(f"{s.config} pass rate {s.pass_rate:.0%} is below {base.config} {base.pass_rate:.0%}")
    if min_pass_rate is not None:
        for s in sums[1:] or sums:
            if s.pass_rate < min_pass_rate:
                problems.append(f"{s.config} pass rate {s.pass_rate:.0%} is under the minimum {min_pass_rate:.0%}")
    return problems
