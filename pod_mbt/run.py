"""CLI entry point.  python3 -m pod_mbt.run testcases/TC-01/state-led.json"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .engine import Session, WorkerConfig, WorkerKind
from .model import ACTION_DB, EventType, Power
from .driver import SimulatedDriver
from .policy import CHAINS, MarkovPolicy, load_test_case, probe_only, validate_chains
import random


def load(path: Path) -> tuple[str, int, list[WorkerConfig], dict]:
    """A plan is a config: four blocks of workers, and the engine treats them alike.

    `walk` is state-led, `probe` is action-led, `sweep` is both, and `faults` becomes one
    scheduled worker. Nothing here says "mode" — which block a worker came from is what
    it is.
    """
    cfg = json.loads(path.read_text())
    workers: list[WorkerConfig] = []

    for w in cfg.get("walk", []):
        workers.append(WorkerConfig(
            worker_id=w.get("workerId", w["component"] + "-worker"),
            kind=WorkerKind.WALK, component=w["component"],
            avg_interval_ms=w.get("avgIntervalMs", 240_000),
            pace=w.get("pace", "response")))

    for w in cfg.get("probe", []):
        workers.append(WorkerConfig(
            worker_id=w.get("workerId", w["component"] + "-probe"),
            kind=WorkerKind.PROBE, component=w["component"],
            pool=w.get("pool", []),
            avg_interval_ms=w.get("avgIntervalMs", 180_000)))

    for w in cfg.get("sweep", []):
        workers.append(WorkerConfig(
            worker_id=w.get("workerId", w["component"] + "-sweep"),
            kind=WorkerKind.SWEEP, component=w["component"],
            pool=w.get("pool", []),
            avg_interval_ms=w.get("avgIntervalMs", 180_000)))

    faults = cfg.get("faults", [])
    if faults:
        workers.append(WorkerConfig(
            worker_id="fault-clock", kind=WorkerKind.SCHEDULE,
            schedule=[{"actionName": f["actionName"],
                       "scheduledOffsetMs": f.get("atMs", f.get("scheduledOffsetMs", 0))}
                      for f in faults]))

    for i, r in enumerate(cfg.get("replay", [])):
        workers.append(WorkerConfig(worker_id=r.get("workerId", f"replay-{i+1}"),
                                    kind=WorkerKind.SCHEDULE,
                                    schedule=r.get("schedule", [])))

    return (cfg.get("sessionId", path.stem),
            cfg.get("durationMs", 8 * 3600 * 1000), workers, cfg)


def test_case(config_path: Path) -> tuple[str, dict | None]:
    """Which test case this plan walks, and its chains — read from where the plan *lives*.

    A plan sits inside its test case's folder (`testcases/TC-01/state-led.json`), so the link is the
    path and there is nothing to keep in step. It used to be a `testCase` field, which is a
    pointer, and a pointer can be wrong: a plan copied from one test case into another would keep
    naming the first and nothing would notice. The same reasoning as generating the Blueprint's
    figure rather than authoring it — a fact that is derived cannot disagree with its source.

    Returns `("", None)` for a plan outside a test case folder, which then walks the built-in
    `CHAINS`.
    """
    model = config_path.parent / "model.json"
    if not model.is_file():
        return "", None
    tc_id, chains, _faults = load_test_case(model)
    return tc_id or config_path.parent.name, chains


def model_version() -> str:
    """What the run was driven by. A reproduction replayed against a different model is
    not the same test, so the folder records which one it used."""
    body = "|".join(f"{a.name}:{a.guard_text}:{a.postcondition_text}"
                     for a in ACTION_DB)
    body += "||" + json.dumps({c: {st: [(e.action, e.nxt, e.p) for e in row]
                                     for st, row in rows.items()}
                                 for c, rows in CHAINS.items()}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def drift(sess, rep) -> dict[str, float]:
    """How far the walk strayed from the chain's own prediction, per component.

    A number no postcondition produces: every step can pass while the device quietly
    spends its time somewhere the model says it should not. Compared step-for-step,
    because a stationary distribution counts turns, not seconds.
    """
    out = {}
    for comp in sess.policy.chains:
        predicted = sess.policy.stationary(comp)
        observed = rep.observed_step_distribution.get(comp, {})
        if observed:
            out[comp] = round(max(abs(observed.get(k, 0.0) - v)
                                  for k, v in predicted.items()), 3)
    return out


def measured_elapsed(rec) -> int:
    """How long the device was actually exercised, from the log.

    `durationMs` is the plan's figure: a run that crashed at hour two still reports its full
    budget. This is `t_ms + elapsed_ms` of the last action to resolve — not `max(t_ms)`, because
    `run_step` completes synchronously, so the final action finishes just past the deadline.
    """
    last, pending = 0, {}
    for e in rec.ordered():
        d = e.details
        if e.event_type == EventType.ACTION_TRIGGER.value:
            pending[e.source] = d.get("t_ms", 0)
            last = max(last, pending[e.source])
        elif e.event_type == EventType.ORACLE_RESULT.value and e.source in pending:
            last = max(last, pending.pop(e.source) + d.get("elapsed_ms", 0))
    return last


def stop_reason(sess, planned: int, measured: int) -> str:
    """Why the run ended. Recorded rather than left for each reader to infer differently.

    `Session.run()` has three exits, and the third is currently unreachable: nothing sets
    `Power.OFF` (docs/pending_fixes.md E2).
    """
    if sess.crashed:
        return "Crash"
    if sess.state.power is Power.OFF:
        return "PowerOff"
    return "CompleteDuration" if measured >= planned * 0.99 else "StoppedEarly"


def session_meta(args, sid: str, duration: int, workers, sess, rep,
                 test_case: str = "") -> dict:
    """session.json — everything needed to line the folder up, run it again, and head a report.

    The report header wants eight facts about a run. Four of them used to live nowhere: the date
    and the folder survived only in the directory name, the elapsed time was the plan's figure
    rather than the measured one, and why the run stopped was left for each reader to infer. The
    remaining three describe the bench, so they come in on the command line or from the config —
    a simulator cannot know them, and a report should say `not recorded` rather than invent one.
    """
    measured = measured_elapsed(sess.rec)
    return {
        "sessionId": sid,
        "testCase": test_case or None,
        "date": time.strftime("%Y-%m-%d", time.localtime(sess.rec._t0 / 1e9)),
        "config": str(args.config),
        "durationMs": duration,
        "elapsedMs": measured,
        "stopReason": stop_reason(sess, duration, measured),
        "dut": getattr(args, "dut", None) or None,
        "releaseVersion": getattr(args, "release_version", None) or None,
        "driverVersion": getattr(args, "driver_version", None) or None,
        "seed": args.seed,
        "defects": sorted(args.defect),
        "modelVersion": model_version(),
        # Simulation shares the host clock. On a bench this is the delta-T the Fig. 3
        # handshake measures, and DeviceLog timestamps mean nothing without it.
        "hostToDeviceClockOffsetMs": 0,
        "systemRamp": getattr(sess, "system_ramp", None) or None,
        "driver": type(sess.driver).__name__,
        "workers": [
            {"workerId": w.worker_id, "kind": w.kind.value, "component": w.component,
             "avgIntervalMs": w.avg_interval_ms}
            for w in workers],
        "result": {
            "crashed": sess.crashed,
            "finalState": sess.state.to_dict(),
            "transitionsWalked": f"{rep.transitions_walked}/{rep.transitions_total}",
            "pairsTried": f"{rep.pairs_tried}/{rep.pairs_total}",
            "actionsRefusedOnly": rep.refused_actions,
            "errorSignatures": len(sess.rec.signatures),
            "distributionDrift": drift(sess, rep),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one MBT session against the simulator.")
    ap.add_argument("config", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--defect", action="append", default=[],
                    choices=["accepts_illegal", "slow_matter", "flaky_dhcp"],
                    help="inject a deliberate device defect so the run has findings")
    ap.add_argument("--save", type=Path, metavar="DIR",
                    help="write this run's folder under DIR (Log/ + Analysis/)")
    ap.add_argument("--log", type=int, default=0, help="print the first N log entries")
    # The bench's own identity. A simulator cannot know these, and a report that invents them is
    # worse than one that says `not recorded`, so they are inputs rather than defaults.
    ap.add_argument("--dut", help="the device under test, e.g. Pod-A2-0041")
    ap.add_argument("--release-version", help="firmware or build under test")
    ap.add_argument("--driver-version", help="version of the bench driver, not its class name")
    a = ap.parse_args(argv)

    problems = validate_chains(ACTION_DB)
    if problems:
        print("MarkovPolicy is inconsistent:", *problems, sep="\n  ")
        return 2
    orphans = probe_only(ACTION_DB)
    if orphans:
        print("  probe-only     " + ", ".join(orphans)
              + "\n                 (no chain walks these; a plan without a probe or sweep "
                "never calls them)")

    sid, duration, workers, raw = load(a.config)
    tc_id, chains = test_case(a.config)
    sess = Session(sid, workers, duration_ms=duration, seed=a.seed,
                   chains=chains, system_ramp=raw.get("systemRamp"),
                   driver=SimulatedDriver(rng=random.Random(a.seed + 1),
                                          defects=frozenset(a.defect)))
    sess.run()
    rep = sess.coverage.report(sess.rec)

    print(f"\n  session        {sid}")
    print(f"  workers        {len(workers)}  ({', '.join(w.worker_id for w in workers)})")
    print(f"  budget         {duration/3_600_000:.1f} h virtual")
    print(f"  log entries    {len(sess.rec.entries)}")
    print(f"  final state    {sess.state.pretty()}")
    print(f"\n  transitions walked   {rep.transitions_walked}/{rep.transitions_total}"
          f"  ({rep.transition_percentage:.0f}% of the chains)")
    print(f"  pairs tried    {rep.pairs_tried}/{rep.pairs_total}"
          f"  ({rep.pair_percentage:.0f}% of state × action)")
    if rep.actions_never_accepted:
        print(f"  never accepted {', '.join(rep.actions_never_accepted)}")
    print("\n  where the walk spent its turns, against what the chain predicts")
    for comp in sess.policy.chains:
        predicted = sess.policy.stationary(comp)
        observed = rep.observed_step_distribution.get(comp, {})
        if not observed:
            continue
        d = max(abs(observed.get(k, 0.0) - v) for k, v in predicted.items())
        flag = "  <-- drifted" if d > 0.10 else ""
        print(f"    {comp:7s} drift {d:.3f}{flag}")
        for st, pv in predicted.items():
            print(f"        {st:28s} predicted {pv:5.1%}   observed {observed.get(st, 0):5.1%}")
    if sess.rec.signatures:
        print("\n  ErrorSignatures")
        for s in sess.rec.signatures.values():
            print(f"    [{s.signature_hash}] {s.trigger_type:24s} {s.bug_bucket}"
                  f"  ×{s.occurrence_count}")
            head = next((e for e in s.reproduction if "initialState" in e), None)
            if head:
                st = head["initialState"]
                print(f"        from  {st['power']}|wifi={st['wifi']}"
                      f"|bt={st['bt_link']}/{st['bt_audio']}|matter={st['matter']}")
            for t in [e for e in s.reproduction if "actionName" in e][-3:]:
                print(f"        {t['scheduledOffsetMs']:>7} ms  {t['workerId']:16s} {t['actionName']}")
    else:
        print("\n  no ErrorSignatures — try --defect accepts_illegal")
    if a.log:
        print()
        for e in sess.rec.ordered()[:a.log]:
            print(f"    {e.timestamp} {e.source:16s} {e.event_type:16s} {e.details}")
    if a.save:
        meta = session_meta(a, sid, duration, workers, sess, rep, tc_id)
        run = sess.rec.save_run(a.save, meta,
                                initial_state=sess.initial_state.to_dict(),
                                coverage=rep,
                                device_log=getattr(sess.driver, "device_log", None))
        print(f"\n  saved          {run}/")
        for rel in ("session.json", "Log/ActionSchedule.json", "Log/EngineEvents.jsonl",
                    "Log/ResourceSnapshot.json", "Log/DeviceLog/dut.log",
                    "Analysis/CoverageReport.json", "Analysis/ErrorSignature.json"):
            f = run / rel
            print(f"      {'  ' if '/' in rel else ''}{rel:34s} "
                  f"{f.stat().st_size if f.exists() else 0:>9,} bytes")
        print(f"        {'Analysis/proposals/':34s} "
              f"{'written by the offline analysis step':>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
