"""anomaly.py — which rows of a finished run deserve a closer read, and where in the log to look.

The Analysis step runs in three stages, and this module is the hinge between the first two:

1. **Compute** (`analyze.skeleton`): every table, by counting. No judgement.
2. **Flag** (here): a fixed list of rules marks the rows whose numbers are off — and each flag
   carries *pointers*, the line numbers in `Log/EngineEvents.jsonl` and `Log/DeviceLog/dut.log`
   where the behaviour it summarises actually happened.
3. **Investigate** (an analyst): reads only those lines, and says what each flag means.

Flagging is rules, not a model, for the same reason the tables are: whether a number crossed a
threshold has one right answer. What a model is for is the read that follows — and a model handed
150,000 log lines with no pointer into them either reads a sample, and misses what the table
already showed, or reads all of it and spends its budget on the 99% that passed.

Not every flag is a failure. Most of what this finds raised no `ErrorSignature` at all: a call that
keeps landing just inside its budget, an edge whose destination the chain does not predict, a
memory series still climbing. Clustering only fires on a failed oracle check; these are the rows
where nothing failed and something is still wrong.

    python3 -m pod_mbt.anomaly test_result/<run folder>/            # the flags, as JSON
    python3 -m pod_mbt.anomaly test_result/<run folder>/ --excerpt ANOM-02
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# The thresholds, in one place, because each is a claim about what "off" means and a reviewer
# should be able to argue with it without reading a function.
NEAR_BUDGET = 0.80          # a pass that used more than this share of its budget is a near miss
NEAR_BUDGET_MIN = 10        # ...and an action needs this many of them before it is a pattern
NEAR_BUDGET_SHARE = 0.05    # ...or this share of its passing calls
POINTERS = 5                # lines kept per flag: enough to read, few enough to read all of

# What a flag can be. Every one names the rule that raised it; a kind outside this set is a bug.
KINDS = {
    "signature": "an ErrorSignature was clustered — a failed oracle check, already counted",
    "unexpected_edge": "the device moved somewhere the chain does not predict, or along an edge "
                       "no chain row describes, and no oracle check failed",
    "near_budget": "a postcondition kept passing, just inside its time budget",
    "resource_growth": "a per-process memory or fd series grew past the gate and was still "
                       "climbing when the run ended",
    "cleanup_lapsed": "a fault's cleanup never fired, so the device stayed perturbed for the "
                      "rest of the run",
}


@dataclass
class Anomaly:
    id: str
    kind: str
    component: Optional[str]
    state: Optional[str]
    action: Optional[str]
    figure: str                               # the number that tripped the rule, as text
    rule: str                                 # the rule, as text a reviewer can check
    count: int = 0                            # how many log events this flag summarises
    tMs: list[int] = field(default_factory=list)          # [first, last] offset seen
    pointers: list[dict[str, Any]] = field(default_factory=list)
    signatureHash: Optional[str] = None


def _load(run_dir: Path, rel: str, default: Any) -> Any:
    f = run_dir / rel
    return json.loads(f.read_text()) if f.is_file() else default


def _landed(t: dict, sc: Optional[dict]) -> Optional[str]:
    """Where a call left its component: no `state_change` means it stayed put."""
    return sc.get("to_state") if sc is not None else t.get("from_state")


def _off(t: dict, sc: Optional[dict]) -> bool:
    """Did the call land somewhere a healthy device would not have put it?

    The trigger's `expected` is the chain's destination, and it only applies when the guard held;
    a refused call's right destination is where it started. Without that, every correct refusal
    of an illegal call reads as a wrong destination.
    """
    right = t.get("expected", t.get("from_state")) if t.get("guard_held") else t.get("from_state")
    return _landed(t, sc) != right


def _matchers(coverage: dict, signatures: list[dict]) -> list[tuple[Anomaly, Any]]:
    """Each flag that can be found in the log, with the predicate that finds its events.

    A predicate sees one `action_trigger`, the `oracle_result` that judged it, and the
    `state_change` between them if the device moved — so a flag points at the call, not at
    whichever of its lines happened to match.
    """
    out: list[tuple[Anomaly, Any]] = []

    for s in signatures:
        a, tt = s["bug_bucket"], s["trigger_type"]
        if tt == "illegal_call_accepted":
            pred = (lambda a: lambda t, o, sc: t["action"] == a and not t.get("guard_held")
                    and o is not None and not o.get("passed"))(a)
        elif tt == "unexpected_destination":
            pred = (lambda a: lambda t, o, sc: t["action"] == a and _off(t, sc))(a)
        else:
            pred = (lambda a: lambda t, o, sc: t["action"] == a and o is not None
                    and not o.get("passed"))(a)
        out.append((Anomaly(
            id="", kind="signature", component=None, state=None, action=a,
            figure=f"{tt} × {s['occurrence_count']:,}",
            rule="clustered by TestRecorder on trigger_type:action_name",
            signatureHash=s["signature_hash"]), pred))

    for e in coverage.get("edges_anomaly", []) + [
            {"component": c, "from": st, "action": a, "to": {}, "note": "wrong destination"}
            for c, st, a in coverage.get("edges_wrong_destination", [])]:
        to = e.get("to") or {}
        if e.get("note") == "no chain row describes this edge":
            figure = f"{sum(to.values()):,}× along an edge no chain row has"
            rule = "observed (component, state, action) has no row in MarkovPolicy"
        else:
            figure = ", ".join(f"→ {k} {v:,}×" for k, v in sorted(to.items(), key=lambda x: -x[1]))
            if e.get("guardFailedMoves"):
                figure += f" ({e['guardFailedMoves']:,} with the guard not held)"
            rule = "destination differs from the chain's prediction, oracle passed"
        # An edge with no chain row is off-model on every call; one with a row is off only when
        # the call landed somewhere other than where the chain said.
        any_call = e.get("note") == "no chain row describes this edge"
        pred = (lambda c, st, a, every: lambda t, o, sc: t.get("component") == c
                and t.get("from_state") == st and t["action"] == a
                and (every or _off(t, sc)))(
            e["component"], e["from"], e["action"], any_call)
        out.append((Anomaly(id="", kind="unexpected_edge", component=e["component"],
                            state=e["from"], action=e["action"], figure=figure, rule=rule),
                    pred))
    return out


def _resource_flags(run_dir: Path) -> list[Anomaly]:
    from .resource import trend

    rows = [e.get("details", {}) for e in _load(run_dir, "Log/ResourceSnapshot.json", [])]
    rows = [d for d in rows if d.get("processes")]
    if len(rows) < 30:
        return []
    hours = [d.get("t_ms", 0) / 3_600_000 for d in rows]
    series: dict[tuple[str, str], list[float]] = {}
    for d in rows:
        for name, pr in d["processes"].items():
            for f_ in ("pss_kb", "fd_count"):
                series.setdefault((name, f_), []).append(float(pr[f_]))
    out = []
    for (name, f_), v in sorted(series.items()):
        # The same gate `analyze._resource_section` renders, so a flag and its table row agree.
        t = trend(f"{name}.{f_}", "fd" if f_ == "fd_count" else "kB", hours, v,
                  growth_threshold=10.0, min_r2=0.20, floor=1.0, window=11, shaped=True)
        if t.flagged:
            out.append(Anomaly(
                id="", kind="resource_growth", component=None, state=None, action=None,
                figure=f"{name}.{f_} {t.growth_pct:+.1f}% (R² {t.r_squared:.2f}, {t.shape})",
                rule="fitted growth ≥ 10%, R² ≥ 0.20, last third still climbing",
                count=len(v), tMs=[rows[0].get("t_ms", 0), rows[-1].get("t_ms", 0)]))
    return out


def find(run_dir: Path, coverage: Optional[dict] = None,
         signatures: Optional[list] = None) -> list[Anomaly]:
    """Every flag for one run folder, numbered, each with pointers into both logs.

    One pass over `EngineEvents.jsonl`. It is the largest file in the folder and the skeleton
    otherwise avoids it; this is the one step that cannot, because a pointer is a line number.
    """
    run_dir = Path(run_dir)
    coverage = coverage if coverage is not None else _load(
        run_dir, "Analysis/CoverageReport.json", {})
    signatures = signatures if signatures is not None else _load(
        run_dir, "Analysis/ErrorSignature.json", [])
    matched = _matchers(coverage, signatures)

    # Per action: passing calls, and those that passed above NEAR_BUDGET of their budget.
    passes: dict[str, int] = {}
    near: dict[str, Anomaly] = {}

    dut = run_dir / "Log" / "DeviceLog" / "dut.log"
    dut_lines = dut.read_text().splitlines() if dut.is_file() else []

    def hit(an: Anomaly, trig: tuple[int, int, dict], line: int) -> None:
        ordinal, tline, t = trig
        an.count += 1
        tm = t.get("t_ms", 0)
        an.tMs = [min(an.tMs[0], tm), max(an.tMs[1], tm)] if an.tMs else [tm, tm]
        if len(an.pointers) < POINTERS:
            # dut.log is one line per call, in call order. That is true of the simulator and of
            # nothing else, so the line is only claimed when it names the same call.
            d = dut_lines[ordinal] if ordinal < len(dut_lines) else None
            same = d is not None and f": {t['action']} " in f"{d} "
            an.pointers.append({"engineLine": tline, "resolvedLine": line, "tMs": tm,
                                "worker": t.get("_source"),
                                "dutLine": ordinal + 1 if same else None})

    f = run_dir / "Log" / "EngineEvents.jsonl"
    if f.is_file():
        # A call is `action_trigger`, then a `state_change` only if the device moved, then its
        # `oracle_result` — always last, so that is where the call is closed and judged.
        pending: dict[str, tuple[int, int, dict]] = {}
        moved: dict[str, dict] = {}
        ordinal = 0
        with f.open() as fh:
            for n, raw in enumerate(fh, 1):
                e = json.loads(raw)
                et, src, d = e["event_type"], e["source"], e["details"]
                if et == "action_trigger":
                    pending[src] = (ordinal, n, {**d, "_source": src})
                    moved.pop(src, None)
                    ordinal += 1
                elif et == "state_change" and src in pending:
                    moved[src] = d
                elif et == "oracle_result" and src in pending:
                    trig = pending.pop(src)
                    t, sc = trig[2], moved.pop(src, None)
                    for an, pred in matched:
                        if pred(t, d, sc):
                            hit(an, trig, n)
                    if d.get("passed"):
                        a = d["action"]
                        passes[a] = passes.get(a, 0) + 1
                        b = d.get("budget_ms") or 0
                        if b and d.get("elapsed_ms", 0) >= b * NEAR_BUDGET:
                            an = near.setdefault(a, Anomaly(
                                id="", kind="near_budget", component=t.get("component"),
                                state=None, action=a, figure="",
                                rule=f"passed with elapsed ≥ {NEAR_BUDGET:.0%} of budget, "
                                     f"≥ {NEAR_BUDGET_MIN}× or ≥ {NEAR_BUDGET_SHARE:.0%} "
                                     "of passes"))
                            hit(an, trig, n)

    flags = [an for an, _ in matched]
    for a, an in near.items():
        share = an.count / passes[a] if passes.get(a) else 0
        if an.count >= NEAR_BUDGET_MIN or share >= NEAR_BUDGET_SHARE:
            an.figure = f"{an.count:,} of {passes[a]:,} passes ({share:.1%}) above " \
                        f"{NEAR_BUDGET:.0%} of budget"
            flags.append(an)
    flags += _resource_flags(run_dir)
    cl = coverage.get("cleanup") or {}
    for lp in cl.get("lapsedDetail", []):
        flags.append(Anomaly(
            id="", kind="cleanup_lapsed", component=None, state=None, action=lp.get("action"),
            figure=f"after {lp.get('after')}: deadline step {lp.get('deadlineStep')}, "
                   f"reached {lp.get('reachedStep')}",
            rule="a scheduled cleanup_action did not fire before its deadline"))

    order = list(KINDS)
    flags.sort(key=lambda x: (order.index(x.kind), -x.count))
    for i, an in enumerate(flags, 1):
        an.id = f"ANOM-{i:02d}"
    return flags


def excerpt(run_dir: Path, an: Anomaly, context: int = 2) -> list[str]:
    """The lines an investigator reads for one flag: each pointer, with `context` lines either
    side from the engine log, and the device's own line for the same call.

    This is the whole input to stage 3 for that flag — what a model is given instead of the
    folder. A flag with no pointers (a resource series, a lapsed cleanup) reads its own figure.
    """
    eng = (Path(run_dir) / "Log" / "EngineEvents.jsonl").read_text().splitlines()
    dut_f = Path(run_dir) / "Log" / "DeviceLog" / "dut.log"
    dut = dut_f.read_text().splitlines() if dut_f.is_file() else []
    out = []
    for p in an.pointers:
        lo, hi = max(1, p["engineLine"] - context), min(len(eng), p["resolvedLine"] + context)
        out.append(f"--- EngineEvents.jsonl:{lo}-{hi}  ({p['worker']}, t={p['tMs']} ms)")
        out += [f"{n:>7}  {eng[n - 1]}" for n in range(lo, hi + 1)]
        if p.get("dutLine"):
            out.append(f"--- dut.log:{p['dutLine']}")
            out.append(f"{p['dutLine']:>7}  {dut[p['dutLine'] - 1]}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Flag the rows of a run worth reading, with "
                                             "pointers into its logs.")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--excerpt", metavar="ANOM-NN", help="print the log lines behind one flag")
    a = ap.parse_args(argv)
    flags = find(a.run_dir)
    if a.excerpt:
        hit = [f for f in flags if f.id == a.excerpt]
        if not hit:
            print(f"no flag {a.excerpt}; this run has {', '.join(f.id for f in flags)}")
            return 1
        print("\n".join(excerpt(a.run_dir, hit[0])))
        return 0
    print(json.dumps([asdict(f) for f in flags], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
