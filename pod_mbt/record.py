"""Recording, coverage, and error clustering.

Concurrent workers' LogEntries interleave into one TestSession because they are ordered
by `host_epoch_ns`, not by which loop produced them — a mechanism built for replay that
turns out to be exactly what merging concurrent streams needs.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .model import Category, CompositeState, EventType


@dataclass
class LogEntry:
    timestamp: str
    host_epoch_ns: int
    source: str            # workerId — which loop produced this
    event_type: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ErrorSignature:
    signature_hash: str
    trigger_type: str      # postcondition_failure | postcondition_timeout |
                            # illegal_call_accepted | unexpected_destination
    bug_bucket: str
    occurrence_count: int = 1
    # An ActionSchedule, not a flat list: a concurrency bug's reproduction is itself
    # concurrent, so worker and offset have to survive the distillation. It holds inputs only —
    # the evidence below is what a replay is compared against, and keeping the two apart is what
    # lets the reproduction stay a runnable test case rather than a snapshot.
    reproduction: list[dict[str, Any]] = field(default_factory=list)
    # Where this slice sits in `Log/ActionSchedule.json`, in that file's own offsets. The
    # reproduction rebases its offsets to the window start, so without this the slice cannot be
    # located in the file it came from — and widening the window, which is the first thing anyone
    # wants when six actions are not enough, means re-running rather than re-reading.
    source_offset_ms: list[int] = field(default_factory=list)
    # The composite states the original run passed through over this window. A result, so it is
    # here rather than in the schedule. Replay compares its own path against it: if the paths
    # diverge the replay never reached the condition under test, and "not reproduced" then says
    # nothing about whether the bug is fixed.
    observed_path: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    """Three denominators, because there are three questions.

    A state-led run walks transitions the chain describes, so its denominator is the chain. An
    action-led run fires actions at states the chain never mentions, so its denominator is every
    `(state, action)` pair. And a plan can only ever reach a subset of those: 36 cells is 42.9% of
    a model with 84 and 97.3% of a configuration that could reach 37, and those two numbers ask a
    reader to do different things — change the plan, or chase a guard. Reporting one without the
    other is how "42.9%" comes to read as a run that underperformed.
    """

    transitions_walked: int
    transitions_total: int
    pairs_tried: int
    pairs_total: int
    actions_accepted: list[str]
    actions_never_accepted: list[str]
    # Which (component, state, action) cells were actually reached, not just how many.
    # The count answers "how much"; only the list answers "which ones are left", which
    # is what the next run's plan is built from.
    pairs: list[tuple[str, str, str]] = field(default_factory=list)
    refused_actions: list[str] = field(default_factory=list)
    per_component: dict[str, dict[str, int]] = field(default_factory=dict)
    # Share of turns spent in each state. A chain's stationary distribution counts turns, so this
    # is the view comparable to it, and comparing them is what catches a device that got stuck
    # without any single step failing. The wall-clock view this used to sit beside was removed:
    # nothing read it, and "stuck" is a time-series property a distribution cannot express — the
    # scalar that answers it is `dwell` below.
    observed_step_distribution: dict[str, dict[str, float]] = field(default_factory=dict)

    # --- how much of the state space, rather than of the graph -----------------------------
    # `CoverageTracker` visited these all along and the report threw them away. It is the most
    # honest figure here: every component can be individually saturated while the combinations
    # are a quarter covered, and a defect needing three regions at once is invisible per-axis.
    composite_states_visited: int = 0
    composite_states_legal: int = 0
    composite_states: list[tuple] = field(default_factory=list)

    # --- what this configuration could ever have reached -----------------------------------
    plan_ceiling_pairs: int = 0
    plan_ceiling_actions: int = 0
    plan_unreachable: list[tuple[str, str, str]] = field(default_factory=list)

    # --- the observed graph against the declared one ---------------------------------------
    # An edge the chain does not describe used to be dropped from coverage entirely — counted
    # neither as covered nor as anomalous. Four readings: it matched, it went somewhere else, it
    # is a fault (off-model by design), or the guard did not hold and the state moved anyway.
    edges_normal: list[tuple[str, str, str]] = field(default_factory=list)
    edges_wrong_destination: list[dict[str, Any]] = field(default_factory=list)
    edges_off_model_fault: list[tuple[str, str, str]] = field(default_factory=list)
    edges_anomaly: list[dict[str, Any]] = field(default_factory=list)

    # --- per state: how many visits, how much work per visit -------------------------------
    # `meanDwellSteps` of exactly 1.00 means the region never receives two consecutive calls in
    # that state — so a defect needing two is unreachable there, however long the run.
    per_state: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    dwell: dict[str, dict[str, int]] = field(default_factory=dict)

    # --- who issued what -------------------------------------------------------------------
    # Counted during the run, not recomputed by a reader: the alternative is scanning
    # `EngineEvents`, the largest file in the folder, for something the tracker already sees. The
    # guard split rides along because it is the same count with one extra condition, and it is
    # what shows a chain that only ever proposes calls it expects to succeed.
    per_worker: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- axes the model declares and coverage never reported -------------------------------
    per_executor: dict[str, dict[str, int]] = field(default_factory=dict)
    per_category: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- what the multi-worker design is for, measured ------------------------------------
    concurrency: dict[str, Any] = field(default_factory=dict)

    # --- did every fault's cleanup actually land -------------------------------------------
    # Counted from `CLEANUP_FIRED` / `CLEANUP_LAPSED`, never by action name: cleanup actions
    # double as ordinary API calls, so a name count says nothing. A lapsed cleanup leaves the
    # device perturbed for the rest of the run and contaminates every figure after it.
    cleanup: dict[str, Any] = field(default_factory=dict)

    # --- states no denominator above contains ---------------------------------------------
    # The transient windows no chain row describes. `chain_state()` returns None for them, so no
    # pair is recorded while the device is in one: the pair percentage is not 0% and not 100% for
    # that window, it is uncounted — and it is exactly where a fault landing mid-transition would
    # find a bug.
    uncounted_states: dict[str, list[str]] = field(default_factory=dict)

    @property
    def transition_percentage(self) -> float:
        return 100.0 * self.transitions_walked / self.transitions_total if self.transitions_total else 0.0

    @property
    def pair_percentage(self) -> float:
        return 100.0 * self.pairs_tried / self.pairs_total if self.pairs_total else 0.0

    @property
    def plan_percentage(self) -> float:
        return 100.0 * self.pairs_tried / self.plan_ceiling_pairs if self.plan_ceiling_pairs else 0.0

    @property
    def composite_percentage(self) -> float:
        return (100.0 * self.composite_states_visited / self.composite_states_legal
                if self.composite_states_legal else 0.0)


class TestRecorder:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.entries: list[LogEntry] = []
        self.signatures: dict[str, ErrorSignature] = {}
        self._lock = threading.Lock()
        self._t0 = time.time_ns()

    def log(self, source: str, event: EventType, **details) -> None:
        now = time.time_ns()
        entry = LogEntry(
            timestamp=time.strftime("%H:%M:%S", time.localtime(now / 1e9)),
            host_epoch_ns=now,
            source=source,
            event_type=event.value,
            details=details,
        )
        with self._lock:
            self.entries.append(entry)

    def cluster(self, trigger_type: str, bucket: str,
                reproduction: list[dict[str, Any]],
                source_offset_ms: Optional[list[int]] = None,
                observed_path: Optional[list[str]] = None) -> ErrorSignature:
        key = f"{trigger_type}:{bucket}"
        with self._lock:
            if key in self.signatures:
                self.signatures[key].occurrence_count += 1
            else:
                self.signatures[key] = ErrorSignature(
                    # blake2s, not `hash()`: Python randomises string hashing per process, so
                    # the same session re-run produced identical counts and identical
                    # reproductions under three different hashes. Proposals are named
                    # `PROP-{hash}`, so a regression referenced by id could not be found again
                    # and a signature could not be deduplicated against a previous run.
                    signature_hash=hashlib.blake2s(key.encode(), digest_size=4).hexdigest(),
                    trigger_type=trigger_type,
                    bug_bucket=bucket,
                    reproduction=reproduction,
                    source_offset_ms=source_offset_ms or [],
                    observed_path=observed_path or [],
                )
            return self.signatures[key]

    def ordered(self) -> list[LogEntry]:
        """The merge that makes concurrency legible: one stream, real time order."""
        with self._lock:
            return sorted(self.entries, key=lambda e: e.host_epoch_ns)

    def schedule(self, header: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Every action this session issued, in one ActionSchedule — inputs only.

        One format for a hand-authored test case, an AI-written plan and a distilled
        reproduction, which is what lets a bug found by a random run become a regression by
        selecting lines rather than by generating anything.

        **It pins what was issued, never what happened.** Results live in `EngineEvents`, and a
        replay whose results were handed to it is not a test: the whole point is that the same
        inputs against a changed device produce different results. So `guardHeld` is gone — it was
        an observation, nothing read it, and a replay recomputes the guard against the state it
        actually finds. `expectedState` stays, because it is what the *chain* said, not what the
        device did, and without it `unexpected_destination` cannot be judged at all.

        `seq` exists because offsets collide: in one 24-hour run seven offsets carried more than
        one entry, three of them at offset 0. The original ordered those by the event queue's
        insertion counter and a stable sort on the offset reorders them, which changes what state
        the second and third actions see.
        """
        triggers = [e for e in self.ordered()
                    if e.event_type == EventType.ACTION_TRIGGER.value]
        t0 = triggers[0].details.get("t_ms", 0) if triggers else 0
        out: list[dict[str, Any]] = [header] if header else []
        out += [{"seq": i,
                 "workerId": e.source,
                 "actionName": e.details["action"],
                 "scheduledOffsetMs": e.details.get("t_ms", 0) - t0,
                 **({"expectedState": e.details["expected"]}
                    if e.details.get("expected") else {})}
                for i, e in enumerate(triggers)]
        return out

    def save_run(self, root: str | Path, session: dict[str, Any],
                 coverage: Optional[CoverageReport] = None,
                 device_log: Optional[list[str]] = None,
                 initial_state: Optional[dict[str, Any]] = None) -> Path:
        """Write one run folder: Log/ is what the run produced, Analysis/ what was derived.

        The folder *is* the TestSession. Nothing here is a summary of something held
        elsewhere — a reproduction points into `Log/ActionSchedule.json` by offset, and
        `session.json` carries what it takes to line the files up again: the seed, the
        model version, and the host-to-device clock offset without which `DeviceLog`
        timestamps cannot be compared to anything else in the folder.
        """
        # `<YYYY-MM-DD>_<testCase>_<HHMMSS>` — the date, what was exercised, and when. The time
        # is always present rather than only on a collision: a suffix that appears sometimes makes
        # two folders in one listing look like different kinds of thing.
        #
        # The session id used to be in this name (`<stamp>_CHAOS-003`) and does not belong: it
        # names the *plan*, so a directory of runs of one test case announced six different things
        # and the test case nowhere. Which plan produced a run is a row in the report's Summary.
        t = time.localtime(self._t0 / 1e9)
        case = session.get("testCase") or "no-test-case"
        run = Path(root) / f"{time.strftime('%Y-%m-%d', t)}_{case}_{time.strftime('%H%M%S', t)}"
        (run / "Log" / "DeviceLog").mkdir(parents=True, exist_ok=True)
        (run / "Analysis" / "proposals").mkdir(parents=True, exist_ok=True)

        def dump(rel: str, payload: Any) -> None:
            (run / rel).write_text(json.dumps(payload, indent=2))

        # `runFolder` is written here because this is the only place that knows it. Without it
        # the folder's own name is the only record of where a report's numbers came from, and a
        # report copied anywhere else loses the link.
        dump("session.json", {**session, "runFolder": run.name})
        # The schedule's header: where the run began, what the action names meant, and how each
        # worker reads its offsets. Together they are what makes the file replayable on its own —
        # the seed deliberately stays out, because in Replay the RNG drives only the stand-in
        # device, and on a bench there is no seed at all.
        dump("Log/ActionSchedule.json", self.schedule(header={
            "initialState": initial_state,
            "modelVersion": session.get("modelVersion"),
            "workers": {w["workerId"]: w["kind"] for w in session.get("workers", [])},
        } if initial_state is not None else None))

        observations = {EventType.TELEMETRY.value}
        with (run / "Log" / "EngineEvents.jsonl").open("w") as fh:
            for e in self.ordered():
                if e.event_type in observations:
                    continue
                fh.write(json.dumps(asdict(e)) + "\n")
        dump("Log/ResourceSnapshot.json",
             [asdict(e) for e in self.ordered()
              if e.event_type == EventType.TELEMETRY.value])
        if device_log:
            (run / "Log" / "DeviceLog" / "dut.log").write_text("\n".join(device_log) + "\n")

        if coverage is not None:
            dump("Analysis/CoverageReport.json", asdict(coverage))
        dump("Analysis/ErrorSignature.json", [asdict(s) for s in self.signatures.values()])
        return run


