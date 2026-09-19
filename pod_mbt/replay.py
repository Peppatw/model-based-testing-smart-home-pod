"""ReplayEngine — take an ErrorSignature's reproduction and run it again.

`replay()` takes an **ActionSchedule**, not a TestSession. Most of a session's log is
not executable at all (`state_change`, `oracle_result` and
`resource_snapshot` are observations), so `load_session()` is how you *reach* an
ErrorSignature and the schedule it carries is what actually runs.

The schedule keeps `workerId` and `scheduledOffsetMs`, which is the whole reason it is
an ActionSchedule rather than a flat list: replaying the actions in order but losing who
did what and when would not reproduce a concurrency bug at all — the fault would land
tidily after the action it was supposed to interrupt.

    python3 -m pod_mbt.run configs/chaos.json --defect accepts_illegal --save runs/
    python3 -m pod_mbt.replay runs/<stamp>_CHAOS-003 --defect accepts_illegal  # reproduces
    python3 -m pod_mbt.replay runs/<stamp>_CHAOS-003                           # fixed: does not
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
from .record import CoverageTracker, TestRecorder


class ReplayEngine:
    def __init__(self, driver: Driver | None = None, seed: int = 7):
        self.driver = driver or SimulatedDriver(rng=random.Random(seed + 1))
        self.db = ACTION_DB

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

        for entry in sorted(steps, key=lambda e: e.get("scheduledOffsetMs", 0)):
            action = self.db.get(entry["actionName"])
            src = entry.get("workerId", "replay")
            t_ms = entry.get("scheduledOffsetMs", 0)
            guard_held = action.guard(state)

            rec.log(src, EventType.ACTION_TRIGGER, action=action.name,
                    guard_held=guard_held, t_ms=t_ms)
            new_state, result = self.driver.execute(action, state)
            before, state = state, new_state

            if guard_held:
                ok = action.postcondition(state)
                trigger = "postcondition_failure"
            else:
                ok = (not result.accepted) and state.as_tuple() == before.as_tuple() \
                     and not result.crashed
                trigger = "illegal_call_accepted"

            rec.log(src, EventType.ORACLE_RESULT, action=action.name, passed=ok)
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

    reproduced = unreachable = 0
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
        reproduced += hit and not bad
        mark = "REPRODUCED" if hit else "not reproduced"
        if bad:
            mark = "inconclusive — see below"
            unreachable += 1
        print(f"    [{sig['signature_hash']}] {sig['trigger_type']:24s} "
              f"{sig['bug_bucket']:32s} {mark}")
        if bad:
            print(f"        (it starts from a state only a broken device reaches: {'; '.join(bad)})")
        if not hit and rec.signatures:
            other = ", ".join(s.bug_bucket for s in rec.signatures.values())
            print(f"        (a different failure appeared instead: {other})")

    print(f"\n  {reproduced}/{len(signatures)} reproduced"
          + (f", {unreachable} inconclusive (their starting state is itself a symptom)"
             if unreachable else ""))
    if fixed and reproduced == 0:
        print("  Every reproduction is now clean — this is what a verified fix looks like.\n")
    elif fixed:
        print("  Some reproductions still fail against a device with no injected defect.\n")
    else:
        print("  The schedule is a regression test: it can be re-run every release.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
