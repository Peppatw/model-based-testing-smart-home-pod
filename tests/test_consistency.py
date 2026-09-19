"""Does the code still say what the Blueprint says?

This suite exists because of what kept going wrong while the design was written: a
document claiming something the data did not support. Everything here compares
`pod_mbt/` against `docs/` rather than against itself, so the two cannot drift quietly.

    python3 -m tests.test_consistency
"""
from __future__ import annotations

import html
import json
import pathlib
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pod_mbt.engine import Session
from pod_mbt.model import ACTION_DB, EventType, Executor
from pod_mbt.plan import History, Intent, RuleBasedPlanner
from pod_mbt.policy import CHAINS, FAULTS, MarkovPolicy, validate_chains
from pod_mbt.run import load

DOCS = Path(__file__).resolve().parents[1] / "docs"
BLUEPRINT = (DOCS / "pod_mbt_blueprint.html").read_text()
PLAIN = html.unescape(re.sub(r"<[^>]+>", " ", BLUEPRINT))
# The Action Database and chain tables live in the reference doc, not the Blueprint
# itself — Slide 03 keeps only what a reader decides from and points there for the rest.
DATA_STRUCTURE = (DOCS / "data_structure.md").read_text()

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  — ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def doc_actions() -> dict[str, tuple[str, str]]:
    """Every action the Action Database table lists, with its guard and postcondition."""
    i = DATA_STRUCTURE.index("The Action Database")
    j = DATA_STRUCTURE.index("</table>", i)
    strip = lambda s: html.unescape(re.sub("<[^>]+>", "", s)).strip()
    return {n: (strip(g), strip(p)) for n, _, _, g, p, _, _ in re.findall(
        r"<tr><td><code>([a-z_]+)</code></td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td>"
        r"<td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", DATA_STRUCTURE[i:j])}


def doc_transitions() -> set[tuple[str, str]]:
    """Every (state, action) pair the chain table prints."""
    i = DATA_STRUCTURE.index("The chains —")
    j = DATA_STRUCTURE.index("</table>", i)
    out, state = set(), ""
    for cell, action in re.findall(
            r"<tr><td>[^<]*</td><td>(.*?)</td><td><code>([a-z_]+)</code></td>",
            DATA_STRUCTURE[i:j]):
        name = html.unescape(re.sub("<[^>]+>", "", cell)).strip()
        state = name or state
        out.add((state, action))
    return out


