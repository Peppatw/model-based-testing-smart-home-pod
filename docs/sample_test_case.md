# Model-Based Testing, with AI assistance — Four Worked Runs

Four plans in `configs/`, and the numbers each produced against the simulated device.
Every figure here came out of a run — `python3 -m pod_mbt.run configs/<name>.json` prints
the same summary, and `tests/test_consistency.py` re-runs all four to check these claims
still hold.

Read `run_planner.md` for what the kinds of run are for.

## NIGHTLY-014 — state-led, the nightly

`configs/nightly.json` — 8.0 h virtual

| | |
|---|---|
| workers | **schedule**: fault-clock · **walk**: wifi-worker, bt-worker, matter-worker |
| transitions walked | **24/24** |
| (state × action) pairs | **29/84** |
| drift per chain | wifi **0.014** · bt **0.002** · matter **0.157** |

Findings:

  - none


## CHAOS-003 — state-led under a heavy fault clock

`configs/chaos.json` — 24.0 h virtual, device defect: `accepts_illegal`

| | |
|---|---|
| workers | **schedule**: fault-clock · **walk**: wifi-worker, bt-worker, matter-worker |
| transitions walked | **24/24** |
| (state × action) pairs | **36/84** |
| drift per chain | wifi **0.008** · bt **0.002** · matter **0.055** |

Findings:

  - `illegal_call_accepted` / `api_bt_audio_streaming` ×1052
  - `illegal_call_accepted` / `api_start_matter` ×819
  - `illegal_call_accepted` / `api_bt_disconnect` ×502
  - `illegal_call_accepted` / `api_remove_matter_fabric` ×346


## PROBE-001 — action-led, into the space no chain describes

`configs/probe.json` — 4.0 h virtual, device defect: `accepts_illegal`

| | |
|---|---|
| workers | **probe**: wifi-probe, bt-probe, matter-probe · **schedule**: fault-clock |
| transitions walked | **24/24** |
| (state × action) pairs | **83/84** |
| drift per chain | wifi **0.259** · bt **0.209** · matter **0.233** |

Findings:

  - `illegal_call_accepted` / `api_connect_wifi` ×21
  - `illegal_call_accepted` / `api_bt_connect` ×22
  - `illegal_call_accepted` / `inject_wrong_setup_code` ×23
  - `illegal_call_accepted` / `api_remove_matter_fabric` ×24


## MIXED-002 — both kinds in one session

`configs/mixed.json` — 8.0 h virtual

| | |
|---|---|
| workers | **probe**: matter-probe · **schedule**: fault-clock · **walk**: wifi-worker, bt-worker |
| transitions walked | **24/24** |
| (state × action) pairs | **34/84** |
| drift per chain | wifi **0.004** · bt **0.004** · matter **0.399** |

Findings:

  - none


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