class CoverageTracker:
    """Exact Python set arithmetic. No AI in the counting, in either kind of run."""

    def __init__(self, db, policy=None, workers=None):
        self.db = db
        self.policy = policy
        self.workers = list(workers or [])
        self.visited_states: set[tuple] = set()
        self.transitions: set[tuple[str, str, str]] = set()   # (component, from, action)
        self.pairs: set[tuple[str, str, str]] = set()          # every pair actually tried
        self.accepted: set[str] = set()
        self.issued: set[str] = set()
        self.issued_count: dict[str, int] = {}
        # worker -> {component -> calls}, plus how many of that worker's calls had a guard that
        # did not hold.
        self.by_worker: dict[str, dict[str, int]] = {}
        self.guard_failed: dict[str, int] = {}
        # What each worker reached on its own. A composite state is the whole device, so a worker
        # does not own one — but how many distinct ones were current while it acted says how much
        # variety of context it saw, which two workers issuing the same number of calls can differ
        # in enormously.
        self.worker_states: dict[str, set[str]] = {}
        self.worker_actions: dict[str, set[str]] = {}
        self.worker_pairs: dict[str, set[tuple[str, str, str]]] = {}
        self.worker_composites: dict[str, set[tuple]] = {}
        self.steps_in_state: dict[str, dict[str, int]] = {}    # counted per turn taken
        self.entered_state: dict[str, dict[str, int]] = {}     # counted per region change
        # Observed edges keyed by (component, from, action); the value is the destinations seen,
        # and whether the guard held on the way. A destination the chain did not name, or a move
        # on a call whose guard failed, is a finding the pair count cannot express.
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._last_region: dict[str, Optional[str]] = {}
        self._runs: dict[str, int] = {}          # consecutive steps with no change, per component
        self._longest_run: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, state: CompositeState, component: Optional[str] = None,
               from_state: Optional[str] = None, action_name: Optional[str] = None,
               accepted: bool = False, to_state: Optional[str] = None,
               guard_held: bool = True, expected: Optional[str] = None,
               worker_id: Optional[str] = None) -> None:
        with self._lock:
            self.visited_states.add(state.as_tuple())
            for comp in ("wifi", "bt", "matter"):
                here = state.chain_state(comp)
                was = self._last_region.get(comp, "\x00")
                if here is not None and here != was:
                    self.entered_state.setdefault(comp, {})
                    self.entered_state[comp][here] = self.entered_state[comp].get(here, 0) + 1
                    self._longest_run[comp] = max(self._longest_run.get(comp, 0),
                                                  self._runs.get(comp, 0))
                    self._runs[comp] = 0
                elif here is not None:
                    self._runs[comp] = self._runs.get(comp, 0) + 1
                if here is not None:
                    self._last_region[comp] = here

            if not action_name:
                return
            self.issued.add(action_name)
            self.issued_count[action_name] = self.issued_count.get(action_name, 0) + 1
            if worker_id:
                row = self.by_worker.setdefault(worker_id, {})
                key = component or "system"
                row[key] = row.get(key, 0) + 1
                if not guard_held:
                    self.guard_failed[worker_id] = self.guard_failed.get(worker_id, 0) + 1
                self.worker_actions.setdefault(worker_id, set()).add(action_name)
                self.worker_composites.setdefault(worker_id, set()).add(state.as_tuple())
                if component and from_state:
                    self.worker_states.setdefault(worker_id, set()).add(from_state)
                    self.worker_pairs.setdefault(worker_id, set()).add(
                        (component, from_state, action_name))
            if accepted:
                self.accepted.add(action_name)
            if component and from_state:
                self.pairs.add((component, from_state, action_name))
                self.steps_in_state.setdefault(component, {})
                self.steps_in_state[component][from_state] = \
                    self.steps_in_state[component].get(from_state, 0) + 1
                e = self.edges.setdefault((component, from_state, action_name),
                                          {"to": {}, "guard_failed_moves": 0, "expected": None})
                if to_state:
                    e["to"][to_state] = e["to"].get(to_state, 0) + 1
                if expected:
                    e["expected"] = expected
                if not guard_held and to_state and to_state != from_state:
                    e["guard_failed_moves"] += 1
                if self.policy and any(t.action == action_name
                                       for t in self.policy.row(component, from_state)):
                    self.transitions.add((component, from_state, action_name))

    def sample(self, state: CompositeState) -> None:
        """One tick of the background monitor. Kept for the resource series; the wall-clock state
        share it used to feed was removed, because nothing read it."""
        with self._lock:
            self.visited_states.add(state.as_tuple())

    # ------------------------------------------------------------------ derived
    def _ceiling_actions(self) -> set[str]:
        """Every action this configuration can issue at all.

        Not derivable from the pair ceiling: a pair needs a chain state to sit in, so an action
        whose component is `system` — `inject_cpu_stress` and its kin — forms no pair and was
        therefore missing from the ceiling while being fired all the same. That produced an
        "actions ever accepted" figure of 107% of its own ceiling, which is the kind of number
        that says the denominator is wrong rather than the measurement.
        """
        if not self.workers:
            return set()
        out: set[str] = set()
        cleanup_of = {a.name: a.cleanup_action for a in self.db if a.cleanup_action}
        for w in self.workers:
            kind = getattr(w.kind, "value", w.kind)
            if kind == "walk":
                if self.policy:
                    out |= {t.action for st in self.policy.states(w.component)
                            for t in self.policy.row(w.component, st)}
            elif kind in ("probe", "sweep"):
                out |= set(w.pool or [a.name for a in self.db if a.component == w.component])
            else:
                names = {t["actionName"] for t in w.schedule}
                out |= names | {cleanup_of[n] for n in names if n in cleanup_of}
        return out

    def _ceiling(self) -> set[tuple[str, str, str]]:
        """Every `(state, action)` cell this configuration could possibly reach.

        Static, from the worker configs: a walk worker can only issue its component's chain edges,
        a probe or sweep is its pool crossed with its component's states, a schedule worker is the
        actions it lists. Each fault's `cleanup_action` counts too — `PendingCleanup` bypasses the
        chain roll, so it reaches pairs the chain never pairs.
        """
        if not (self.policy and self.workers):
            return set()
        out: set[tuple[str, str, str]] = set()
        comp_of = {a.name: a.component for a in self.db}
        cleanup_of = {a.name: a.cleanup_action for a in self.db if a.cleanup_action}
        for w in self.workers:
            kind = getattr(w.kind, "value", w.kind)
            if kind == "walk":
                out |= {(w.component, st, t.action)
                        for st in self.policy.states(w.component)
                        for t in self.policy.row(w.component, st)}
            elif kind in ("probe", "sweep"):
                pool = w.pool or [a.name for a in self.db if a.component == w.component]
                out |= {(w.component, st, n) for st in self.policy.states(w.component)
                        for n in pool}
            else:
                names = [t["actionName"] for t in w.schedule]
                names += [cleanup_of[n] for n in names if n in cleanup_of]
                for n in names:
                    c = comp_of.get(n)
                    if c in self.policy.chains:
                        out |= {(c, st, n) for st in self.policy.states(c)}
        return out

    def report(self, rec: Optional["TestRecorder"] = None) -> CoverageReport:
        from .model import legal_composite_states
        with self._lock:
            all_names = {a.name for a in self.db}
            by_name = {a.name: a for a in self.db}
            transitions_total = len(self.policy.transitions()) if self.policy else 0
            pairs_total = 0
            per_comp: dict[str, dict[str, int]] = {}
            model_pairs: set[tuple[str, str, str]] = set()
            if self.policy:
                for comp in self.policy.chains:
                    states = self.policy.states(comp)
                    acts = {a.name for a in self.db if a.component == comp}
                    pairs_total += len(states) * len(acts)
                    model_pairs |= {(comp, st, n) for st in states for n in acts}
                    per_comp[comp] = {
                        "states": len(states),
                        "transitions": len([e for e in self.policy.transitions() if e[0] == comp]),
                        "transitionsWalked": len([e for e in self.transitions if e[0] == comp]),
                        "pairs": len(states) * len(acts),
                        "pairsTried": len([p for p in self.pairs if p[0] == comp]),
                    }

            def share(src):
                out = {}
                for comp, counts in src.items():
                    total = sum(counts.values()) or 1
                    out[comp] = {k: round(v / total, 4) for k, v in sorted(counts.items())}
                return out

            # --- the observed graph, four ways ---------------------------------------------
            fault_names = {a.name for a in self.db if a.category is Category.FAULT}
            normal, wrong, off_fault, anomaly = [], [], [], []
            for (comp, src, act), info in sorted(self.edges.items()):
                in_chain = bool(self.policy and any(
                    t.action == act for t in self.policy.row(comp, src)))
                expected = info["expected"] or (
                    next((t.nxt for t in self.policy.row(comp, src) if t.action == act), None)
                    if self.policy else None)
                bad = {d: n for d, n in info["to"].items() if expected and d != expected}
                if info["guard_failed_moves"]:
                    anomaly.append({"component": comp, "from": src, "action": act,
                                    "to": info["to"], "guardFailedMoves":
                                        info["guard_failed_moves"]})
                elif not in_chain:
                    (off_fault if act in fault_names else anomaly).append(
                        (comp, src, act) if act in fault_names
                        else {"component": comp, "from": src, "action": act, "to": info["to"],
                              "note": "no chain row describes this edge"})
                elif bad:
                    wrong.append({"component": comp, "from": src, "action": act,
                                  "chainExpected": expected, "reached": bad})
                else:
                    normal.append((comp, src, act))

            # --- per state: visits, work per visit ----------------------------------------
            per_state: dict[str, dict[str, dict[str, float]]] = {}
            for comp, counts in self.steps_in_state.items():
                per_state[comp] = {}
                for st, acts in sorted(counts.items()):
                    ent = self.entered_state.get(comp, {}).get(st, 0)
                    per_state[comp][st] = {
                        "actionsFrom": acts, "entered": ent,
                        "meanDwellSteps": round(acts / ent, 2) if ent else 0.0}

            # --- axes the model declares --------------------------------------------------
            per_executor: dict[str, dict[str, int]] = {}
            for a in self.db:
                k = a.executor.value
                d = per_executor.setdefault(k, {"actions": 0, "issued": 0, "calls": 0})
                d["actions"] += 1
                if a.name in self.issued:
                    d["issued"] += 1
                    d["calls"] += self.issued_count.get(a.name, 0)
            per_category: dict[str, dict[str, Any]] = {}
            for a in self.db:
                k = a.category.value
                d = per_category.setdefault(k, {"actions": 0, "issued": 0, "neverIssued": []})
                d["actions"] += 1
                if a.name in self.issued:
                    d["issued"] += 1
                else:
                    d["neverIssued"].append(a.name)

            # --- states no denominator contains -------------------------------------------
            uncounted = {}
            if self.policy:
                named = {c: set(self.policy.states(c)) for c in self.policy.chains}
                probe = {"wifi": ["Disconnected", "Scanning", "Connecting", "Connected",
                                  "Streaming"],
                         "bt": ["Idle", "Advertising", "Pairing", "Connected"],
                         "matter": ["Uncommissioned", "Commissioning", "Operational",
                                    "Fabric_Removing"]}
                for c, all_st in probe.items():
                    missing = [st for st in all_st
                               if not any(st in n for n in named.get(c, set()))]
                    if missing:
                        uncounted[c] = missing

            per_worker = {}
            for w, row in self.by_worker.items():
                total = sum(row.values())
                gf = self.guard_failed.get(w, 0)
                per_worker[w] = {"byComponent": dict(sorted(row.items())), "total": total,
                                 "guardFailed": gf,
                                 "guardFailedShare": round(gf / total, 4) if total else 0.0,
                                 "states": len(self.worker_states.get(w, ())),
                                 "actions": len(self.worker_actions.get(w, ())),
                                 "pairs": len(self.worker_pairs.get(w, ())),
                                 "compositeStates": len(self.worker_composites.get(w, ()))}

            ceiling = self._ceiling()
            legal = legal_composite_states()
            return CoverageReport(
                transitions_walked=len(self.transitions),
                transitions_total=transitions_total,
                pairs_tried=len(self.pairs),
                pairs_total=pairs_total,
                actions_accepted=sorted(self.accepted),
                actions_never_accepted=sorted(all_names - self.accepted),
                pairs=sorted(self.pairs),
                refused_actions=sorted(self.issued - self.accepted),
                per_component=per_comp,
                observed_step_distribution=share(self.steps_in_state),
                composite_states_visited=len(self.visited_states),
                composite_states_legal=len(legal),
                composite_states=sorted(self.visited_states),
                plan_ceiling_pairs=len(ceiling),
                plan_ceiling_actions=len(self._ceiling_actions()),
                plan_unreachable=sorted(model_pairs - ceiling) if ceiling else [],
                edges_normal=normal,
                edges_wrong_destination=wrong,
                edges_off_model_fault=off_fault,
                edges_anomaly=anomaly,
                per_state=per_state,
                dwell={c: {"longestRunWithoutChange": v}
                       for c, v in sorted(self._longest_run.items())},
                per_worker=per_worker,
                per_executor=per_executor,
                per_category=per_category,
                concurrency=concurrency(rec) if rec else {},
                cleanup=cleanups(rec) if rec else {},
                uncounted_states=uncounted,
            )


