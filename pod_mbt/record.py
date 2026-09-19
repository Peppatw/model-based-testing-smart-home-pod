"""Recording, coverage, and error clustering.

Concurrent workers' LogEntries interleave into one TestSession because they are ordered
by `host_epoch_ns`, not by which loop produced them — a mechanism built for replay that
turns out to be exactly what merging concurrent streams needs.
"""
from __future__ import annotations

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
    # concurrent, so worker and offset have to survive the distillation.
    reproduction: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CoverageReport:
    """Two denominators, because there are two questions.

    A state-led run walks transitions the chain describes, so its denominator is the chain. An
    action-led run fires actions at states the chain never mentions, so its denominator is
    every (state, action) pair — a far bigger number, and the one that says how much of
    the device nobody has tried. Reported per component, because a two-state chain
    saturates while a five-state one is still half unseen.
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
    # Share of turns spent in each state, per component. Compared against the chain's
    # stationary distribution, this is what catches a device that got stuck without any
    # single step failing.
    observed_distribution: dict[str, dict[str, float]] = field(default_factory=dict)
    # The same thing counted per turn rather than per second. A chain's stationary
    # distribution is step-weighted, so this is the one to compare it against; the
    # time-weighted view above answers the different question of where the hours went.
    observed_step_distribution: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def transition_percentage(self) -> float:
        return 100.0 * self.transitions_walked / self.transitions_total if self.transitions_total else 0.0

    @property
    def pair_percentage(self) -> float:
        return 100.0 * self.pairs_tried / self.pairs_total if self.pairs_total else 0.0


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
                reproduction: list[dict[str, Any]]) -> ErrorSignature:
        key = f"{trigger_type}:{bucket}"
        with self._lock:
            if key in self.signatures:
                self.signatures[key].occurrence_count += 1
            else:
                self.signatures[key] = ErrorSignature(
                    signature_hash=f"{abs(hash(key)) % (16 ** 8):08x}",
                    trigger_type=trigger_type,
                    bug_bucket=bucket,
                    reproduction=reproduction,
                )
            return self.signatures[key]

    def ordered(self) -> list[LogEntry]:
        """The merge that makes concurrency legible: one stream, real time order."""
        with self._lock:
            return sorted(self.entries, key=lambda e: e.host_epoch_ns)

    def schedule(self) -> list[dict[str, Any]]:
        """Every action this session issued, in one ActionSchedule.

        One format for both scheduler types. A sync worker's entries replay in order and
        an async worker's on its offset, but the file does not need to say which: the
        worker's own config does. Recording them the same way is what lets any slice of a
        run — not only a distilled failure window — be handed straight back to Replay.
        """
        triggers = [e for e in self.ordered()
                    if e.event_type == EventType.ACTION_TRIGGER.value]
        t0 = triggers[0].details.get("t_ms", 0) if triggers else 0
        return [{"workerId": e.source, "actionName": e.details["action"],
                 "scheduledOffsetMs": e.details.get("t_ms", 0) - t0,
                 "guardHeld": e.details.get("guard_held")}
                for e in triggers]

    def save_run(self, root: str | Path, session: dict[str, Any],
                 coverage: Optional[CoverageReport] = None,
                 device_log: Optional[list[str]] = None) -> Path:
        """Write one run folder: Log/ is what the run produced, Analysis/ what was derived.

        The folder *is* the TestSession. Nothing here is a summary of something held
        elsewhere — a reproduction points into `Log/ActionSchedule.json` by offset, and
        `session.json` carries what it takes to line the files up again: the seed, the
        model version, and the host-to-device clock offset without which `DeviceLog`
        timestamps cannot be compared to anything else in the folder.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._t0 / 1e9))
        run = Path(root) / f"{stamp}_{self.session_id}"
        (run / "Log" / "DeviceLog").mkdir(parents=True, exist_ok=True)
        (run / "Analysis" / "proposals").mkdir(parents=True, exist_ok=True)

        def dump(rel: str, payload: Any) -> None:
            (run / rel).write_text(json.dumps(payload, indent=2))

        dump("session.json", session)
        dump("Log/ActionSchedule.json", self.schedule())

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

    def __init__(self, db, policy=None):
        self.db = db
        self.policy = policy
        self.visited_states: set[tuple] = set()
        self.transitions: set[tuple[str, str, str]] = set()      # (component, from, action)
        self.pairs: set[tuple[str, str, str]] = set()      # every pair actually tried
        self.accepted: set[str] = set()
        self.issued: set[str] = set()
        self.time_in_state: dict[str, dict[str, int]] = {}   # sampled on the clock
        self.steps_in_state: dict[str, dict[str, int]] = {}  # counted per turn taken
        self._lock = threading.Lock()

    def record(self, state: CompositeState, component: Optional[str] = None,
               from_state: Optional[str] = None, action_name: Optional[str] = None,
               accepted: bool = False) -> None:
        with self._lock:
            self.visited_states.add(state.as_tuple())
            if not action_name:
                return
            self.issued.add(action_name)
            if accepted:
                self.accepted.add(action_name)
            if component and from_state:
                self.pairs.add((component, from_state, action_name))
                self.steps_in_state.setdefault(component, {})
                self.steps_in_state[component][from_state] = \
                    self.steps_in_state[component].get(from_state, 0) + 1
                if self.policy and any(e.action == action_name
                                       for e in self.policy.row(component, from_state)):
                    self.transitions.add((component, from_state, action_name))

    def sample(self, state: CompositeState) -> None:
        """One tick of the background monitor: where is each component right now.

        Sampled on the clock rather than per action, because the question is what share of
        *time* the device spent in each state — an action-weighted count would say a fast
        state and a slow one were equally visited.
        """
        with self._lock:
            for comp in ("wifi", "bt", "matter"):
                here = state.chain_state(comp)
                if here:
                    self.time_in_state.setdefault(comp, {})
                    self.time_in_state[comp][here] = self.time_in_state[comp].get(here, 0) + 1

    def report(self) -> CoverageReport:
        with self._lock:
            all_names = {a.name for a in self.db}
            transitions_total = len(self.policy.transitions()) if self.policy else 0
            pairs_total = 0
            per_comp: dict[str, dict[str, int]] = {}
            if self.policy:
                for comp in self.policy.chains:
                    states = self.policy.states(comp)
                    acts = {a.name for a in self.db if a.component == comp}
                    pairs_total += len(states) * len(acts)
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
            dist, step_dist = share(self.time_in_state), share(self.steps_in_state)
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
                observed_distribution=dist,
                observed_step_distribution=step_dist,
            )
