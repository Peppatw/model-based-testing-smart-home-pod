# Model-Based Testing, with AI assistance — Smart Home Pod

A runnable companion to `docs/pod_mbt_blueprint.html`. Everything the Blueprint
describes — 32 actions, three Markov chains, state-led, action-led and sweeping
workers, the oracle, coverage and error clustering — is implemented here against a
simulated device.
Swapping `Driver` is what points it at a real bench.

Plan a run, then run the plan:

```bash
python3 -m pod_mbt.plan --changed wifi --minutes 480 --history runs/ --out next_run.json
python3 -m pod_mbt.run next_run.json
```

`plan.py` is the seam the "AI-driven" claim rests on, and it is the same kind of seam as
`Driver`: an interface with a runnable stand-in behind it. `RuleBasedPlanner` decides what
an LLM planner would decide — which components get a worker, how often each acts, which
faults are scheduled when, what the session is giving up — and writes its reasoning into
the plan. Swapping in a model means implementing `plan(intent, history)`; nothing else
moves. What no planner may touch is `ActionDatabase` or `MarkovPolicy`: those change only
when a person changes them, so a plan can change what a run exercises and can never change
what a result means.

Or run a config directly:

```bash
python3 -m pod_mbt.run configs/nightly.json          # state-led: 3 chains + a fault clock
python3 -m pod_mbt.run configs/probe.json            # action-led: into the unmodelled space
python3 -m pod_mbt.run configs/mixed.json            # both kinds in one session
python3 -m pod_mbt.run configs/sweep.json            # every state x action cell, fewest calls
python3 -m pod_mbt.run configs/fuzz.json             # no flow, no pacing: does it stay up?
python3 -m tests.test_consistency                    # does the code still match the docs?
```

Find a bug, save it, replay it, verify the fix:

```bash
python3 -m pod_mbt.run configs/chaos.json --defect accepts_illegal --save runs/
python3 -m pod_mbt.replay runs/<stamp>_CHAOS-003 --defect accepts_illegal  # reproduces
python3 -m pod_mbt.replay runs/<stamp>_CHAOS-003                           # fixed → 0 of N
```

Nothing to install: standard library only, Python 3.10+.

## Layout

| | |
|---|---|
| `pod_mbt/model.py` | States, `CompositeState`, and the 32-action `ActionDatabase` |
| `pod_mbt/plan.py` | `RuleBasedPlanner` — intent + past runs → `next_run.json` |
| `pod_mbt/policy.py` | `MarkovPolicy` — three chains, 24 transitions, each state summing to 1 |
| `pod_mbt/engine.py` | `EngineLoop` (Fig. 2) and the session scheduler |
| `pod_mbt/driver.py` | The `Driver` seam, plus a simulator that can be given defects |
| `pod_mbt/record.py` | `TestRecorder`, `CoverageTracker`, `ErrorSignature` |
| `pod_mbt/replay.py` | `ReplayEngine` — runs an `ErrorSignature`'s reproduction again |
| `pod_mbt/analyze.py` | `RuleBasedAnalyst` — reads a finished run, writes `report.md` + proposals |
| `pod_mbt/run.py` | The CLI: load a config, run the session, save the folder |
| `configs/` | Session configs, the same shape the Blueprint prints |
| `tests/` | Code-versus-docs consistency checks |

## Two things worth knowing before reading the code

**Guards are stored twice.** `guard_text` is what the Blueprint prints; `guard` is the
predicate `GuardCheck` evaluates. `tests/test_consistency.py` compares the code against
`docs/` rather than against itself, so a document can no longer claim something the data
does not support — which is the specific failure this design kept hitting while it was
being written.

**Concurrency is simulated as discrete events on a virtual clock, not as threads.** An
8-hour config therefore finishes instantly and a run is reproducible from its seed,
which is what makes replay mean anything. The interleaving semantics are the ones the
Blueprint specifies — a `walk`'s next turn comes when its action resolves, a `schedule`'s
when its own clock says so — and a real-hardware Driver runs the same loops on real
threads.

## Seeing the design actually work

```bash
python3 -m pod_mbt.run configs/chaos.json                            # clean slide
python3 -m pod_mbt.run configs/chaos.json --defect accepts_illegal   # findings
```

The first run issues illegal calls on purpose — the guard judges but never gates — and the simulated
device refuses every one of them, so no `ErrorSignature` is raised. The second gives the
device a defect — it acts on calls it should refuse — and the same session immediately
clusters `illegal_call_accepted` signatures, each carrying a reproduction that keeps
**worker id and time offset**, because a concurrency bug reproduced as a flat list of
action names is not reproduced at all.

Two other defects are available: `--defect flaky_dhcp` (`api_connect_wifi` answers
"done" and leaves the link down — invisible to a "did the call return?" check, caught
immediately by an assertion that reads the state instead of the reply) and
`--defect slow_matter` (commissioning exceeds its budget).

## Known gaps, carried over from the design on purpose

- Four states the enums declare are never reached: `Wifi.SCANNING`, `Wifi.CONNECTING`,
  `BtLink.ADVERTISING`, `BtLink.PAIRING`, `Matter.FABRIC_REMOVING`. They are windows
  inside a call, and the simulator models every call as atomic. They are kept for a real
  `Driver`, which can report one — so the "mid-transition, no turn" branch in
  `EngineLoop.select()` is unreachable here and live on a bench.
- There is no LLM in this package at all, and that is the design: `Analysis/report.md`
  and `Analysis/proposals/` are written by an offline step this repo does not implement.
  What it does implement is the folder that step reads.
- A reproduction carries the state it started from, not just its six actions. Without
  that, replaying from a cold boot lands somewhere else entirely and every guard
  evaluates differently — the code found this before a person would have.
- **Some reproductions are inconclusive, and the harness says so.** An action-led
  run against a device that accepts illegal calls can leave the model in a state no
  healthy device reaches — audio streaming over a link that is down. When such a state is
  the *starting point* of a reproduction, replaying it against a fixed device proves
  nothing in either direction, so `ReplayEngine` marks it inconclusive and names the
  contradiction instead of counting it as a pass or a failure.

- Reproduction is not guaranteed. `--defect accepts_illegal` fires on a coin flip and
  the workers interleave differently each time, so replaying against a still-broken
  device reproduces a minority of the signatures — 6 of 27 in one run. That is faithful
  rather than convenient: intermittent bugs are intermittent, and a replay harness that
  always reproduced them would be lying. Against a *fixed* device the result is
  unambiguous: 0 of 27, every time.

## Where each Blueprint slide lives in the code

| Slide | Code |
|---|---|
| 00 Why Model-Based Testing? | argument only — no code |
| 01 MBT Test Case | `configs/nightly.json`, `pod_mbt/policy.py` |
| 02 Architecture Overview | the package as a whole |
| 03 Data Structure | `pod_mbt/model.py`, `pod_mbt/policy.py` |
| 04 Runtime Flow | `pod_mbt/engine.py` |
| 05 What AI Does Here | `pod_mbt/record.py`, `pod_mbt/analyze.py` |
| 06 Action-Led Test | `WorkerKind.PROBE` and `WorkerKind.SWEEP` in `pod_mbt/engine.py` |
| 07 Pure Fuzz Test | `configs/fuzz.json` |
| 08 Run Planner | `pod_mbt/plan.py` |
