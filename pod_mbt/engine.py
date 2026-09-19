"""EngineLoop — one worker taking one turn at a time, in one of three ways.

A worker is not configured with a "mode"; it is configured with a **kind**, and the kind
is decided by which part of the plan it came from:

* **walk** — state-led. Reads this component's current state, rolls that state's row in
  the `MarkovPolicy`, and takes the transition it lands on. The model chooses and the model
  judges. This is the kind a chaos run is built from: a realistic walk, there to be
  perturbed.
* **probe** — action-led. Ignores the chain and fires an action from its pool at whatever
  state the device happens to be in. Chance chooses; the model still judges, because the
  guard says whether this call should have worked — which is what makes a probe an
  assertion rather than noise. This is the kind that reaches the pairs no chain describes.
* **sweep** — both at once. Inside a state it calls only what it has not called there
  yet, crossing the state off action by action; when that list is empty it hands the turn
  to the chain and lets a declared transition carry it somewhere with cells still open.
  Off the flow to cover a state, on the flow to leave it — which is what makes it the only
  kind that reaches every state x action pair rather than a sample of them.
* **schedule** — a timed list: faults on the clock, and saved reproductions replayed. It
  reads neither state nor chain, which is exactly why it can land a fault while another
  worker's action is still in flight.

Concurrency is discrete-event simulation on a virtual clock, not threads: an 8-hour plan
finishes instantly and a run is reproducible from its seed, which is what makes a replay
mean anything. A real-hardware Driver runs these same loops on real threads.
"""
from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .model import (ACTION_DB, ActionDatabase, ActionDefinition, Category,
                    CompositeState, EventType, Power, Stress)
from .driver import Driver, SimulatedDriver
from .policy import MarkovPolicy
from .record import CoverageTracker, TestRecorder


class WorkerKind(str, Enum):
    WALK = "walk"           # state-led: the chain chooses
    PROBE = "probe"         # action-led: chance chooses, the model judges
    SWEEP = "sweep"         # both: exhaust a state, then use the chain to leave it
    SCHEDULE = "schedule"   # a clock chooses: faults, and replays


@dataclass
class WorkerConfig:
    worker_id: str
    kind: WorkerKind = WorkerKind.WALK
    component: str = "wifi"                                       # walk + probe
    avg_interval_ms: int = 240_000
    pool: list[str] = field(default_factory=list)                 # probe: what it may fire
    schedule: list[dict[str, Any]] = field(default_factory=list)  # schedule: when, what


@dataclass
class PendingCleanup:
    triggering_action: str
    cleanup_action: str
    deadline_step: int