def main() -> int:
    pol = MarkovPolicy()

    print("\nAction Database — the contract")
    check("32 actions in code", len(ACTION_DB) == 32, f"got {len(ACTION_DB)}")
    doc = doc_actions()
    check("the table has a row for all 32 actions", len(doc) == 32, str(len(doc)))
    check("code and the table list the same actions",
          set(doc) == {a.name for a in ACTION_DB},
          str(set(doc) ^ {a.name for a in ACTION_DB}))
    drift = [a.name for a in ACTION_DB
             if a.name in doc and doc[a.name] != (a.guard_text, a.postcondition_text)]
    check("no action has drifted between docs and code", not drift, ", ".join(drift[:3]))
    check("an action declares no source or destination — the transition belongs to the chain",
          not hasattr(ACTION_DB.get("api_connect_wifi"), "source_text"))
    check("every cleanupAction names a real action",
          all(a.cleanup_action in {x.name for x in ACTION_DB}
              for a in ACTION_DB if a.cleanup_action))
    check("21 dut / 11 equipment",
          sum(1 for a in ACTION_DB if a.executor is Executor.DUT) == 21
          and sum(1 for a in ACTION_DB if a.executor is Executor.EQUIPMENT) == 11)

    print("\nThe chains")
    problems = validate_chains(ACTION_DB)
    check("every state's transitions sum to 1, name known actions, and land in this chain",
          not problems, "; ".join(problems[:3]))
    n_states = sum(len(c) for c in CHAINS.values())
    n_transitions = len(pol.transitions())
    check(f"the reference doc states the real size ({len(CHAINS)} chains, {n_states} states, "
          f"{n_transitions} transitions)",
          f"{n_transitions} transitions" in DATA_STRUCTURE and f"{n_states} states" in DATA_STRUCTURE,
          "data_structure.md claims a different total")
    missing = doc_transitions() - {(s, a) for _, s, a in pol.transitions()}
    check("every transition printed in Slide 03 exists in code", not missing,
          str(sorted(missing)[:3]))
    check("fourteen actions sit outside every chain", len(FAULTS) == 14, str(len(FAULTS)))
    check("no fault leaked into a chain",
          not [e for e in pol.transitions() if e[2] in FAULTS])
    check("api_bt_disconnect is weighted differently per state — the reason a "
          "matrix beats a global weight",
          len({e.p for rows in CHAINS.values() for r in rows.values() for e in r
               if e.action == "api_bt_disconnect"}) >= 2)

    print("\nWhat the chains predict")
    for comp in CHAINS:
        dist = pol.stationary(comp)
        check(f"{comp:7s} stationary distribution sums to 1",
              abs(sum(dist.values()) - 1.0) < 0.01, str(sum(dist.values())))

    print("\nEvery published plan actually runs")
    for cfg in sorted(pathlib.Path("configs").glob("*.json")):
        sid, dur, workers, _ = load(cfg)
        sess = Session(sid, workers, duration_ms=dur, seed=7)
        sess.run()
        rep = sess.coverage.report()
        kinds = ", ".join(sorted({w.kind.value for w in workers}))
        check(f"{cfg.stem:9s} runs — transitions {rep.transitions_walked}/{rep.transitions_total}, "
              f"pairs {rep.pairs_tried}/{rep.pairs_total}  ({kinds})", rep.pairs_tried > 0)

    print("\nState-led and action-led reach different things")
    runs = {}
    for name, path in (("walk", "configs/nightly.json"), ("probe", "configs/probe.json")):
        sid, dur, workers, _ = load(pathlib.Path(path))
        s = Session(sid, workers, duration_ms=dur, seed=11)
        s.run()
        runs[name] = s.coverage.report()
    check(f"a walk covers the chain's transitions — {runs['walk'].transition_percentage:.0f}%",
          runs["walk"].transition_percentage > 90)
    check(f"a walk cannot cover state × action on its own — "
          f"{runs['walk'].pair_percentage:.0f}%", runs["walk"].pair_percentage < 60)
    check(f"a probe reaches pairs a walk does not — {runs['probe'].pairs_tried} vs "
          f"{runs['walk'].pairs_tried}",
          runs["probe"].pairs_tried > runs["walk"].pairs_tried)

    print("\nSlide 06's wifi table — the numbers printed there came out of these runs")
    from pod_mbt.engine import WorkerConfig, WorkerKind
    slide = {}
    for kind in (WorkerKind.WALK, WorkerKind.PROBE):
        cfg = WorkerConfig(worker_id="wifi-w", kind=kind, component="wifi",
                           avg_interval_ms=120_000)
        s = Session("SLIDE06", [cfg], duration_ms=8 * 3600_000, seed=11)
        s.run()
        slide[kind.value] = s.coverage.report().per_component["wifi"]
    w, pr = slide["walk"], slide["probe"]
    check(f"a walk reaches every transition and no more — {w['transitionsWalked']}/"
          f"{w['transitions']} transitions, {w['pairsTried']}/{w['pairs']} pairs",
          (w["transitionsWalked"], w["pairsTried"]) == (9, 9))
    check("a walk's pairs and its transitions are the same set — it cannot reach a "
          "tenth cell", w["pairsTried"] == w["transitionsWalked"])
    check(f"a probe reaches most of the grid — {pr['transitionsWalked']}/{pr['transitions']} "
          f"transitions, {pr['pairsTried']}/{pr['pairs']} pairs",
          (pr["transitionsWalked"], pr["pairsTried"]) == (7, 32))

    cfg = WorkerConfig(worker_id="wifi-sweep", kind=WorkerKind.SWEEP, component="wifi",
                       avg_interval_ms=120_000)
    s = Session("SLIDE06", [cfg], duration_ms=8 * 3600_000, seed=11)
    s.run()
    sw = s.coverage.report().per_component["wifi"]
    trig = [e for e in s.rec.ordered()
            if e.event_type == EventType.ACTION_TRIGGER.value and e.details.get("from_state")]
    seen, at = set(), None
    for n, e in enumerate(trig, 1):
        seen.add((e.details["from_state"], e.details["action"]))
        if len(seen) == sw["pairs"] and at is None:
            at = n
    check(f"a sweep closes the grid a probe cannot — {sw['transitionsWalked']}/"
          f"{sw['transitions']} transitions, {sw['pairsTried']}/{sw['pairs']} pairs",
          (sw["transitionsWalked"], sw["pairsTried"]) == (9, 42))
    check(f"the sweep gets there on call {at}, against a floor of {sw['pairs']}",
          at is not None and at <= 2 * sw["pairs"])

    print("\nA generated plan is a runnable config")
    plan = RuleBasedPlanner("PLAN-TEST").plan(Intent(changed=["wifi"], minutes=60), History())
    check("a plan names walkers, probes and faults",
          bool(plan["walk"] and plan["probe"] and plan["faults"]))
    check("a plan says what it is not covering", isinstance(plan["notCovering"], list))
    check("a plan carries the chain it walks, read-only", "usesPolicy" in plan)
    check("a plan never sets a probability — that is a proposal, not a plan",
          '"p"' not in json.dumps(plan["walk"]) + json.dumps(plan["probe"]))
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "next_run.json"
        f.write_text(json.dumps(plan))
        sid, dur, workers, _ = load(f)
        sess = Session(sid, workers, duration_ms=dur, seed=7)
        sess.run()
        rep = sess.coverage.report()
        check(f"the plan runs end to end — pairs {rep.pairs_tried}/{rep.pairs_total}",
              rep.pairs_tried > 0)

    print("\nThe planner reads coverage back, and changes tool when it has to")
    from pod_mbt.run import session_meta
    from pod_mbt.policy import MarkovPolicy as _MP

    def snapshot(cfg_path, root, seed=7):
        sid, dur, workers, _ = load(pathlib.Path(cfg_path))
        s = Session(sid, workers, duration_ms=dur, seed=seed)
        s.run()
        r = s.coverage.report()
        class A:                                  # the argparse namespace save_run wants
            config, defect, save = pathlib.Path(cfg_path), [], pathlib.Path(root)
            seed = 7
        s.rec.save_run(root, session_meta(A, sid, dur, workers, s, r), coverage=r)
        return r

    with tempfile.TemporaryDirectory() as tmp:
        pol2 = _MP()
        rep_n = snapshot("configs/nightly.json", tmp)
        hist = History.load(pathlib.Path(tmp))
        untried = hist.untried_pairs(pol2)
        check(f"a run folder says which pairs it reached, not just how many — "
              f"{rep_n.pairs_tried} tried + {len(untried)} untried = {rep_n.pairs_total}",
              rep_n.pairs_tried + len(untried) == rep_n.pairs_total)
        p1 = RuleBasedPlanner().plan(Intent(changed=["wifi"], minutes=480), hist)
        check(f"a grid still mostly unreached is swept, not walked — "
              f"{[w['component'] for w in p1['sweep']]}", bool(p1["sweep"]))
        snapshot("configs/sweep.json", tmp)
        hist2 = History.load(pathlib.Path(tmp))
        check("once the grid is closed there is nothing left untried",
              not hist2.untried_pairs(pol2))
        p2 = RuleBasedPlanner().plan(Intent(changed=["wifi"], minutes=480), hist2)
        check("and the planner stops sweeping, because the reason is gone",
              not p2["sweep"] and bool(p2["walk"]))

    print("\nA run folder is written, and replays")
    import random
    from pod_mbt.driver import SimulatedDriver
    from pod_mbt.replay import ReplayEngine
    from pod_mbt.run import model_version, session_meta
    sid, dur, workers, _ = load(pathlib.Path("configs/mixed.json"))
    sess = Session(sid, workers, duration_ms=3_600_000, seed=5,
                   driver=SimulatedDriver(rng=random.Random(6),
                                          defects=frozenset(["accepts_illegal"])))
    sess.run()
    rep = sess.coverage.report()
    with tempfile.TemporaryDirectory() as tmp:
        class A:                                  # the argparse namespace save_run wants
            config, seed, defect, save = pathlib.Path("configs/mixed.json"), 5, \
                ["accepts_illegal"], pathlib.Path(tmp)
        run = sess.rec.save_run(tmp, session_meta(A, sid, dur, workers, sess, rep),
                                coverage=rep,
                                device_log=getattr(sess.driver, "device_log", None))
        for rel in ("session.json", "Log/ActionSchedule.json", "Log/EngineEvents.jsonl",
                    "Analysis/CoverageReport.json", "Analysis/ErrorSignature.json"):
            check(f"the folder holds {rel}", (run / rel).is_file())
        meta = json.loads((run / "session.json").read_text())
        check("session.json pins the model version", meta["modelVersion"] == model_version())
        loaded = ReplayEngine.load_session(run)
        check("replay can reach the signatures from the folder",
              isinstance(loaded.get("errorSignatures"), list))
        if loaded.get("errorSignatures"):
            engine = ReplayEngine(driver=SimulatedDriver(rng=random.Random(6)), seed=5)
            rec, _ = engine.replay(loaded["errorSignatures"][0]["reproduction"])
            check("a reproduction replays without raising", rec is not None)

        print("\nThe analyst reads a run folder and writes report.md + proposals/")
        from pod_mbt.analyze import RuleBasedAnalyst, write_back
        analysis = RuleBasedAnalyst().analyze(run)
        write_back(run, analysis)
        check("report.md exists and is not empty",
              (run / "Analysis" / "report.md").read_text().strip() != "")
        check("a proposal was written per signature",
              len(analysis.proposals) == len(loaded.get("errorSignatures", [])))
        if analysis.proposals:
            p = json.loads((run / "Analysis" / "proposals"
                            / f"{analysis.proposals[0]['id']}.json").read_text())
            check("a proposal carries the documented envelope",
                  {"id", "type", "baseVersion", "target", "current", "proposed",
                   "evidence", "rationale", "status"} <= set(p))
            check("a proposal starts pending, and names the model it was written against",
                  p["status"] == "pending" and p["baseVersion"] == model_version())
            check("every proposal id has a matching file in proposals/",
                  all((run / "Analysis" / "proposals" / f"{pr['id']}.json").is_file()
                      for pr in analysis.proposals))

    print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