def cleanups(rec: "TestRecorder") -> dict[str, Any]:
    """Which faults got their cleanup back, and which did not."""
    fired, lapsed = [], []
    for e in rec.ordered():
        if e.event_type == EventType.CLEANUP_FIRED.value:
            fired.append({"after": e.details.get("after"), "action": e.details.get("action")})
        elif e.event_type == EventType.CLEANUP_LAPSED.value:
            lapsed.append({"after": e.details.get("after"), "action": e.details.get("action"),
                           "deadlineStep": e.details.get("deadline_step"),
                           "reachedStep": e.details.get("step")})
    return {"scheduled": len(fired) + len(lapsed), "fired": len(fired),
            "lapsed": len(lapsed), "lapsedDetail": lapsed}


def concurrency(rec: "TestRecorder") -> dict[str, Any]:
    """How often one worker acted while another's action was still in flight.

    The whole reason there is more than one worker, and nothing measured it. A flight window is a
    trigger paired with its oracle result; an action is concurrent if any other worker's trigger
    falls inside that window. If this number is near zero, a multi-worker run was a single-threaded
    walk and the design bought nothing.
    """
    import bisect
    flights: list[tuple[int, int, str]] = []
    pending: dict[str, tuple[int, str]] = {}
    for e in rec.ordered():
        d = e.details
        if e.event_type == EventType.ACTION_TRIGGER.value:
            pending[e.source] = (d.get("t_ms", 0), d.get("action", ""))
        elif e.event_type == EventType.ORACLE_RESULT.value and e.source in pending:
            t0, _ = pending.pop(e.source)
            flights.append((t0, t0 + d.get("elapsed_ms", 0), e.source))
    if not flights:
        return {}
    flights.sort()
    starts = [f[0] for f in flights]
    solo = overlapped = 0
    pairs: dict[str, int] = {}
    for t0, t1, who in flights:
        hits = 0
        j = bisect.bisect_left(starts, t0)
        while j < len(flights) and flights[j][0] < t1:
            if flights[j][2] != who:
                hits += 1
                key = " + ".join(sorted((who, flights[j][2])))
                pairs[key] = pairs.get(key, 0) + 1
            j += 1
        if hits:
            overlapped += 1
        else:
            solo += 1
    total = solo + overlapped
    return {"actions": total, "overlapping": overlapped, "solo": solo,
            "overlappingShare": round(overlapped / total, 4) if total else 0.0,
            "byPair": dict(sorted(pairs.items(), key=lambda kv: -kv[1]))}