class EngineLoop:
    """One worker. Everything it needs is its config plus the shared session."""

    def __init__(self, cfg: WorkerConfig, session: "Session"):
        self.cfg = cfg
        self.s = session
        self.step = 0
        self.cursor = 0                       # position in a schedule
        self.pending: list[PendingCleanup] = []
        self.untried: dict[str, set[str]] = {}   # sweep: what each state still owes

    # ------------------------------------------------------------- selection
    def _pool(self) -> list[str]:
        if self.cfg.pool:
            return self.cfg.pool
        return [a.name for a in self.s.db if a.component == self.cfg.component]

    def select(self, state: CompositeState
               ) -> tuple[Optional[ActionDefinition], Optional[str]]:
        """The action to issue, and where the chain expected it to land (walk only)."""
        for p in list(self.pending):          # a fault's cleanup outranks selection
            if self.step >= p.deadline_step:
                self.pending.remove(p)
                return self.s.db.get(p.cleanup_action), None

        if self.cfg.kind is WorkerKind.WALK:
            here = state.chain_state(self.cfg.component)
            if here is None:                  # mid-transition: no turn in this chain
                return None, None
            transition = self.s.policy.next_transition(self.cfg.component, here)
            return (self.s.db.get(transition.action), transition.nxt) if transition else (None, None)

        if self.cfg.kind is WorkerKind.PROBE:
            pool = self._pool()
            return (self.s.db.get(self.s.rng.choice(pool)), None) if pool else (None, None)

        if self.cfg.kind is WorkerKind.SWEEP:
            here = state.chain_state(self.cfg.component)
            if here is None:                  # mid-transition: no turn in this chain
                return None, None
            owed = self.untried.setdefault(here, set(self._pool()))
            if owed:
                name = self.s.rng.choice(sorted(owed))   # sorted: seed reproduces the order
                owed.discard(name)
                return self.s.db.get(name), None
            # Nothing left to try here. The chain is the way out, and unlike a random
            # draw it is guaranteed to be an action that moves.
            transition = self.s.policy.next_transition(self.cfg.component, here)
            return (self.s.db.get(transition.action), transition.nxt) if transition else (None, None)

        names = [t["actionName"] for t in self.cfg.schedule]
        if self.cursor >= len(names):
            return None, None
        name = names[self.cursor]
        self.cursor += 1
        return self.s.db.get(name), None

    # --------------------------------------------------------------- one lap
    def run_step(self, now_ms: int) -> int:
        state = self.s.state
        action, expected = self.select(state)
        if action is None:
            # A walker with no turn is waiting for the transition it sits inside to
            # resolve; a schedule that has run out is simply finished.
            return (now_ms + self._interval()
                    if self.cfg.kind in (WorkerKind.WALK, WorkerKind.SWEEP) else -1)

        self.step += 1
        src = self.cfg.worker_id
        guard_held = action.guard(state)
        before = state.chain_state(action.component)

        self.s.note_window(state)
        self.s.rec.log(src, EventType.ACTION_TRIGGER, action=action.name, step=self.step,
                       guard_held=guard_held, t_ms=now_ms, component=action.component,
                       **({"from_state": before} if before else {}),
                       **({"expected": expected} if expected else {}))

        new_state, result = self.s.driver.execute(action, state)
        elapsed = self._elapsed(action, state)
        self.s.state = new_state
        after = new_state.chain_state(action.component)
        if new_state.as_tuple() != state.as_tuple():
            self.s.rec.log(src, EventType.STATE_CHANGE, action=action.name,
                           to=new_state.pretty(),
                           **({"to_state": after} if after else {}))

        # --- which assertion applies ---------------------------------------
        timed_out = elapsed > action.postcondition_timeout_ms
        if guard_held:
            ok = action.postcondition(new_state) and not timed_out
            detail = action.postcondition_text
            trigger = "postcondition_timeout" if timed_out else "postcondition_failure"
        else:
            # The generic rule for a call the guard says should not have worked.
            ok = (not result.accepted) and new_state.as_tuple() == state.as_tuple() \
                 and not result.crashed
            detail = "state unchanged ∧ error returned ∧ no crash"
            trigger = "illegal_call_accepted"

        # The chain said where this transition lands. Arriving somewhere else is a finding a
        # postcondition can miss: the call worked, and the model is wrong about the graph.
        if expected and after and after != expected and guard_held:
            ok = False
            trigger = "unexpected_destination"
            detail = f"chain expected {expected}, reached {after}"

        self.s.rec.log(src, EventType.ORACLE_RESULT, action=action.name, passed=ok,
                       expected=detail, elapsed_ms=elapsed,
                       budget_ms=action.postcondition_timeout_ms,
                       under_stress=state.cpu_load is not Stress.NORMAL
                                    or state.mem_pressure is not Stress.NORMAL)
        if not ok:
            self.s.rec.cluster(trigger, action.name, self.s.reproduction_window())

        if action.category is Category.FAULT and action.cleanup_action:
            self.pending.append(PendingCleanup(action.name, action.cleanup_action,
                                               self.step + 4))
        self.s.coverage.record(new_state, action.component, before, action.name,
                               accepted=result.accepted)

        if self.cfg.kind is WorkerKind.SCHEDULE:
            self.s.rec.log(src, EventType.ASYNC_FIRED, action=action.name)
            nxt = (self.cfg.schedule[self.cursor].get("scheduledOffsetMs")
                   if self.cursor < len(self.cfg.schedule) else None)
            return nxt if nxt is not None else -1
        return now_ms + (elapsed if self.cfg.kind is WorkerKind.WALK else self._interval())

    def _elapsed(self, action: ActionDefinition, state: CompositeState) -> int:
        """Virtual duration, stretched by whatever pressure the device is under — which is
        what makes `inject_cpu_stress` mean something rather than flip a flag."""
        base = action.postcondition_timeout_ms
        load = {Stress.NORMAL: 1.0, Stress.ELEVATED: 1.8, Stress.CRITICAL: 3.2}
        factor = load[state.cpu_load] * (1.0 if state.mem_pressure is Stress.NORMAL else 1.4)
        return max(500, int(self.s.rng.uniform(0.15, 0.75) * base * factor))

    def _interval(self) -> int:
        return max(1000, int(self.s.rng.expovariate(1 / self.cfg.avg_interval_ms)))


