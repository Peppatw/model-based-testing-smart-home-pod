# Model-Based Testing, with AI assistance — Four Worked Runs

Four **plans** in `testcases/TC-01/`, and the numbers each produced against the simulated device.
All four walk the same single test case, `testcases/TC-01.json` — what differs is the plan:
which worker kinds, how long, how many faults. A test case is the model, a plan is one way
to walk it, and a session is one execution of a plan.

**These figures are generated, not transcribed.** `tests/test_consistency.py` runs every
plan and asserts it produces coverage, but it does not assert the numbers below — an
earlier version of this file claimed it did, and three of the four had drifted by the time
anyone checked. Re-run `python3 -m pod_mbt.run testcases/TC-01/<name>.json` to reproduce one.

Read `run_planner.md` for what the kinds of run are for.

## NIGHTLY-014 — state-led, the nightly

`testcases/TC-01/nightly.json` — 8.0 h virtual, test case `TC-01`

| | |
|---|---|
| workers | **schedule**: fault-clock · **walk**: wifi-worker, bt-worker, matter-worker |
| transitions walked | **24/24** |
| (state × action) pairs | **29/84** model, **29/43** of what this plan can reach |
| legal composite states | **27/96** |
| concurrent actions | **70.2%** issued while another was in flight |
| drift per chain | bt **0.002** · matter **0.157** · wifi **0.014** |

Findings: none.

## CHAOS-003 — State-led walk under a heavy fault clock

`testcases/TC-01/chaos.json` — 24.0 h virtual, test case `TC-01`, device defect: `accepts_illegal`

| | |
|---|---|
| workers | **schedule**: fault-clock · **walk**: wifi-worker, bt-worker, matter-worker |
| transitions walked | **24/24** |
| (state × action) pairs | **36/84** model, **36/55** of what this plan can reach |
| legal composite states | **27/96** |
| concurrent actions | **68.7%** issued while another was in flight |
| drift per chain | bt **0.001** · matter **0.049** · wifi **0.002** |

Findings:

  - `illegal_call_accepted` / `api_bt_audio_streaming` ×1051
  - `illegal_call_accepted` / `api_start_matter` ×805
  - `illegal_call_accepted` / `api_bt_disconnect` ×492
  - `illegal_call_accepted` / `api_remove_matter_fabric` ×319

## PROBE-001 — Action-led

`testcases/TC-01/probe.json` — 4.0 h virtual, test case `TC-01`, device defect: `accepts_illegal`

| | |
|---|---|
| workers | **probe**: wifi-probe, bt-probe, matter-probe · **schedule**: fault-clock |
| transitions walked | **24/24** |
| (state × action) pairs | **81/84** model, **81/84** of what this plan can reach |
| legal composite states | **31/96** |
| concurrent actions | **19.6%** issued while another was in flight |
| drift per chain | bt **0.205** · matter **0.200** · wifi **0.289** |

Findings:

  - `illegal_call_accepted` / `inject_wrong_setup_code` ×23
  - `illegal_call_accepted` / `api_remove_matter_fabric` ×23
  - `illegal_call_accepted` / `api_complete_matter_commissioning` ×22
  - `illegal_call_accepted` / `api_bt_connect` ×20

## MIXED-002 — Both kinds at once

`testcases/TC-01/mixed.json` — 8.0 h virtual, test case `TC-01`

| | |
|---|---|
| workers | **probe**: matter-probe · **schedule**: fault-clock · **walk**: wifi-worker, bt-worker |
| transitions walked | **24/24** |
| (state × action) pairs | **35/84** model, **35/41** of what this plan can reach |
| legal composite states | **27/96** |
| concurrent actions | **58.0%** issued while another was in flight |
| drift per chain | bt **0.004** · matter **0.269** · wifi **0.003** |

Findings: none.
## What the four runs say together

- **A walk saturates the chain and stops.** All 24 transitions within the first hour, and then
  nothing new: a walker only ever takes transitions the model already describes.
- **A probe reaches what the chain cannot, and now reaches the chain too.** 83 of 84
  (state × action) pairs against a walk's 29 — the difference *is* the unexplored space,
  quantified. Unlike the old five-state Bluetooth chain, three states per component
  saturates even under uniform choice, so both kinds now finish the transitions; only the
  deep pairs still separate them.
- **Drift is a state-led metric.** Under 0.06 on a walked chain that behaved; 0.21 to 0.26
  on probed ones, where it means nothing because a probe is not reproducing real usage.
- **`matter` drifting 0.157 in the nightly is the finding to point at.** It spent 55% of
  its turns in `Uncommissioned` where the chain predicts 39%, because `api_start_matter`
  waits on `wifi = Connected`. Every step passed its own assertion.
