"""Planning — turning an engineer's intent plus past runs into the next run's config.

This is the seam the "AI-driven" claim actually rests on, and it is deliberately the same
kind of seam as `Driver`: an interface with a runnable stand-in behind it. `Driver` lets
the whole design run without a bench; `RuleBasedPlanner` lets it run without a model.

What a planner may decide, and what it may not, is not a limitation of the stand-in — it
is the design:

* **May decide** which components get a worker, what each one runs, how long the session
  is, how often each worker acts, which faults are scheduled at which offsets, and which
  saved reproductions are worth re-running first. All of that only ever *references*
  actions that already exist.
* **May not decide** what an action means or what a probability row says. `ActionDatabase`
  and `MarkovPolicy` change when a person changes them; everything a planner would like to
  change there comes out as a proposal, with its evidence, for review.

So the plan can change what a run exercises, and can never change what a result means.
That is the property that makes it safe to let a model write one.

    python3 -m pod_mbt.plan --changed wifi --minutes 480 --history runs/ --out next_run.json
    python3 -m pod_mbt.run next_run.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from .model import ACTION_DB
from .policy import FAULTS, MarkovPolicy

COMPONENTS = ("wifi", "bt", "matter")


@dataclass
class Intent:
    """What the test engineer asked for. The only human input a plan needs."""

    changed: list[str] = field(default_factory=list)   # components under suspicion
    scope: list[str] = field(default_factory=list)    # what this bench can drive
    minutes: int = 480
    bench: str = "simulation"
    note: str = ""
    # component -> avgIntervalMs, for a TE who wants to set a walker's pace directly
    # instead of accepting the size-derived default below. Absent means "derive it."
    intervals: dict[str, int] = field(default_factory=dict)


@dataclass
class History:
    """What earlier runs already established. A plan with no history is still a plan."""

    runs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, root: Optional[Path]) -> "History":
        if root is None or not Path(root).is_dir():
            return cls()
        runs = []
        for folder in sorted(Path(root).iterdir()):
            meta = folder / "session.json"
            if not meta.is_file():
                continue
            entry: dict[str, Any] = {"folder": folder.name,
                                     "session": json.loads(meta.read_text())}
            for key, rel in (("coverage", "Analysis/CoverageReport.json"),
                             ("signatures", "Analysis/ErrorSignature.json")):
                f = folder / rel
                if f.is_file():
                    entry[key] = json.loads(f.read_text())
            runs.append(entry)
        return cls(runs)

    def never_accepted(self) -> list[str]:
        """Actions no run has ever had the device accept — refused every time, or never
        tried at all. These are where a plan can still buy something new."""
        accepted: set[str] = set()
        for r in self.runs:
            accepted |= set(r.get("coverage", {}).get("actions_accepted", []))
        return sorted({a.name for a in ACTION_DB} - accepted)

    def untried_pairs(self, policy) -> set[tuple[str, str, str]]:
        """(component, state, action) combinations no run has fired yet — the space a
        chain never describes, and where an action-led probe earns its keep."""
        tried: set[tuple] = set()
        for r in self.runs:
            for t in r.get("coverage", {}).get("pairs", []):
                tried.add(tuple(t))
        every = {(c, st, a.name) for c in policy.chains for st in policy.states(c)
                 for a in ACTION_DB if a.component == c}
        return every - tried

    def open_signatures(self) -> list[dict[str, Any]]:
        """Distinct failures seen before, newest run first — regression candidates."""
        seen: dict[str, dict[str, Any]] = {}
        for r in reversed(self.runs):
            for sig in r.get("signatures", []):
                key = f"{sig['trigger_type']}:{sig['bug_bucket']}"
                seen.setdefault(key, {"folder": r["folder"], **sig})
        return list(seen.values())


class Planner(Protocol):
    """Swap the implementation, not the shape. An LLM planner reads the same two inputs
    and returns the same document — the engine cannot tell which one wrote it."""

    def plan(self, intent: Intent, history: History) -> dict[str, Any]: ...


class RuleBasedPlanner:
    """A planner with no model behind it, so the seam can be exercised and reviewed.

    Its decisions are the obvious ones: pace each walker by how much chain it has to
    cover, probe the pairs nothing has tried, spend the fault budget on what has never
    landed, and say what is being given up. An LLM planner is expected to do better at
    exactly those, which is also how it can be judged.
    """

    # How much of a component's grid has to be untried before a sweep is worth the
    # session it costs. A judgement, not a constant of nature — an LLM planner is
    # expected to make a better one, which is also how it can be judged.
    sweep_threshold = 0.30

    def __init__(self, session_id: str = "PLANNED-001"):
        self.session_id = session_id

    def plan(self, intent: Intent, history: History) -> dict[str, Any]:
        pol = MarkovPolicy()
        duration = max(1, intent.minutes) * 60_000
        changed = [c for c in intent.changed if c in pol.chains]
        scope = [c for c in (intent.scope or list(pol.chains)) if c in pol.chains]
        rationale: list[str] = []

        # 0. Which components should be swept rather than walked and probed. A walk is
        #    capped at the transitions its chain declares and a probe plateaus well short
        #    of the grid, so a component still missing most of its state x action pairs
        #    gets the one worker that closes them — and gives up its walk and probe to
        #    pay for it. Nothing to go on before the first run, so this needs history.
        untried_pairs = history.untried_pairs(pol)
        sweep = []
        if history.runs:
            for c in list(scope):
                total = len(pol.states(c)) * len([a for a in ACTION_DB if a.component == c])
                left = len({a for comp, _, a in untried_pairs if comp == c})
                if total and left > total * self.sweep_threshold:
                    sweep.append({"workerId": f"{c}-sweep", "component": c,
                                  "avgIntervalMs": 120_000})
                    scope.remove(c)
        if sweep:
            rationale.append(
                f"{', '.join(s['component'] for s in sweep)} still "
                f"{'has' if len(sweep) == 1 else 'have'} more than "
                f"{self.sweep_threshold:.0%} of the state x action grid untried, so "
                f"{'it is' if len(sweep) == 1 else 'they are'} swept instead of walked and "
                "probed: a sweep exhausts each state and takes the chain out of it, which "
                "is the only way the remaining cells get reached inside one session.")

        # 1. Walkers, paced by how much chain each one has to get through. A two-state
        #    chain saturates while a five-state one is still half unseen.
        sizes = {c: len([e for e in pol.transitions() if e[0] == c]) for c in scope}
        widest = max(sizes.values()) if sizes else 1
        walk = []
        overridden = [c for c in scope if c in intent.intervals]
        for c in scope:
            if c in intent.intervals:
                interval = intent.intervals[c]
            else:
                base = 240_000 * (sizes[c] / widest if sizes[c] else 1)
                interval = int(base / 2) if c in changed else int(base)
            walk.append({"workerId": f"{c}-worker", "component": c,
                         "avgIntervalMs": max(30_000, interval)})
        rationale.append(
            "Walkers are paced by chain size — "
            + ", ".join(f"{c} has {sizes[c]} transitions" for c in scope)
            + (f"; {', '.join(changed)} then runs twice as often because it changed. "
               if changed else ". ")
            + "The probability rows themselves are untouched: weighting them is a "
              "proposal, not a plan."
            + (f" {', '.join(overridden)} "
               + ("uses" if len(overridden) == 1 else "use")
               + " the pace the TE set directly, in place of that derived rate."
               if overridden else ""))

        # 2. Probes, aimed at the pairs no run has tried. This is where a plan has room,
        #    because the chain never described these combinations in the first place.
        untried = untried_pairs
        probe = []
        for c in scope:
            pool = sorted({a for comp, _, a in untried if comp == c})
            if pool or not history.runs:
                probe.append({"workerId": f"{c}-probe", "component": c,
                              "avgIntervalMs": 180_000, "pool": pool})
        if probe:
            n = sum(len(p.get("pool", [])) for p in probe)
            rationale.append(
                f"Probes carry {n or 'every'} action(s) that no previous run has fired "
                "in the state it will find. The guard still judges each one, so a refusal "
                "is a result rather than noise.")

        # 3. Faults, on the clock, favouring the ones nothing has ever had accepted.
        never = [n for n in history.never_accepted() if n in FAULTS] or FAULTS[:8]
        step = max(60_000, duration // (len(never) + 1))
        faults = [{"actionName": n, "atMs": step * (i + 1)} for i, n in enumerate(never)]
        rationale.append(
            f"{len(faults)} fault(s) on the clock, spread across the session. A fault that "
            "must land while an action is in flight cannot be rolled by a walker; it has "
            "to be scheduled.")

        # 4. What the plan is not buying. A plan that lists only its wins is advocacy.
        not_covering = []
        quiet = [c for c in scope if c not in changed]
        if changed and quiet:
            not_covering.append(
                f"{', '.join(quiet)} walk at half the rate of {', '.join(changed)} — a "
                "regression there is likelier to be missed this session than last.")
        off = [c for c in pol.chains if c not in scope]
        if off:
            not_covering.append(f"{', '.join(off)} is not exercised at all this session.")
        if intent.minutes < 120:
            not_covering.append(
                f"{intent.minutes} minutes is short for a weighted walk; expect coverage "
                "to be luck rather than method.")

        return {
            "sessionId": self.session_id,
            "durationMs": duration,
            "walk": walk,
            "probe": probe,
            "sweep": sweep,
            "faults": faults,
            "replay": [
                {"from": s["folder"], "signatureHash": s["signature_hash"],
                 "bugBucket": s["bug_bucket"]}
                for s in history.open_signatures()[:6]],
            # Read-only, for the person reviewing: the chain this plan walks, and what it
            # predicts. The plan may print the matrix; it may not change it.
            "usesPolicy": {c: {"states": pol.states(c),
                               "stationary": pol.stationary(c)} for c in scope},
            "intent": {"changed": changed, "scope": scope, "minutes": intent.minutes,
                       "bench": intent.bench, "note": intent.note},
            "basedOn": [r["folder"] for r in history.runs],
            "generatedBy": type(self).__name__,
            "rationale": rationale,
            "notCovering": not_covering,
            "status": "draft — a person accepts this before it runs",
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Plan the next run from an engineer's intent and previous run folders.")
    ap.add_argument("--changed", action="append", default=[], choices=list(COMPONENTS),
                    help="a component that changed and deserves the weight")
    ap.add_argument("--scope", action="append", default=[], choices=list(COMPONENTS),
                    help="what this bench can drive; default is everything")
    ap.add_argument("--minutes", type=int, default=480)
    ap.add_argument("--bench", default="simulation")
    ap.add_argument("--note", default="")
    ap.add_argument("--history", type=Path, help="directory holding previous run folders")
    ap.add_argument("--session-id", default="PLANNED-001")
    ap.add_argument("--out", type=Path, help="write the plan here; default is stdout")
    a = ap.parse_args(argv)

    history = History.load(a.history)
    plan = RuleBasedPlanner(a.session_id).plan(
        Intent(changed=a.changed, scope=a.scope, minutes=a.minutes, bench=a.bench,
               note=a.note), history)
    text = json.dumps(plan, indent=2)

    if a.out:
        a.out.write_text(text + "\n")
        print(f"\n  plan           {a.out}")
        print(f"  based on       {len(history.runs)} previous run(s)")
        for line in plan["rationale"]:
            print(f"  why            {line}")
        for line in plan["notCovering"]:
            print(f"  not covering   {line}")
        print(f"\n  run it with    python3 -m pod_mbt.run {a.out}\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