class BackgroundMonitor:
    """Samples the device's resource state on a fixed interval, not per action.

    The one producer whose output is a time series rather than a consequence of a step: a
    leak shows up as a slope across a session, and a per-action record cannot show a slope.
    """

    def __init__(self, session: "Session", interval_ms: int = 30_000):
        self.s = session
        self.interval_ms = interval_ms

    def run_step(self, now_ms: int) -> int:
        st = self.s.state
        self.s.coverage.sample(st)
        self.s.rec.log("monitor", EventType.TELEMETRY, t_ms=now_ms,
                       cpu_load=st.cpu_load.value, mem_pressure=st.mem_pressure.value,
                       uplink=st.uplink.value, power=st.power.value,
                       wifi=st.chain_state("wifi"), bt=st.chain_state("bt"),
                       matter=st.chain_state("matter"))
        return now_ms + self.interval_ms


class Session:
    """Owns the shared state every worker reads and writes."""

    def __init__(self, session_id: str, workers: list[WorkerConfig],
                 duration_ms: int = 8 * 3600 * 1000, seed: int = 7,
                 db: ActionDatabase | None = None, driver: Driver | None = None,
                 policy: MarkovPolicy | None = None):
        self.session_id = session_id
        self.rng = random.Random(seed)
        self.db = db or ACTION_DB
        self.driver = driver or SimulatedDriver(rng=random.Random(seed + 1))
        self.policy = policy or MarkovPolicy(rng=self.rng)
        self.state = CompositeState()
        self.window_state = self.state
        self._recent: list[CompositeState] = []
        self.rec = TestRecorder(session_id)
        self.coverage = CoverageTracker(self.db, self.policy)
        self.duration_ms = duration_ms
        self.crashed = False
        self.loops = [EngineLoop(c, self) for c in workers]
        self.monitor = BackgroundMonitor(self)

    def note_window(self, state: CompositeState, n: int = 6) -> None:
        self._recent.append(state)
        if len(self._recent) > n:
            self._recent.pop(0)
        self.window_state = self._recent[0]

    def reproduction_window(self, n: int = 6) -> list[dict[str, Any]]:
        """The tail of the run, as a schedule Replay can take back.

        Worker and offset are kept because a concurrency bug reproduced as a flat list is
        not reproduced at all; the starting state is kept because replaying six actions
        from a cold boot lands somewhere else entirely.
        """
        triggers = [e for e in self.rec.ordered()
                    if e.event_type == EventType.ACTION_TRIGGER.value][-n:]
        if not triggers:
            return []
        t0 = triggers[0].details.get("t_ms", 0)
        out: list[dict[str, Any]] = [{"initialState": self.window_state.to_dict()}]
        out += [{"workerId": e.source, "actionName": e.details["action"],
                 "scheduledOffsetMs": e.details.get("t_ms", 0) - t0}
                for e in triggers]
        return out

    def run(self) -> None:
        counter = itertools.count()
        queue: list[tuple[int, int, Any]] = []
        for loop in self.loops:
            start = 0
            if loop.cfg.kind is WorkerKind.SCHEDULE and loop.cfg.schedule:
                start = loop.cfg.schedule[0].get("scheduledOffsetMs", 0)
            heapq.heappush(queue, (start, next(counter), loop))
        heapq.heappush(queue, (0, next(counter), self.monitor))

        self.coverage.record(self.state)
        while queue:
            now, _, loop = heapq.heappop(queue)
            if now >= self.duration_ms:
                break
            nxt = loop.run_step(now)
            if self.state.power is Power.OFF or self.crashed:
                break
            if nxt >= 0:
                heapq.heappush(queue, (max(nxt, now + 1), next(counter), loop))
