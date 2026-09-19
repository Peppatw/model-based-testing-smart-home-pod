"""CLI entry point.  python3 -m pod_mbt.run configs/nightly.json"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .engine import Session, WorkerConfig, WorkerKind
from .model import ACTION_DB
from .driver import SimulatedDriver
from .policy import CHAINS, MarkovPolicy, validate_chains
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
            avg_interval_ms=w.get("avgIntervalMs", 240_000)))

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


def session_meta(args, sid: str, duration: int, workers, sess, rep) -> dict:
    """session.json — everything needed to line the folder up and run it again."""
    return {
        "sessionId": sid,
        "config": str(args.config),
        "durationMs": duration,
        "seed": args.seed,
        "defects": sorted(args.defect),
        "modelVersion": model_version(),
        # Simulation shares the host clock. On a bench this is the delta-T the Fig. 3
        # handshake measures, and DeviceLog timestamps mean nothing without it.
        "hostToDeviceClockOffsetMs": 0,
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
    a = ap.parse_args(argv)

    problems = validate_chains(ACTION_DB)
    if problems:
        print("MarkovPolicy is inconsistent:", *problems, sep="\n  ")
        return 2

    sid, duration, workers, raw = load(a.config)
    sess = Session(sid, workers, duration_ms=duration, seed=a.seed,
                   driver=SimulatedDriver(rng=random.Random(a.seed + 1),
                                          defects=frozenset(a.defect)))
    sess.run()
    rep = sess.coverage.report()

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
        run = sess.rec.save_run(a.save, session_meta(a, sid, duration, workers, sess, rep),
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
