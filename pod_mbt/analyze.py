"""analyze.py — the "AI reads a finished run" step: report.md + proposals/.

Same seam as `Driver` and `Planner`: an interface with a runnable stand-in behind it.
`RuleBasedAnalyst` is deliberately not an LLM — it is rules over the files a real analyst would
read anyway, so the seam is exercised and testable without an API key. A real LLM-backed analyst
reads the same run folder and writes the same two things, and nothing downstream can tell which
one wrote them.

**The report is a skeleton plus a few slots, and only the slots are an analyst's to write.**
`skeleton()` builds the title, the Summary block, the test-case figure and every computed table:
identical for every run and for every analyst, because each of those is a lookup or set
arithmetic with exactly one right answer. What differs between analysts is judgement — which
finding matters, why, and what to propose — and that goes in `SLOTS`, a fixed allowlist of named
places. A model therefore cannot restate a number it disagrees with, and its contribution can be
reviewed slot by slot.

That split is not tidiness. Building the fixed parts inside `RuleBasedAnalyst` would mean an
LLM analyst reimplements them, and two implementations of the same header drift — which is
precisely what happened to the Blueprint's Slide 01 against `policy.CHAINS`, in a project whose
test suite exists to stop documents disagreeing with data.

**Compute, flag, investigate, propose.** The skeleton computes every table. `anomaly.find` then
marks the rows that are off — most of them raised no ErrorSignature — and gives each one pointers
into `EngineEvents.jsonl` and `dut.log`. An analyst investigates each flag from those lines alone
and returns a *finding*: one verdict out of `VERDICTS`, the evidence it read, and what would settle
it if it is not settled. Proposals are written from findings, and a `targeted_run` proposal is
what the next plan reads back once a person accepts it.

`device_bug` is the one verdict that needs more than reading. `write_back` refuses it unless the
finding names what confirmed it — a replay that reproduced, a soak, a baseline — because a log
excerpt can make a bug plausible and only re-running it makes it a bug.

What this stand-in does NOT do, on purpose: read the excerpts. Its findings come from the flag's
kind and its pointers' metadata, so most of them end `inconclusive` with the step that would
decide them. That is the stage a real LLM analyst is for, and the one this rule-based version is
honest about not attempting.

    python3 -m pod_mbt.run testcases/TC-01/chaos.json --defect accepts_illegal --save test_result/
    python3 -m pod_mbt.analyze test_result/<the folder just printed>/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from . import anomaly as A
from . import html as H
from .figure import chain_figure
from .model import ACTION_DB
from .policy import CHAINS, FAULTS, load_test_case

# The only places an analyst may write. Anything outside this set is the skeleton's, which is
# how a figure stays a figure no matter who produced the prose around it.
# One analysis slot per section that has facts worth reading into, plus the proposals that come
# out of reading them. The earlier shape was three global slots — a verdict, a significance note,
# a correlation note — which put the reading in one place and the facts in another, so a reader
# had to hold a table in their head to follow the sentence about it. Each section now states its
# numbers and then reads them, and the proposals are written from those readings rather than from
# the raw folder: an analyst that cannot explain a finding in its own section has no business
# proposing a change from it.
ANALYSED = ("crash", "coverage", "execution", "monitor", "failures", "anomalies", "proposals")
SLOTS = tuple(f"{k}.analysis" for k in ANALYSED)

# The report's shape, in order. `crash` renders only when there was one and everything below it
# shifts down, which is why section numbers are assigned at assembly rather than written in.
# A section with nothing to say renders as a marked blank rather than vanishing: a reader who
# cannot tell "measured nothing" from "does not measure this" has to go and ask.
SECTIONS = ("summary", "testcase", "crash", "coverage", "execution",
            "monitor", "failures", "anomalies", "proposals")

# What an investigation may conclude about one flag. Anything else is refused at write-back.
VERDICTS = {
    "device_bug": "the device did something a healthy device would not — needs `confirmedBy`",
    "model_error": "the device is right and the model is not: a guard, a postcondition, a "
                   "budget or a probability row says the wrong thing",
    "model_gap": "the device did something the model has no action or chain row for at all",
    "config_gap": "the plan or harness, not the device or the model: a missing worker, a lapsed "
                  "cleanup, a schedule that cannot reach the cell",
    "environment": "the bench, not the device: RF, the AP, the controller",
    "inconclusive": "the excerpt does not decide it — `decidedBy` says what would",
}
# What can turn a plausible bug into a bug. A reading is never on this list.
CONFIRMATIONS = ("replay", "soak", "baseline")

# The order the pieces are assembled in — data, not control flow, so adding a section does not
# mean finding the right place in a function.
# Kept while the markdown writer is replaced section by section.
LAYOUT = tuple(("section", k) for k in SECTIONS)


@dataclass
class Report:
    """The fixed part, plus the assets it needs written beside `report.md`."""

    sections: dict[str, str] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)   # relative path -> contents
    facts: dict[str, Any] = field(default_factory=dict)     # what an analyst reads, not recomputes


@dataclass
class Analysis:
    """What gets written back — the same shape whether a person, a rule or a model produced it."""

    sections: dict[str, str] = field(default_factory=dict)
    slots: dict[str, str] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)   # one per anomaly read


class Analyst(Protocol):
    """Swap the implementation, not the shape — same pattern as `Driver` and `Planner`."""

    def analyze(self, run_dir: Path,
                baseline: Optional[Path] = None) -> Analysis: ...


def _load(run_dir: Path, rel: str, default: Any) -> Any:
    f = run_dir / rel
    return json.loads(f.read_text()) if f.is_file() else default


# --------------------------------------------------------------------- the fixed part
def _measured_elapsed(run_dir: Path, session: dict) -> Optional[int]:
    """Prefer the field; fall back to the log for a folder written before it existed.

    `elapsedMs` is recorded now, so the common path is a lookup. The fallback streams
    `EngineEvents` — the largest file in the folder — which is exactly the cost the field exists
    to avoid, and it is here only so an older run folder still produces a report.
    """
    if session.get("elapsedMs"):
        return int(session["elapsedMs"])
    f = run_dir / "Log" / "EngineEvents.jsonl"
    if not f.is_file():
        return None
    last, pending = 0, {}
    with f.open() as fh:
        for line in fh:
            e = json.loads(line)
            d, src = e["details"], e["source"]
            if e["event_type"] == "action_trigger":
                pending[src] = d.get("t_ms", 0)
                last = max(last, pending[src])
            elif e["event_type"] == "oracle_result" and src in pending:
                last = max(last, pending.pop(src) + d.get("elapsed_ms", 0))
    return last or None


def _hm(ms: int) -> str:
    return f"{ms // 3_600_000}H{ms % 3_600_000 // 60_000:02d}M"


def _stop_reason(session: dict, measured: Optional[int]) -> tuple[str, str]:
    """Why the run ended, and whether that was recorded or inferred.

    The field exists now; the fallback is for folders written before it did, and a report should
    say which it is reading rather than presenting both the same way.
    """
    if session.get("stopReason"):
        return session["stopReason"], "recorded in session.json"
    if session.get("result", {}).get("crashed"):
        return "Crash", "derived from result.crashed"
    planned = session.get("durationMs") or 0
    if measured is None or not planned:
        return "unknown", "derived"
    return (("CompleteDuration" if measured >= planned * 0.99 else "StoppedEarly"),
            "derived, no field records it")


def _cell(k: str, v: Any, tone: str = "") -> str:
    """One Summary cell: a label and a value, nothing else.

    `tone` is ok / bad / none. A field with no source reads `N/A` rather than an explanation —
    the Summary is scanned, and a sentence in a cell is read by nobody and crowds the ten figures
    that are.
    """
    return (f'<div><span class="k">{H.esc(k)}</span>'
            f'<span class="v{" " + tone if tone else ""}">{v}</span></div>')


def _pct(a: int, b: int) -> str:
    return f"{a / b:.1%}" if b else "—"


def _execution_section(c: dict, session: dict) -> str:
    """Who issued what: one row per worker, one column per component.

    Counting, so it belongs to the skeleton rather than to an analyst. The counts come from
    `CoverageReport.per_worker`, tallied while the run happened — the alternative is scanning
    `EngineEvents`, the largest file in the folder, for something the tracker already saw.

    `per_worker` also carries how many of each worker's calls had a guard that did not hold. It is
    not rendered here, and the field stays because it is the same tally with one extra condition
    and a later section may want it.
    """
    pw = c.get("per_worker") or {}
    if not pw:
        return ""
    kinds = {w["workerId"]: w.get("kind", "") for w in session.get("workers", [])}
    comps = sorted({k for w in pw.values() for k in w["byComponent"]},
                   key=lambda x: (x == "system", x))
    grand = sum(w["total"] for w in pw.values())

    L = ["## Execution result", "",
         "| worker | kind | " + " | ".join(c_.upper() for c_ in comps) + " | total | share |",
         "|---|---|" + "---|" * len(comps) + "---|---|"]
    order = sorted(pw, key=lambda w: -pw[w]["total"])
    for w in order:
        row = pw[w]["byComponent"]
        cells = " | ".join(f"{row[c_]:,}" if c_ in row else "—" for c_ in comps)
        L.append(f"| `{w}` | {kinds.get(w, '') or '—'} | {cells} | {pw[w]['total']:,} | "
                 f"{pw[w]['total'] / grand:.1%} |")
    totals = " | ".join(f"{sum(pw[w]['byComponent'].get(c_, 0) for w in pw):,}" for c_ in comps)
    L += [f"| **total** | | {totals} | **{grand:,}** | 100% |", ""]
    return "\n".join(L)


def _testcase_table(chains: dict, legal: int) -> str:
    """What the test case declares. Nothing about any run.

    It sits above the coverage figures because it is their denominator: the same information the
    drawing carries, counted, and the thing every percentage below is a fraction of.

    Grouped by who performs the action. `api_inject_wrong_psk` and `inject_ap_psk_changed` are both
    "the password is wrong"; one is software on the device and one means somebody reconfigures the
    AP, and a grouping that puts them together hides the only difference that costs money. So
    everything the device does to itself sits under its own region, and everything an external rig
    fires sits together — which is also the hardware list a bench needs before this test case can
    run at all.

    `positive` is ordinary use, `negative` is deliberate breakage the device inflicts on itself,
    and `total action` is the two added up. The split says which behaviour is under test, not which
    mechanism issues the call.

    `chain edge` is how many arrows that region carries on the drawing — how many
    state-to-state transitions the chain declares. It is not the action count and should not be
    expected to match it: one action can cause a transition out of several states, and an action
    with no arrow at all is one only a probe or a sweep reaches. `fault inject` has no edges by
    definition: a fault is fired by a clock at whatever state the device is in, so it has no source
    state to draw an arrow from.

    A group with no actions is left out rather than shown at zero. TC-01 declares no system
    conditions: CPU and memory pressure are fields on `CompositeState` that stay `Normal`, read by
    guards and by the device's own timing, and they are measured every thirty seconds rather than
    tested.
    """
    from .model import ACTION_DB, Category, Executor
    groups: dict[str, list] = {c: [] for c in chains}
    groups["fault inject"] = []
    for a in ACTION_DB:
        key = "fault inject" if a.executor is Executor.EQUIPMENT else a.component
        groups.setdefault(key, []).append(a)

    dash = '<span class="dash">&mdash;</span>'
    rows = []
    for name, acts in groups.items():
        if not acts:
            continue
        pos = [a for a in acts if a.category is Category.API]
        neg = [a for a in acts if a.category is Category.FAULT]
        states = len(chains[name]) if name in chains else None
        edges = (sum(len(r) for r in chains[name].values()) if name in chains else None)
        rows.append([name,
                     H.num(states) if states else dash,
                     H.num(len(pos)) if pos else dash,
                     H.num(len(neg)) if neg else dash,
                     H.num(len(acts)),
                     H.num(edges) if edges else dash])
    return H.table(["group", "state", "positive", "negative", "total action", "chain edge"],
                   rows, right=[1, 2, 3, 4, 5], title="Test case statistics",
                   note=f"The regions combine into {legal:,} composite states no legality rule "
                        "declares impossible. That is a property of the test case, not of any run.")


def _coverage_section(c: dict, base: dict, chains: dict, issued: set) -> str:
    """How much of the test case this run reached.

    Three denominators, because there are three questions and one number answers none of them
    alone: the same cells are a fraction of the model and a much larger fraction of what this
    configuration could ever reach. A shortfall against the model is closed by changing the plan;
    a shortfall against the plan's own ceiling is closed in a guard or by running longer.
    """
    cmp_ = bool(base)
    out = [_testcase_table(chains, c.get("composite_states_legal", 0))]

    acc = len(c.get("actions_accepted", []))
    all_a = acc + len(c.get("actions_never_accepted", []))
    bacc = len(base.get("actions_accepted", []) or [])
    measures = [
        ("chain transitions", c.get("transitions_walked", 0), c.get("transitions_total", 0),
         c.get("transitions_total", 0), base.get("transitions_walked") if cmp_ else None),
        ("state-action pairs", c.get("pairs_tried", 0), c.get("plan_ceiling_pairs", 0),
         c.get("pairs_total", 0), base.get("pairs_tried") if cmp_ else None),
        ("legal composite states", c.get("composite_states_visited", 0), None,
         c.get("composite_states_legal", 0),
         base.get("composite_states_visited") if cmp_ else None),
        # `actions ever accepted` used to sit here. It belongs with Execution result: whether a
        # call was accepted is what happened when it was made, not how much of the model a run
        # covered, and reading it beside two coverage denominators invited it to be read as a
        # third one.
    ]
    head = ["measure", "reached", "plan ceiling", "model total", "vs plan", "vs model"]
    right = [1, 2, 3, 4, 5]
    if cmp_:
        head.append("vs baseline")
        right.append(6)
    rows = []
    for name, reached, ceil, total, b in measures:
        r = [name, H.num(reached),
             H.num(ceil) if ceil else f'<span class="dash">&mdash;</span>', H.num(total),
             H.pct(reached, ceil) if ceil else f'<span class="dash">&mdash;</span>',
             H.pct(reached, total)]
        if cmp_:
            r.append(H.delta(reached, b))
        rows.append(r)
    out.append(H.table(head, rows, right=right, title="Test coverage"))

    never = c.get("actions_never_accepted", [])
    refused = set(c.get("refused_actions", []))
    if never:
        out.append(H.table(
            ["action", "what happened"],
            [[H.code(a), "issued, refused every time" if a in refused
              else "<b>never issued</b> &mdash; no mechanism in this plan calls it"]
             for a in never],
            title="Actions the device never accepted",
            note="Issued-and-refused is a test: the guard judging the refusal is the assertion. "
                 "Never issued is not &mdash; it is either a model that should not have declared "
                 "the action or a plan missing a worker, and which of the two cannot be decided "
                 "from here."))

    un = c.get("uncounted_states") or {}
    if un:
        names = ", ".join(f"{H.code(comp)} {', '.join(v)}" for comp, v in sorted(un.items()))
        out.append(f'<figcaption>Outside every denominator above: {names}. These are the '
                   "transient windows no chain row describes, so no pair is recorded while the "
                   "device is in one.</figcaption>")
    return "".join(out)


def _resource_section(run_dir: Path) -> str:
    """Per-process memory and fd counts, fitted. The one resource claim that holds up.

    Memory is an accumulating level: nothing dilutes a leak, so a straight line fits it closely
    and a growth figure means something. Per-thread CPU is a differenced bursty rate whose mean is
    dominated by burst count, so a rise in a thread's idle floor arrives diluted by the burst
    share — four separate instruments failed to recover planted floor drifts from it at 30-second
    sampling, and longer runs and faster sampling did not rescue any of them. So CPU is reported
    as a level here and never as a verdict.
    """
    from .resource import smooth, trend

    snaps = _load(run_dir, "Log/ResourceSnapshot.json", [])
    rows = [e.get("details", {}) for e in snaps]
    rows = [d for d in rows if d.get("processes")]
    if len(rows) < 30:
        return ("## Resources\n\nNo numeric telemetry in this run folder. Until "
                "`ResourceSnapshot` carries per-process PSS and fd counts, no leak, drift or "
                "slope claim belongs in a report — a three-level `Stress` enum can only echo the "
                "fault schedule back.\n")

    hours = [d.get("t_ms", 0) / 3_600_000 for d in rows]
    series: dict[tuple[str, str], list[float]] = {}
    for d in rows:
        for name, pr in d["processes"].items():
            series[(name, "pss_kb")] = series.get((name, "pss_kb"), []) + [float(pr["pss_kb"])]
            series[(name, "fd_count")] = series.get((name, "fd_count"), []) + \
                [float(pr["fd_count"])]
    fits = []
    for (name, field_), v in series.items():
        t = trend(f"{name}.{field_}", "fd" if field_ == "fd_count" else "kB", hours, v,
                  growth_threshold=10.0, min_r2=0.20, floor=1.0, window=11, shaped=True)
        fits.append((name, field_, t))
    fits.sort(key=lambda x: -x[2].growth_pct)

    flagged = [f for f in fits if f[2].flagged]
    L = ["## Resources", "",
         f"{len(rows):,} samples. Every series below is fitted by least squares over a smoothed "
         "copy; a growth figure is the fitted line's rise across the session relative to where it "
         "started, not last sample minus first.", "",
         "| series | start | end | growth | R² | shape | verdict |",
         "|---|---|---|---|---|---|---|"]
    for name, field_, t in fits:
        unit = "fd" if field_ == "fd_count" else "MB"
        conv = (lambda x: x) if field_ == "fd_count" else (lambda x: x / 1024)
        L.append(f"| `{name}.{field_}` | {conv(t.first_fit):,.1f} | {conv(t.last_fit):,.1f} "
                 f"{unit} | {t.growth_pct:+.1f}% | {t.r_squared:.2f} | {t.shape or '—'} | "
                 + ("**leak**" if t.flagged else "within") + " |")
    L.append("")
    if flagged:
        L += [f"**{len(flagged)} series over the 10% gate and still climbing at the end.** A "
              "series that clears the gate but levels off is a bounded cache filling up, not a "
              "leak, and the last third's slope against the whole session's is what separates "
              "them — so growth alone is never the whole verdict.", ""]
    levelled = [f for f in fits if f[2].shape == "levelled off" and f[2].growth_pct >= 10]
    for name, field_, t in levelled:
        L.append(f"- `{name}.{field_}` grew {t.growth_pct:.1f}% and stopped: the last third runs "
                 f"at {t.tail_ratio:.0%} of the session slope. Not flagged.")
    if levelled:
        L.append("")
    cpu = [d.get("cpu_pct") for d in rows if d.get("cpu_pct") is not None]
    if cpu:
        L += [f"System CPU ran {min(cpu):.0f}–{max(cpu):.0f}%, mean {sum(cpu)/len(cpu):.1f}%. "
              "Reported as a level, not fitted: at this sampling interval a per-thread CPU drift "
              "cannot be separated from burst noise, so a CPU figure here is context for a memory "
              "finding rather than a finding of its own.", ""]
    return "\n".join(L)


def _anomaly_section(flags: list[A.Anomaly]) -> str:
    """The rows worth a closer read, and where each one is in the logs.

    Rules, not judgement: each row names the rule that raised it. The pointer column is the point
    of the table — it is what an investigator opens, instead of opening the log.
    """
    if not flags:
        return ('<div class="empty">No row crossed a rule. That is a statement about these '
                "rules, not about the device.</div>")

    def where(f: A.Anomaly) -> str:
        parts = [H.code(x) for x in (f.component, f.state, f.action) if x]
        return " / ".join(parts) or '<span class="dash">&mdash;</span>'

    def ptr(f: A.Anomaly) -> str:
        if not f.pointers:
            return '<span class="dash">&mdash;</span>'
        p = f.pointers[0]
        out = f"EngineEvents:{p['engineLine']}"
        if p.get("dutLine"):
            out += f" · dut:{p['dutLine']}"
        more = f" +{len(f.pointers) - 1}" if len(f.pointers) > 1 else ""
        return H.code(out) + more

    rows = [[H.code(f.id), H.esc(f.kind), where(f), H.esc(f.figure),
             H.num(f.count) if f.pointers else '<span class="dash">&mdash;</span>', ptr(f)]
            for f in flags]
    kinds = sorted({f.kind for f in flags}, key=list(A.KINDS).index)
    note = " ".join(f"<b>{H.esc(k)}</b> &mdash; {H.esc(A.KINDS[k])}." for k in kinds)
    quiet = sum(1 for f in flags if f.kind != "signature")
    head = (f"<p>{len(flags)} flagged, {quiet} of them with no ErrorSignature behind them. "
            "Read one with <code>python3 -m pod_mbt.anomaly &lt;run&gt; --excerpt ANOM-NN</code>."
            "</p>")
    return head + H.table(["id", "kind", "where", "figure", "events", "first pointer"], rows,
                          right=[4], title="Flagged for investigation", note=note)


def skeleton(run_dir: Path, baseline: Optional[Path] = None) -> Report:
    """Title, Summary, the test-case figure, and every table whose numbers are computed.

    Everything here has one right answer, so none of it is an analyst's to produce. The figure
    is rendered from `CHAINS` for the same reason: a drawing derived from the model it depicts
    cannot disagree with it.
    """
    session = _load(run_dir, "session.json", {})
    coverage = _load(run_dir, "Analysis/CoverageReport.json", {})
    signatures = _load(run_dir, "Analysis/ErrorSignature.json", [])

    # Date and folder survive only in the directory name; see docs/pending_fixes.md B4.
    # The folder is named `<date>_<testCase>_<sessionId>`, so its own name is the fallback when
    # a folder predates the `date` field. Both spellings are accepted because older folders used
    # `<YYYYMMDD-HHMMSS>_<sessionId>`.
    # The folder is `<YYYY-MM-DD>_<testCase>_<HHMMSS>`, so its first ten characters are the
    # fallback when a folder predates the `date` field. Older folders spelled it
    # `<YYYYMMDD-HHMMSS>_<sessionId>`, which is why both are parsed.
    stamp = run_dir.name.split("_")[0]
    date = session.get("date") or (
        stamp[:10] if len(stamp) >= 10 and stamp[4] == "-"
        else f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else "unknown")
    # The report is about a test case; which plan produced this run is a Summary row, not the
    # title. `sessionId` stood in while nothing recorded the test case.
    case = session.get("testCase") or session.get("sessionId", run_dir.name)
    measured = _measured_elapsed(run_dir, session)
    planned = session.get("durationMs") or 0
    reason, reason_src = _stop_reason(session, measured)
    crashed = session.get("result", {}).get("crashed")

    # Draw the test case this run actually walked, not whatever is currently in `policy.py`.
    # A report whose figure comes from today's source rather than from the run's own model is
    # the Slide 01 problem with an extra step: it would look right and be wrong the moment a
    # chain changed.
    chains, faults = CHAINS, FAULTS
    tc_name = session.get("testCase")
    if tc_name:
        tc_path = Path("testcases") / tc_name / "model.json"
        if tc_path.is_file():
            _id, chains, faults = load_test_case(tc_path)
    # Issued, not accepted: a call the device refused was still made, and the guard judging it is
    # the test. Counting only accepted calls would report a correctly-rejected action as untested.
    issued = {f["actionName"] for f in _load(run_dir, "Log/ActionSchedule.json", [])
              if f.get("actionName")}
    scheduled = {f["actionName"] for f in _load(run_dir, "Log/ActionSchedule.json", [])
                 if f.get("actionName") in set(faults)} or None
    svg = chain_figure(chains, faults, scheduled,
                       executor_of={a.name: a.executor.value for a in ACTION_DB},
                       component_of={a.name: a.component for a in ACTION_DB})

    # A baseline is a previous run folder the TE hands over — the same test case on the same DUT
    # at a different firmware is the comparison worth making, and only a person knows which run
    # that was. Nothing searches for it: `test_result/` has no index, and a report that guessed
    # its own comparison would be asserting a claim nobody made.
    base_name, base_note, base_cov = "", "", {}
    if baseline is not None:
        bs = _load(baseline, "session.json", {})
        base_cov = _load(baseline, "Analysis/CoverageReport.json", {})
        base_name = bs.get("runFolder") or Path(baseline).name
        parts = [f"DUT {bs['dut']}" if bs.get("dut") else "",
                 f"release {bs['releaseVersion']}" if bs.get("releaseVersion") else "",
                 f"model {bs['modelVersion'][:8]}" if bs.get("modelVersion") else ""]
        base_note = " · ".join(x for x in parts if x)
        if bs.get("testCase") and tc_name and bs["testCase"] != tc_name:
            base_note += f" — different test case ({bs['testCase']}), so the tables below " \
                         "compare things that were never the same measurement"

    crashed = session.get("result", {}).get("crashed")
    tone_crash = "bad" if crashed else "ok"

    NA = '<span class="v none">N/A</span>'
    summary = "".join([
        _cell("Test Crash", "YES" if crashed else "NO", tone_crash),
        _cell("Test Duration", H.esc(_hm(measured)) if measured else "N/A",
              "" if measured else "none"),
        _cell("Test End Reason", H.esc(reason)),
        _cell("Test Case", H.code(tc_name) if tc_name else "N/A", "" if tc_name else "none"),
        _cell("Model Version", H.code(session.get("modelVersion", "?"))),
        _cell("DUT", H.code(session["dut"]) if session.get("dut") else "N/A",
              "" if session.get("dut") else "none"),
        _cell("Release Version",
              H.code(session["releaseVersion"]) if session.get("releaseVersion") else "N/A",
              "" if session.get("releaseVersion") else "none"),
        _cell("Driver Version",
              H.code(session.get("driverVersion") or session.get("driver") or "N/A")),
        _cell("Testing Folder", H.code(session.get("runFolder") or run_dir.name)),
        _cell("Compared With", H.code(base_name) if base_name else "N/A",
              "" if base_name else "none"),
    ])

    blank = lambda what: f'<div class="empty">{what}</div>'
    flags = A.find(run_dir, coverage, signatures)

    # `(title, body)`, not a rendered section: the analysis slot goes *inside* its section, so
    # the section cannot be assembled until an analyst has filled it.
    sections: dict[str, Any] = {
        "__title__": f"Test Report of {date} {case}",
        "summary": ("Summary", f'<div class="summary">{summary}</div>'),
        "testcase": ("Test case", f'<div class="fig"><div class="scroll">{svg}</div></div>'),
        # Renders only when the run crashed.
        "crash": ("Crash", blank("Not yet built.")) if crashed else None,
        "coverage": ("Coverage", _coverage_section(coverage, base_cov, chains, issued)),
        "execution": ("Execution result", blank("Not yet converted.")),
        "monitor": ("System monitor", blank("Not yet converted.")),
        "failures": ("Failures", blank("Not yet converted.")),
        "anomalies": ("Anomalies", _anomaly_section(flags)),
        "proposals": ("Proposals", blank("Not yet converted.")),
    }
    return Report(sections=sections, assets={"testcase.svg": svg},
                  facts={"session": session, "coverage": coverage, "signatures": signatures,
                         "baseline": base_name, "baselineCoverage": base_cov,
                         "anomalies": flags,
                         "measuredElapsedMs": measured, "stopReason": reason,
                         "modelVersion": session.get("modelVersion")})


# ------------------------------------------------------------------ the judged part
class RuleBasedAnalyst:
    """No model behind it, on purpose — the same reason `RuleBasedPlanner` exists in `plan.py`.

    It fills the same slots an LLM analyst would, from rules. Every signature gets exactly one
    reading, decided by its `trigger_type`: `illegal_call_accepted` is the ambiguous one (guard
    too strict, or the device is missing a check) and becomes a `model_correction` proposal for a
    person to judge; everything else clustered at all, which already means it is worth keeping as
    a regression, becomes a `regression` proposal carrying the reproduction itself.

    Every flag gets a finding, decided by its kind and by who reached it — never by reading the
    excerpt, which is the step a model is for. A flag with a cell to aim at becomes a
    `targeted_run` proposal: the next plan probes that state with that action, once a person
    accepts it.
    """

    def analyze(self, run_dir: Path, baseline: Optional[Path] = None) -> Analysis:
        rep = skeleton(run_dir, baseline)
        signatures = rep.facts["signatures"]
        coverage = rep.facts["coverage"]
        base_version = rep.facts["modelVersion"]

        flags: list[A.Anomaly] = rep.facts["anomalies"]
        findings = [self.investigate(f, flags) for f in flags]

        slots = {
            "anomalies.analysis": self._read_anomalies(findings),
            "coverage.analysis": self._read_coverage(coverage),
            "execution.analysis": self._read_execution(coverage),
            "monitor.analysis": "",
            "failures.analysis": self._read_failures(signatures),
            "proposals.analysis": "<p>One proposal per signature, each pending a person's decision. "
                         "A rule cannot weigh them against each other; that ordering is the "
                         "reading a model-backed analyst is here to give.</p>",
        }
        if rep.facts["session"].get("result", {}).get("crashed"):
            slots["crash.analysis"] = ""

        proposals: list[dict[str, Any]] = []
        for sig in signatures:
            if sig["trigger_type"] == "illegal_call_accepted":
                proposals.append({
                    "id": f"PROP-{sig['signature_hash']}",
                    "type": "model_correction",
                    "baseVersion": base_version,
                    "target": {"file": "pod_mbt/model.py",
                               "key": f"{sig['bug_bucket']}.guard"},
                    "current": "guard unchanged",
                    "proposed": "relax or tighten — needs a person's read, not this tool's",
                    "evidence": {"signatureHash": sig["signature_hash"],
                                 "occurrenceCount": sig["occurrence_count"]},
                    "rationale": f"`{sig['bug_bucket']}` was accepted "
                                 f"{sig['occurrence_count']}x when its guard said it shouldn't be.",
                    "status": "pending",
                })
            else:
                proposals.append({
                    "id": f"PROP-{sig['signature_hash']}",
                    "type": "regression",
                    "baseVersion": base_version,
                    "target": {"file": "testcases/TC-01/regressions.json", "key": "replay"},
                    "current": [],
                    "proposed": sig["reproduction"],
                    "evidence": {"signatureHash": sig["signature_hash"]},
                    "rationale": f"`{sig['trigger_type']}` on `{sig['bug_bucket']}`, seen "
                                 f"{sig['occurrence_count']}x — worth keeping as a permanent "
                                 "regression.",
                    "status": "pending",
                })
        for f in flags:
            p = self._target(f, base_version)
            if p:
                proposals.append(p)
        return Analysis(sections=rep.sections, slots=slots, assets=rep.assets,
                        proposals=proposals, findings=findings)

    @staticmethod
    def investigate(f: A.Anomaly, flags: list[A.Anomaly]) -> dict[str, Any]:
        """One flag's finding, from its kind and its pointers' metadata.

        A real analyst reads `anomaly.excerpt(run_dir, f)` and writes the same envelope. This one
        reaches a verdict only where the metadata alone decides it, and otherwise names the
        candidates and the step that would choose between them.
        """
        workers = sorted({p["worker"] for p in f.pointers if p.get("worker")})
        # The same action raised as a signature and as an edge is one behaviour seen twice: the
        # signature says it failed, the edge says from which state.
        related = sorted(g.id for g in flags if g is not f and g.action and g.action == f.action
                         and "signature" in (g.kind, f.kind))
        verdict, candidates, decided, reading = "inconclusive", [], "", ""
        if f.kind == "signature" and f.figure.startswith("illegal_call_accepted"):
            candidates = ["device_bug", "model_error"]
            decided = "replay the reproduction against this firmware; reproduced is device_bug, "                       "and a guard the device owner argues is too strict is model_error"
            reading = "the device acted on a call its guard says it must refuse"
        elif f.kind == "signature":
            candidates = ["device_bug", "environment"]
            decided = "replay on a second bench; reproducing on both rules out environment"
            reading = "a legal call did not reach its postcondition"
        elif f.kind == "unexpected_edge" and workers and set(workers) <= {"fault-clock"}:
            # Only the fault schedule reached this cell, so the walk's chain never had to say
            # what happens there — the model has no row for where a fault leaves the device.
            verdict, candidates = "model_gap", ["model_gap"]
            reading = "reached only by the fault clock: the chain has no row for the state a "                       "fault leaves behind"
        elif f.kind == "unexpected_edge":
            candidates = ["device_bug", "model_error"]
            decided = "read the excerpt: guard not held and the device moved anyway is the "                       "related signature seen from its state; guard held and it landed elsewhere "                       "is the chain's destination being wrong"
            reading = "the device left this state for one the chain does not predict"
        elif f.kind == "near_budget":
            candidates = ["device_bug", "model_error"]
            decided = "compare against a baseline run on the previous firmware: slower there "                       "too is a tight budget, slower only here is a regression"
            reading = "passing, but close enough to its budget that it will not stay passing"
        elif f.kind == "resource_growth":
            candidates = ["device_bug"]
            decided = "a soak at twice this duration: still climbing at the end is a leak, "                       "levelling off is a bounded cache"
            reading = "still climbing when the run ended"
        elif f.kind == "cleanup_lapsed":
            verdict, candidates = "config_gap", ["config_gap"]
            reading = "the harness never restored the device, so every figure after this "                       "point was measured on a perturbed device"
        return {"anomaly": f.id, "verdict": verdict, "candidates": candidates,
                "reading": reading, "decidedBy": decided, "related": related,
                "evidence": {"engineLines": [p["engineLine"] for p in f.pointers],
                             "dutLines": [p["dutLine"] for p in f.pointers if p.get("dutLine")],
                             "workers": workers},
                "confirmedBy": None, "by": "RuleBasedAnalyst"}

    @staticmethod
    def _target(f: A.Anomaly, base_version: Optional[str]) -> Optional[dict[str, Any]]:
        """A `targeted_run` for a flag that names a cell: probe that state with that action.

        Signatures already carry a reproduction and get their proposal above; a resource series
        and a lapsed cleanup name no cell to aim at, so their next step lives in the finding.
        """
        if f.kind not in ("unexpected_edge", "near_budget") or not (f.component and f.action):
            return None
        key = f"{f.kind}:{f.component}:{f.state}:{f.action}"
        return {
            "id": f"PROP-{hashlib.sha1(key.encode()).hexdigest()[:8]}",
            "type": "targeted_run",
            "baseVersion": base_version,
            "target": {"file": "next_run.json", "key": "probe"},
            "current": None,
            "proposed": {"component": f.component, "state": f.state, "actions": [f.action]},
            "evidence": {"anomaly": f.id, "events": f.count,
                         "engineLines": [p["engineLine"] for p in f.pointers]},
            "rationale": f"{f.id}: {f.figure}. A probe aimed at this cell re-runs it on purpose "
                         "rather than waiting for the walk to land there.",
            "status": "pending",
        }

    @staticmethod
    def _read_anomalies(findings: list[dict[str, Any]]) -> str:
        if not findings:
            return ""
        rows = []
        for f in findings:
            v = f["verdict"] if f["verdict"] != "inconclusive" else \
                "inconclusive: " + " or ".join(f["candidates"])
            then = f" Decided by: {H.esc(f['decidedBy'])}." if f["decidedBy"] else ""
            rel = f" Same behaviour as {', '.join(f['related'])}." if f["related"] else ""
            rows.append(f"<li><code>{f['anomaly']}</code> <b>{H.esc(v)}</b> &mdash; "
                        f"{H.esc(f['reading'])}.{rel}{then}</li>")
        settled = sum(1 for f in findings if f["verdict"] != "inconclusive")
        return (f"<p>{settled} of {len(findings)} settled from metadata alone. The rest need "
                "their excerpt read, and no finding here is <code>device_bug</code>: that takes "
                "a replay, a soak or a baseline, not a reading.</p><ul>" + "".join(rows) + "</ul>")

    @staticmethod
    def _read_coverage(c: dict) -> str:
        """What the coverage numbers mean, as far as a rule can say.

        The one reading a rule can defend is the gap between the two denominators: reaching almost
        everything a plan can reach is a different situation from reaching half the model, and they
        call for different actions — change the plan, or chase a guard.
        """
        tried, ceil, total = (c.get("pairs_tried", 0), c.get("plan_ceiling_pairs", 0),
                              c.get("pairs_total", 0))
        if not (tried and total):
            return ""
        out = []
        if ceil:
            out.append(f"<p>The same {tried} cells are {tried / total:.1%} of the model and "
                       f"{tried / ceil:.1%} of what this plan could ever reach. The "
                       f"{total - ceil} cells outside the ceiling belong to the configuration "
                       "and are closed by adding a worker, not by running longer; the shortfall "
                       "against the ceiling is the part to chase in a guard or in the walk.</p>")
        # A composite-state paragraph used to sit here, reading `composite_states_visited` against
        # `composite_states_legal`. Both halves were wrong. The denominator was the product of the
        # `CompositeState` enums (115), which counts six state values no chain declares, so a run
        # that reached every combination the test case defines still read as 31% — a 69% gap that
        # did not exist, and the paragraph then explained it. The numerator was described as
        # "every component is individually saturated", asserted rather than checked: a probe-only
        # plan leaves `bt` at 7 of 9 edges after 195,220 actions. Nothing goes here until the
        # denominator comes from the chains.
        un = c.get("uncounted_states") or {}
        if un:
            names = ", ".join(f"<code>{comp}</code> {', '.join(v)}"
                              for comp, v in sorted(un.items()))
            out.append(f"<p>Not counted in any denominator above: {names}. These are the "
                       "transient windows no chain row describes — not 0%, not 100%, uncounted — "
                       "and they are where a fault landing mid-transition would find a bug.</p>")
        return "".join(out)

    @staticmethod
    def _read_execution(c: dict) -> str:
        """The one column of the worker table that carries a judgement."""
        pw = c.get("per_worker") or {}
        flat = [w for w, d in pw.items() if d["total"] > 100 and d["guardFailed"] == 0]
        if not flat:
            return ""
        return ("<p>" + ", ".join(f"<code>{w}</code>" for w in sorted(flat))
                + " issued no call its own guard expected to be refused. That component's "
                  "rejection behaviour — what the device does when told to do something it must "
                  "not — went untested by the walk, however many calls it made.</p>")

    @staticmethod
    def _read_failures(signatures: list[dict[str, Any]]) -> str:
        if not signatures:
            return "<p>No signatures were raised. That is a statement about what this plan "\
                   "reached, not about the device.</p>"
        kinds = {s["trigger_type"] for s in signatures}
        out = []
        if "illegal_call_accepted" in kinds:
            out.append("<p><code>illegal_call_accepted</code> has two readings and a rule cannot "
                       "choose between them: the guard is stricter than the device needs, or the "
                       "device is missing a check. Each becomes a proposal for a person.</p>")
        if kinds - {"illegal_call_accepted"}:
            out.append("<p>Everything else clustered at all is worth keeping as a permanent "
                       "regression, so each carries its own reproduction.</p>")
        return "".join(out)


def write_back(run_dir: Path, analysis: Analysis) -> None:
    """Assemble the sections into `report.html`, write the assets beside it, one file per proposal.

    Section numbers are assigned here, over whatever sections actually rendered, so a crash
    section appearing pushes every number below it down without any section knowing it happened.

    Slot keys are checked against `SLOTS` and proposal ids against an allowlist before anything is
    written: an id goes straight into a filename, and once a model is producing ids, `..` in one of
    them writes outside the folder.
    """
    unknown = set(analysis.slots) - set(SLOTS)
    if unknown:
        raise ValueError(f"analyst wrote outside its slots: {sorted(unknown)}")
    for f in analysis.findings:
        if f.get("verdict") not in VERDICTS:
            raise ValueError(f"{f.get('anomaly')}: verdict {f.get('verdict')!r} is not one of "
                             f"{sorted(VERDICTS)}")
        conf = f.get("confirmedBy") or {}
        if f["verdict"] == "device_bug" and conf.get("kind") not in CONFIRMATIONS:
            raise ValueError(f"{f.get('anomaly')}: device_bug needs confirmedBy.kind in "
                             f"{CONFIRMATIONS} — a reading makes a bug plausible, not confirmed")

    out = run_dir / "Analysis"
    out.mkdir(parents=True, exist_ok=True)
    for rel, body in analysis.assets.items():
        if "/" in rel or rel.startswith("."):
            raise ValueError(f"asset path not allowed: {rel!r}")
        (out / rel).write_text(body)

    numbered, n = [], 0
    for key in SECTIONS:
        part = analysis.sections.get(key)
        if not part:
            continue
        n += 1
        title, body = part
        numbered.append(H.section(n, title, body,
                                  analysis=analysis.slots.get(f"{key}.analysis", "")))

    title = analysis.sections.get("__title__", "Test Report")
    head = (f'<header><div class="eyebrow">AI-Driven MBT</div>'
            f"<h1>{H.esc(title)}</h1></header>")
    foot = ("<footer><div>Every figure above is counting or set arithmetic over this run's own "
            "files, computed in Python. A section marked as written by the analyst is the only "
            "place a reading appears, and nothing in it changes a number.</div></footer>")
    (out / "report.html").write_text(H.document(title, [head] + numbered, foot))

    prop_dir = out / "proposals"
    prop_dir.mkdir(parents=True, exist_ok=True)
    for p in analysis.proposals:
        pid = str(p.get("id", ""))
        if not pid or not all(c.isalnum() or c in "-_." for c in pid) or pid.startswith("."):
            raise ValueError(f"proposal id not allowed as a filename: {pid!r}")
        (prop_dir / f"{pid}.json").write_text(json.dumps(p, indent=2) + "\n")
    index = [{"id": p["id"], "type": p["type"], "status": p["status"]}
             for p in analysis.proposals]
    (prop_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    (out / "findings.json").write_text(json.dumps(analysis.findings, indent=2,
                                                  ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read a finished run folder, write report.md + testcase.svg + proposals/.")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--baseline", type=Path, metavar="RUN_DIR",
                    help="a previous run folder to compare against — the same test case on the "
                         "same DUT at a different firmware is the comparison worth making, and "
                         "only you know which run that was")
    a = ap.parse_args(argv)

    analysis = RuleBasedAnalyst().analyze(a.run_dir, a.baseline)
    write_back(a.run_dir, analysis)
    print(f"\n  report         {a.run_dir / 'Analysis' / 'report.html'}")
    print(f"  figure         {a.run_dir / 'Analysis' / 'testcase.svg'}")
    filled = len([v for v in analysis.slots.values() if v])
    print(f"  sections       {len([k for k in SECTIONS if analysis.sections.get(k)])} rendered, "
          f"{filled}/{len(SLOTS)} analysis slots written")
    print(f"  findings       {len(analysis.findings)} flag(s) read, "
          f"{sum(1 for f in analysis.findings if f['verdict'] != 'inconclusive')} settled")
    print(f"  proposals      {len(analysis.proposals)} written to "
          f"{a.run_dir / 'Analysis' / 'proposals'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
