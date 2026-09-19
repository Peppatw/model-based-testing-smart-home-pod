# Model-Based Testing, with AI assistance — Run Planner (Slide 08)

What a run is made of, and how one gets planned. The runs themselves are in
`sample_test_case.md`.

> The published Blueprint renders this slide with an **interactive planner**: state what
> changed, what the bench can drive, and how long it is free, and it derives the plan —
> the workers, the faults, the files it reads, the hardware it needs, and what the plan
> gives up. That part exists only in `pod_mbt_blueprint.html`; the tables below are the
> same information in static form.

## The two kinds of run, and the clock that interrupts both

| Kind | What chooses the next action | What it is for |
|---|---|---|
| `walk` — state-led | The chain: this component's current state has a row, and the row is rolled. The model chooses *and* judges. | **Chaos engineering.** A realistic walk with a steady state you can state exactly, perturbed on a schedule, measured by how far it drifts and how fast it returns. |
| `probe` — action-led | Chance: any action from its pool, at whatever state the device is in. Chance chooses; the guard still judges. | **Exploring the unknown.** The chains describe 25 transitions; the device has 85 (state × action) combinations. A probe is how the rest gets reached. |
| `schedule` — clock-led | A list of times: faults, and saved reproductions replayed. | **Interrupting both.** A stepwise worker never starts a new action before the last resolves, so only a clock can land a fault mid-flight. |

- **Neither exploring kind terminates by itself.** `durationMs` ends the session. A
  schedule ends when its list runs out — the one length you can count in advance.
- **A probe without a model is a fuzzer.** Random calls only find crashes unless something
  can say what *should* have happened. The guard is that something, which is why action-led
  is still model-based testing: the model judges rather than chooses.
- **Drift belongs to the walk.** A probe is not reproducing realistic usage, so the
  distribution it produces means nothing. Report drift per component, for walked chains.

## What a plan contains

```jsonc
{
  "sessionId": "NIGHTLY-015",
  "durationMs": 28800000,
  "walk":   [ { "workerId": "wifi-worker", "component": "wifi", "avgIntervalMs": 120000 } ],
  "probe":  [ { "workerId": "matter-probe", "component": "matter", "pool": ["api_start_matter"] } ],
  "faults": [ { "actionName": "inject_dhcp_timeout", "atMs": 600000 } ],
  "replay": [ { "from": "20260915-163829_NIGHTLY-014", "signatureHash": "a0a968cd" } ],

  "usesPolicy": { "wifi": { "states": [...], "stationary": {...} } },
  "intent": { "changed": ["wifi"], "minutes": 480 },
  "rationale": [ "..." ],
  "notCovering": [ "..." ]
}
```

- **The plan is also just a config.** `sessionId`, `durationMs` and the worker blocks are
  all `pod_mbt.run` reads; the rest is for the person reviewing it.
- **`usesPolicy` is printed, never set.** The chain a plan walks is shown so a reviewer can
  see what it will do — but changing a probability is a proposal, which waits for review.
  A plan can change what a run exercises; it can never change what a result means.
- **`notCovering` is not optional.** Time spent on the component that changed is time taken
  from the others, and a plan that lists only its wins is advocacy.

## Who decides what

| | Decided by the plan (AI drafts, a person accepts) | Decided by a person, through a proposal |
|---|---|---|
| state-led | which components walk, how fast, how long, from where | the probabilities themselves |
| action-led | which (state × action) pairs are worth probing | adding an action the model lacks |
| faults | what fires, and when | — |
