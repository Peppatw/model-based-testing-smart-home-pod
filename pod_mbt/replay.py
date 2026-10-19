"""ReplayEngine — take an ErrorSignature's reproduction and run it again.

`replay()` takes an **ActionSchedule**, not a TestSession. Most of a session's log is
not executable at all (`state_change`, `oracle_result` and
`resource_snapshot` are observations), so `load_session()` is how you *reach* an
ErrorSignature and the schedule it carries is what actually runs.

The schedule keeps `workerId` and `scheduledOffsetMs`, which is the whole reason it is
an ActionSchedule rather than a flat list: replaying the actions in order but losing who
did what and when would not reproduce a concurrency bug at all — the fault would land
tidily after the action it was supposed to interrupt.

    python3 -m pod_mbt.run testcases/TC-01/chaos.json --defect accepts_illegal --save test_result/
    python3 -m pod_mbt.replay test_result/<stamp>_CHAOS-003 --defect accepts_illegal  # reproduces
    python3 -m pod_mbt.replay test_result/<stamp>_CHAOS-003                           # fixed: does not
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

from .driver import Driver, SimulatedDriver
from .model import ACTION_DB, CompositeState, EventType
from .oracle import judge
from .record import CoverageTracker, TestRecorder


class ReplayEngine:
    def __init__(self, driver: Driver | None = None, seed: int = 7):
        self.driver = driver or SimulatedDriver(rng=random.Random(seed + 1))
        self.db = ACTION_DB
        # Replay's own dice, for the durations it measures. The schedule deliberately does not
        # carry the original run's elapsed times: those are results, and a replay whose results
        # were handed to it is not a test. It measures the device in front of it — which on a
        # bench is a wall clock and here is the same model the engine uses.
        self.rng = random.Random(seed)

    @staticmethod
    def load_session(path: Path) -> dict[str, Any]:
        """Reach the ErrorSignatures. The log itself is not what gets replayed.

        `path` is a run folder: the signatures live in `Analysis/`, the run they came
        from in `Log/`, and `session.json` says which model version produced both. A
        reproduction replayed against a different model is a different test, so the
        mismatch is worth surfacing rather than silently running.
        """
        path = Path(path)
        if path.is_dir():
            meta = json.loads((path / "session.json").read_text())
            sigs = json.loads((path / "Analysis" / "ErrorSignature.json").read_text())
            return {**meta, "errorSignatures": sigs}
        return json.loads(path.read_text())

    def replay(self, schedule: list[dict[str, Any]],
               session_id: str = "REPLAY") -> tuple[TestRecorder, CompositeState]:
        rec = TestRecorder(session_id)
        cov = CoverageTracker(self.db)
        head = next((e for e in schedule if "initialState" in e), None)
        state = (CompositeState.from_dict(head["initialState"]) if head
                 else CompositeState())
        steps = [e for e in schedule if "actionName" in e]

        # Validate every name before executing any of them. `ActionDatabase.get` raises a bare
        # KeyError, and called inside the loop a typo on the fortieth entry runs thirty-nine
        # actions first and then fails with the device already moved.
        unknown = sorted({e["actionName"] for e in steps} - set(self.db.names()))
        if unknown:
            raise ValueError(
                f"schedule names {len(unknown)} action(s) this model does not have: "
                + ", ".join(unknown))

        # Offsets collide, so `seq` breaks the tie. Ordering by offset alone lets a stable sort
        # reshuffle the three actions that share offset 0, and what the second and third see then
        # differs from what the original run gave them.
        for entry in sorted(steps, key=lambda e: (e.get("scheduledOffsetMs", 0),
                                                  e.get("seq", 0))):
            action = self.db.get(entry["actionName"])
            src = entry.get("workerId", "replay")
            t_ms = entry.get("scheduledOffsetMs", 0)
            guard_held = action.guard(state)

            rec.log(src, EventType.ACTION_TRIGGER, action=action.name,
                    guard_held=guard_held, t_ms=t_ms)
            new_state, result = self.driver.execute(action, state)
            elapsed = self.driver.elapsed_ms(action, state, self.rng)
            before, state = state, new_state

            # The same four-way judgement the engine makes — one copy, in `oracle.py`.
            # Two of these were missing here, and `postcondition_timeout` is the commonest
            # trigger a chaos run produces, so most reproductions could not be reproduced.
            v = judge(action, before, state, result, elapsed, guard_held,
                      entry.get("expectedState"))
            ok, trigger = v.ok, v.trigger

            rec.log(src, EventType.ORACLE_RESULT, action=action.name, passed=ok,
                    expected=v.detail, elapsed_ms=elapsed,
                    budget_ms=action.postcondition_timeout_ms)
            if not ok:
                rec.cluster(trigger, action.name, list(schedule))
            cov.record(state, action.component, before.chain_state(action.component),
                   action.name, accepted=result.accepted)

        return rec, state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Replay the reproductions saved in a TestSession.")
    ap.add_argument("session", type=Path, metavar="RUN_FOLDER")
    ap.add_argument("--defect", action="append", default=[],
                    choices=["accepts_illegal", "slow_matter", "flaky_dhcp"],
                    help="device defects present during replay; omit to model a fixed device")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)

    session = ReplayEngine.load_session(a.session)
    signatures = session.get("errorSignatures", [])
    if not signatures:
        print(f"\n  {a.session} holds no ErrorSignatures — nothing to replay.\n")
        return 0

    fixed = not a.defect
    print(f"\n  replaying {len(signatures)} reproduction(s) from {a.session}")
    print(f"  device     {'fixed — defects removed' if fixed else 'defective: ' + ', '.join(a.defect)}")
    if session.get("modelVersion"):
        from .run import model_version
        now = model_version()
        same = now == session["modelVersion"]
        print(f"  model      {session['modelVersion']}"
              + ("" if same else f"  — CHANGED since the run (now {now}); "
                                 "a reproduction is only comparable against the model it was found on"))
    print()

    reproduced = unreachable = diverged = 0
    for sig in signatures:
        engine = ReplayEngine(
            driver=SimulatedDriver(rng=random.Random(a.seed + 1),
                                   defects=frozenset(a.defect)),
            seed=a.seed)
        head = next((e for e in sig["reproduction"] if "initialState" in e), None)
        start = (CompositeState.from_dict(head["initialState"]) if head else CompositeState())
        bad = start.inconsistencies()
        rec, _ = engine.replay(sig["reproduction"], session_id=sig["signature_hash"])
        hit = any(s.bug_bucket == sig["bug_bucket"] and s.trigger_type == sig["trigger_type"]
                  for s in rec.signatures.values())

        # Did this replay reach the same condition the original did? `observed_path` is the states
        # the original walked over this window, and where the two first differ says which of two
        # very different things happened.
        #
        # Diverging **at the last step** is what a fix looks like: the call under test no longer
        # moves the device, so the path stops matching exactly where it should. Diverging
        # **earlier** means the replay wandered off before reaching the condition, and then "not
        # reproduced" says nothing about whether the bug is fixed — reporting that as a pass is
        # how a regression suite comes to certify a bug it never ran.
        walked = [start.pretty()] + [e.details["to"] for e in rec.ordered()
                                     if e.event_type == EventType.STATE_CHANGE.value]
        recorded = sig.get("observed_path") or []
        off = next((i for i, (x, y) in enumerate(zip(walked, recorded)) if x != y), None)
        if off is None and len(walked) != len(recorded) and recorded:
            off = min(len(walked), len(recorded))
        # The failing action is the last one in the window, so the last state is its consequence.
        shifted = (bool(recorded) and off is not None and not hit
                   and off < len(recorded) - 1)

        if bad:
            mark, unreachable = "inconclusive — see below", unreachable + 1
        elif hit:
            mark, reproduced = "REPRODUCED", reproduced + 1
        elif shifted:
            mark, diverged = (f"not comparable — path diverged at step {off} "
                              f"of {len(recorded) - 1}"), diverged + 1
        else:
            mark = "not reproduced — the call under test no longer moves the device"

        print(f"    [{sig['signature_hash']}] {sig['trigger_type']:24s} "
              f"{sig['bug_bucket']:32s} {mark}")
        if bad:
            print(f"        (it starts from a state only a broken device reaches: {'; '.join(bad)})")
        if shifted and off is not None and off < len(recorded):
            print(f"        (expected {recorded[off]}")
            print(f"              got {walked[off] if off < len(walked) else '— run ended'})")
        if not hit and rec.signatures:
            other = ", ".join(s.bug_bucket for s in rec.signatures.values())
            print(f"        (a different failure appeared instead: {other})")

    comparable = len(signatures) - unreachable - diverged
    print(f"\n  {reproduced}/{comparable} reproduced, of {len(signatures)} signature(s)")
    if diverged:
        print(f"  {diverged} not comparable: the replay left the recorded path before reaching "
              "the call under test, so whether that bug is fixed is not what they answer")
    if unreachable:
        print(f"  {unreachable} inconclusive: their starting state is itself a symptom")
    if fixed and reproduced == 0 and not diverged and not unreachable:
        print("  Every reproduction is now clean — this is what a verified fix looks like.\n")
    elif fixed and reproduced == 0:
        print("  No reproduction failed, but some were not comparable — this is not yet a "
              "verified fix.\n")
    elif fixed:
        print("  Some reproductions still fail against a device with no injected defect.\n")
    else:
        print("  The schedule is a regression test: it can be re-run every release.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
