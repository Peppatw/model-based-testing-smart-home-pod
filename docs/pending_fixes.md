# Pending fixes

Everything below came out of one review session (2026-09-23) that took `testcases/TC-01/chaos.json` —
TC-01, the worked test case on Slide 01 — ran it, and compared what the framework reported
against what its own files contained. Each item names the evidence, so none of them has to be
re-argued before it is fixed.

Grouped by consequence, not by file. Group A can make a reader draw a wrong conclusion; B is
data that does not exist; C is data that exists and is thrown away; D is a document and the code
disagreeing; E is structure.

---

## A — Produces a wrong conclusion

**A1. `replay` reports "verified fix" for bugs it structurally cannot detect.**  ✅ **done 2026-09-23** — one oracle in `pod_mbt/oracle.py`, called by both loops; `Driver.elapsed_ms` so a replay measures its own timings; `expectedState` on the schedule. Fixed device now reports 20/20 reproduced of 27 with 7 not comparable, where it used to report 0/27 and "verified fix"
`replay.py:72-80` implements two of the four trigger types: it never computes `elapsed`, so
`postcondition_timeout` is unreachable, and it never carries `expected`, so
`unexpected_destination` is unreachable. The hit check at `replay.py:127` requires
`trigger_type` to match exactly. In the reviewed run **20 of 27 signatures are
`postcondition_timeout`**, and replaying against an unfixed device prints
`0/27 reproduced` followed by "Every reproduction is now clean — this is what a verified fix
looks like."
*Fix:* extract the oracle branch from `engine.py:151-176` into one function and call it from both
loops. Replay must measure `elapsed` and compare it against `postcondition_timeout_ms`, and the
schedule must carry `expectedState` (see B2).

**A2. `avgIntervalMs` has no effect on a walk worker.**  ✅ **done 2026-09-23** — a walk worker takes `pace: "response" | "interval"`, default unchanged so existing plans reproduce. `pace: "interval"` on the 8 h nightly drops 47,341 log entries to 2,688 — the 18x the config always meant
`engine.py:193` returns `now + elapsed` for `WorkerKind.WALK`; the configured interval is only
consulted when the component has no turn, which never happened because no transient state was
ever entered. Cadence is therefore set entirely by `postcondition_timeout_ms`: mean elapsed of
3,610 / 5,377 / 10,963 ms predicts shares of 49.9 / 33.5 / 16.5% against observed
50.0 / 33.6 / 16.4%. The config asks for one action per 90 s; the engine issued one per 3.6 s —
**25× more than intended** — so the "realistic user-paced walk" premise does not hold, and the
timeout budget silently controls how often a component is tested.
*Fix:* decide whether a walk worker paces on its own interval or on the device's response, and
make the config say which. This must land before the first real-hardware run: at 3.6 s intervals
against a device whose `api_connect_wifi` takes 30 s, "8 hours" stops meaning 8 hours.

**A3. `session.json` reports the configured duration as if it were the measured one.**  ✅ **done 2026-09-23** — `elapsedMs` = `max(t_ms + elapsed_ms)`, plus `stopReason` and `date`
`durationMs` is the plan's value. A run that crashed at hour two still reads 24H00M.
*Fix:* add `elapsedMs`, computed as `max(trigger.t_ms + elapsed_ms)` — not `max(t_ms)`, because
`run_step` completes synchronously, so the final action finishes slightly past the deadline. Add
`stopReason` (`CompleteDuration` / `Crash` / `PowerOff`) rather than leaving three consumers to
infer it from `result.crashed`, the final state and the duration.

**A4. `write_back` uses an unvalidated proposal id as a filename.**  ✅ **done 2026-09-23**
`analyze.py:117` writes `prop_dir / f"{p['id']}.json"`. Safe today because ids are
`PROP-{hash}` generated in Python; an LLM-produced id containing `/` or `..` writes outside the
folder.
*Fix:* character allowlist on `id`, enforced before the write, together with the rest of the
proposal envelope validation that an LLM analyst will need.

---

## B — Data that does not exist

**B1. Resource telemetry has no numeric values.**  ✅ **done 2026-09-23** — `Driver.sample_resources` — per-process PSS and fd counts, system CPU%. Telemetry draws on its own RNG, because sharing the session's changed which actions a probe fired
`BackgroundMonitor` (`engine.py:212-218`) records `cpu_load` and `mem_pressure` as a three-level
enum whose only writers are `inject_cpu_stress`, `inject_memory_stress` and
`clear_system_stress`. Over 2,880 samples the monitor read back exactly what the fault schedule
wrote: 121 Critical CPU samples are the injected hour, to the sample. A heap climb, an fd leak in
a reconnect loop, or a thread whose idle CPU is creeping would leave that table unchanged.
*Fix:* record numbers — per-process PSS and `fd` count, per-thread CPU — and derive the `Stress`
enum from them so guards and postconditions keep working unchanged. Read `/proc/stat`,
`/proc/<pid>/task/<tid>/stat` and `/proc/<pid>/smaps_rollup` rather than `top`.
**And keep the two measures apart:** memory is an accumulating level and supports a leak verdict
(fitted growth with an R² and a plateau test); per-thread CPU at 30 s is a differenced bursty
rate whose mean is dominated by burst count, so a drift in the idle floor arrives diluted by the
burst share and four separate instruments failed to recover planted floor drifts from it. Publish
CPU as a ranking to read beside a memory finding, never as a pass/fail.

**B2. `ActionSchedule` cannot be replayed from itself.**  ✅ **done 2026-09-23** — a header with `initialState`, `modelVersion` and the worker kinds; `expectedState` per entry; `guardHeld` removed; the seed deliberately stays out
It carries `workerId`, `actionName`, `scheduledOffsetMs`, `guardHeld`. The full-run file has no
`initialState` (only a distilled reproduction does), and nothing identifies the
`ActionDatabase` the names refer to.
*Fix:* a header element carrying `initialState`, `modelVersion` and the worker kinds. **Not the
seed** — in `ReplayEngine` the RNG is consumed only by `SimulatedDriver`, for deciding whether an
injected defect fires, and on a bench there is no seed at all; recording it would pin the device,
which is the half that must be free to differ. Add `expectedState` per entry so A1 has something
to check against. Remove `guardHeld`: it is an observation, nothing reads it, and the schedule
pins inputs only.

**B3. Entries sharing a `scheduledOffsetMs` have no defined order.**  ✅ **done 2026-09-23** — `seq` on every entry, and replay orders on `(scheduledOffsetMs, seq)`
7 offsets carry more than one entry across 15 entries, including three at offset 0
(`api_connect_wifi`, `api_bt_connect`, `api_start_matter`). The original run ordered them by the
event heap's insertion counter; `replay.py:62` reorders them with a stable sort on the offset, and
which of those three runs first decides what state the other two see.
*Fix:* a monotonic `seq` on every entry, and replay orders on `(scheduledOffsetMs, seq)`.

**B4. `session.json` cannot fill a report header.**  ✅ **done 2026-09-23** — `--dut`, `--release-version`, `--driver-version`, plus `runFolder` written by `save_run`
No `dut`, no `releaseVersion`; `driver` holds the class name `"SimulatedDriver"` rather than a
version. The run date and folder path survive only in the directory name
(`20260923-095312_CHAOS-003`).
*Fix:* add `dut`, `releaseVersion`, `driverVersion`, `runFolder` and `date`. The first two have
to come from the bench, so they belong in the config or on the command line.

**B5. Fault cleanup is not recorded.**  ✅ **done 2026-09-23** — `CLEANUP_FIRED` / `CLEANUP_LAPSED`, counted from the `PendingCleanup` instance. The reviewed run had 6 fired and **2 lapsed**
`PendingCleanup` schedules a deadline; neither the cleanup firing nor the deadline lapsing is
logged, and cleanup cannot be counted by action name because cleanup actions double as ordinary
API calls — `api_connect_wifi` appears 3,927 times, almost none of them as a cleanup.
*Fix:* log the `PendingCleanup` instance's resolution. A fault whose cleanup never fired leaves
the device perturbed for the remainder of the run and contaminates every figure after it. Two
cleanup-side judgements already failed in the reviewed run: `clear_system_stress` at 253% of its
budget and `inject_memory_stress` at 180%.

---

## C — Data that exists and is discarded

**C1. `visited_states` is tracked and never reported.**  ✅ **done 2026-09-23**
`CoverageTracker` records every `CompositeState` visited; `report()` does not return it. The
figure it would produce is the most honest number in the run: **27 of 101 legal composite states,
26.7%**, against a `CoverageReport` that currently leads with "24/24 transitions, 100%".

**C2. Coverage has one denominator where it needs two.**  ✅ **done 2026-09-23**
36 reached cells are 42.9% of the model's 84 and **97.3% of the 37 this configuration could
possibly reach**. Read alone, 42.9% describes a run that underperformed; it in fact reached all
but one cell available to it. The 47-cell shortfall belongs to the config and is fixed by adding a
probe or sweep worker, not by running longer.
*Fix:* report reached / plan ceiling / model total. The ceiling is computable statically from the
worker configs — a walk worker can only issue its component's chain edges, probe and sweep are
their pool crossed with their component's states, a schedule worker is its listed actions — plus
each fault's `cleanup_action`, since `PendingCleanup` bypasses the chain roll and reaches pairs
the chain never pairs.

**C3. Off-model transitions are dropped, including the anomalies.**  ✅ **done 2026-09-23**
`record.py:238-240` adds an edge only when the policy already has a row for it. 19 distinct
`(component, from_state, action)` edges were observed: 13 normal, **1 whose destination differs
from the chain's prediction** (`matter / Operational / api_start_matter` reached `Commissioning`
**273 times**, every call passing its postcondition so no signature was raised), 4 off-model by
design (fault injections), and **1 anomaly** (`wifi / Streaming / api_connect_wifi → Connected`
with the guard not held — the call should have been refused, the device did it, and the region
moved backwards). The six in the last three rows are counted neither as covered nor as anomalous.
*Fix:* carry an observed-edge set beside the model-edge set and classify four ways.

**C4. Per-state counts are normalised away.**  ✅ **done 2026-09-23**
`steps_in_state` holds raw counts and `report()` keeps only the share. The missing companion is
how many times each region *entered* the state; the two together give mean dwell — 1.00 means the
region never receives two consecutive calls there, and `matter=Commissioning` sits at exactly 1.00
across 2,537 visits, so the commissioning-in-progress window this framework exists to reach was
never entered twice.
*Fix:* report `entered`, `actionsFrom` and `meanDwellSteps` per state.

**C5. `observed_distribution` (time-weighted) has no consumer.**  ✅ **done 2026-09-23** — removed; replaced by `dwell`
Defined at `record.py:66`, assigned at `record.py:260`, read nowhere. Its stated purpose is
better served by per-action `elapsed_ms` against budget, and "stuck" is a time-series property a
distribution cannot express — two very different sessions produce the same shape.
*Fix:* replace it with max dwell / longest run without a state change, or drop it. The
step-weighted view stays: `run.py:74-88` compares it against the chain's stationary distribution,
which counts turns, so only the per-turn view is dimensionally comparable.

**C6. Concurrency is never reported.**  ✅ **done 2026-09-23**
**68.7% of actions (32,914 of 47,928) were issued while another worker's action was still in
flight**, and all 19 faults landed mid-flight. Nothing in `CoverageReport` or in any printed
summary says so; it had to be recomputed from the raw log, which means no earlier run was ever
checked for it.

**C7. The executor and category axes are absent.**  ✅ **done 2026-09-23**
`dut` actions ran 47,918 times, `equipment` actions **10 times** — 0.02% — and `executor` is a
deliberate axis of the model (`data_structure.md:127`). `data_structure.md:136` documents a
`faultInjectionCoverage` field that does not exist; `api` covered 13/13 while
`fault_injection` covered 18/19.

**C8. Coverage is silent about the transient window.**  ✅ **done 2026-09-23** — reported as `uncounted_states`, and named in the report
`Scanning`, `Connecting`, `Advertising`, `Pairing` and `Fabric_Removing` have no chain row, so
`chain_state()` returns `None` and `record.py`'s `if component and from_state:` drops the pair.
42.9% is not 0% and not 100% for that window — it is uncounted, and it is exactly where a fault
landing mid-transition would find a bug.

**C9. The reproduction is a copy, not a pointer.**  ✅ **done 2026-09-23** — `ErrorSignature.source_offset_ms`. The evidence went on the signature, not into the schedule, so the reproduction stays inputs only
`reproduction_window(n=6)` freezes six entries at the moment of failure and rebases their offsets
to the window start (0, 1932, 3351, …) while `ActionSchedule` uses run-absolute offsets
(0 … 86,398,845), so the slice cannot be located in the file it came from. Six is also arbitrary:
across four workers it is roughly 1.5 steps each, and an accumulating cause can be hundreds of
steps back.
*Fix:* carry `sourceOffsetMs` as an absolute range and let the window size be an analysis-time
parameter. Widening a window then costs nothing.

**C10. `signature_hash` is not stable across processes.**  ✅ **done 2026-09-23** — blake2s
It is `abs(hash(key)) % 16**8`, and Python randomises string hashing per process. The same
session re-run three times produced identical counts and identical reproductions under three
different hashes — `8b178fd0`, `e8bbb950`, `3b0efa10` — converging only under
`PYTHONHASHSEED=0`. Proposals are named `PROP-{hash}`, so a regression referenced by id cannot be
found again and a signature cannot be deduplicated against a previous run. Behaviour is otherwise
fully deterministic from the seed.

**C11. Replay has two verdicts where it needs three.**  ✅ **done 2026-09-23** — `observed_path` on the signature; diverging at the last step is what a fix looks like, diverging earlier means the test never ran
A walk worker's next turn is scheduled at `now + elapsed`, so the offsets in `ActionSchedule` are
partly a function of device response times. On a slower device, replaying at recorded offsets
issues the next action while the previous is still in flight, and the state each action encounters
diverges — a consequence of pinning inputs rather than results, and sometimes the interesting
case. But the run must not report it as a fix.
*Fix:* `REPRODUCED` / `NOT REPRODUCED, timing aligned` / `NOT REPRODUCED, timing shifted — not
comparable`, with the third excluded from the denominator.

**C12. Schedule loading has no up-front validation.**  ✅ **done 2026-09-23**
`ActionDatabase.get()` (`model.py:441`) raises a bare `KeyError`, and `replay.py:62` calls it
inside the execution loop, so a schedule with a typo on its 40th entry runs 39 entries and then
fails with state already modified. `data_structure.md:126` claims names are "validated against
`ActionDefinition` at load time".

---

## D — A document and the code disagreeing

**D1. Slide 01 draws a test case the code does not implement.**  ✅ **done 2026-09-23** — the slide's figure is generated by `pod_mbt.figure`, and `test_consistency` fails if it disagrees
The Blueprint's `bt` chain has **5 states** (Unpaired, Pairing, Connected, Disconnected,
Streaming); `policy.CHAINS` has **3** (Idle, Connected·None, Connected·Streaming). Slide 01 calls
Matter's third state `Commissioned`; the code calls it `Operational`. Slide 01's action names are
unprefixed. The reduction was deliberate — `sample_test_case.md` records that "unlike the old
five-state Bluetooth chain, three states per component saturates even under uniform choice" — and
the slide was not redrawn. `test_consistency.py` validates chains against the tables in
`data_structure.md`; it never reads Slide 01's drawing, so all its checks pass.
*Fix:* generate the figure from `CHAINS` instead of authoring it. See E1 — this is the same fix.

**D2. `sample_test_case.md`'s recorded figures no longer reproduce.**  ✅ **done 2026-09-23** — regenerated from real runs, and the false claim about the suite guarding them removed
NIGHTLY-014 matches exactly. CHAOS-003's drift moved 0.008/0.002/0.055 → 0.002/0.001/0.049;
PROBE-001's pairs moved 83/84 → 81/84 and its drift 0.259/0.209/0.233 → 0.289/0.205/0.200;
MIXED-002's pairs moved 34/84 → 35/84 with matter drift 0.399 → 0.269. MIXED-002 runs with no
injected defect, so its change is a code change, not a dice roll. The document claims
`test_consistency.py` "re-runs all four to check these claims still hold"; the suite never reads
the document and never runs `testcases/TC-01/chaos.json`.
*Fix:* either have the suite assert the documented figures, or generate the document from a run.

---

## E — Structure

**E1. A test case has no single home.**  ✅ **done 2026-09-23**
TC-01 exists as four independent copies: the chains in `policy.py`, the actions in `model.py`, the
plan in `testcases/TC-01/chaos.json`, and a hand-drawn figure in the Blueprint. D1 is the predictable
result. Slide 01 already carries a matrix editor whose own note says "the matrix is the source of
truth here, not the chain" — and it persists nowhere.
Worse, nothing records which test case a run used. All six configs walk the same three chains
from the same `CHAINS` and the same `ACTION_DB`, so **there is exactly one test case — TC-01 — and
six plans against it**: CHAOS-003 (walk ×3, 24 h, 13 faults), NIGHTLY-014 (walk ×3, 8 h, 6),
PROBE-001 (probe ×3, 4 h, 2), MIXED-002 (walk ×2 + probe, 8 h, 4), SWEEP-001 (sweep ×3, 8 h, 0),
FUZZ-001 (probe ×4, 1 h, 0). None of them carries a `testCase` field, and the Blueprint names only
TC-01 while `sample_test_case.md` names only the four sessions — so "which test case did CHAOS-003
run?" is answerable by inference and nowhere else. A report that asserts the link is asserting its
author's reading.

*Fix:* one file per test case (`testcases/TC-01.json`: states, the matrix with an action and a
probability per cell, and the fault list), loaded by `MarkovPolicy` and validated by
`validate_chains` on load. Every config gains a `testCase` field naming it, and `session.json`
carries it through, so a report states the link instead of inferring it. Then extract the figure
renderer into `pod_mbt/figure.py` — done — so the Blueprint slide, the run summary and the test
report all call it on the same file. "The report's figure matches the Blueprint's" stops being
something anyone maintains.
Keep the vocabulary straight while doing it: a **test case** is the model, a **plan** is one way
to walk it, a **session** is one execution of a plan. `sessionId` is currently doing all three
jobs, which is why the Summary block had a row labelled "Test Case" holding a session id.

**E2. `Power.OFF` is unreachable.**  ✅ **done 2026-09-23** — decided: kept, because a bench can cut power; recorded as a known dead path rather than removed
No effect anywhere sets it — `inject_power_reboot`'s postcondition explicitly requires
`power = Active` and it passed in 31.3 s of its 50 s budget, resetting the three regions. So the
enum value, the `inconsistencies()` check against it, and the `engine.py:291` break on it are
three dead paths guarding a state the model cannot enter.
*Fix:* decide whether a powered-off state is in scope. If it is, an action has to produce it; if
it is not, remove all three.

**E3. `inconsistencies()` carries only two rules, so the coverage denominator is set by omission.**  ✅ **done 2026-09-23** — `Fabric_Removing` with no network ruled illegal; the 15 wifi/matter combinations deliberately left legal, with the reasoning written down. 101 legal states → 96
It rejects `bt_audio` streaming without a connected link (120 combinations) and regions still
active after power off (158), leaving 101 of 320. Nothing concerns Wi-Fi and Matter together, so
15 combinations such as `wifi=Disconnected | matter=Operational` count as legal because no one
ruled on them. Some are genuinely reachable — a commissioned fabric stays Operational while the
network is down, and commissioning can run over BLE — but `matter=Fabric_Removing` with no network
deserves its own decision. The denominator is sensitive to the call: ruling those 15 out moves
101 → 86 and 26.7% → 31.4%.
*Fix:* rule on them explicitly. The same function is `replay`'s test for "this reproduction starts
from a state only a broken device reaches", so a missing rule weakens two things at once.

**E4. The report's fixed parts have no shared home, so the two analysts will diverge.**  ✅ **done 2026-09-23** — `skeleton()` + `SLOTS` + `LAYOUT` in `analyze.py`; `Analysis` now carries `sections` / `slots` / `assets` / `proposals`, and `write_back` rejects a slot key or a proposal id outside its allowlist (which also closes A4).
Title, the Summary field list, the test-case figure and every computed table are the same for
every run and for every analyst — only the interpretation differs. Building them inside
`RuleBasedAnalyst.analyze()` means an `LLMAnalyst` reimplements them, which is D1's failure mode
reproduced inside the one file whose docstring exists to prevent it.
*Fix:* a module-level `skeleton(run_dir) -> Report` in `analyze.py` that both implementations
call, holding the title template, the Summary block, the figure call and all the tables. The
figure renderer itself goes in `pod_mbt/figure.py` (E1) because the Blueprint is its second
consumer. `Analysis` then needs structure rather than one `report_md` string —
`sections: dict[str, str]` filled by the skeleton plus `slots: dict[str, str]` filled by the
analyst against a fixed key allowlist — so an LLM writes into named places and cannot touch a
figure. On present evidence the skeleton is most of the report and the slots are four:
a verdict, per-finding significance, cross-file correlation, and the proposals.

---

## Status

All 27 items above are done, in one session on 2026-09-23. Worth keeping in mind about that:

- **Six were behaviour changes, not refactors.** A1 changed what `replay` reports, A2 gave a
  walk worker a pacing choice, B1 added telemetry the enum could not carry, C10 changed every
  `signature_hash`, C11 added a third verdict, and E3 moved the legal-state denominator from 101
  to 96. `modelVersion` stayed `53dc995cfce9` throughout, which is the check that the *model* did
  not move underneath them.
- **Two of the fixes found new things.** B5 immediately reported **2 cleanups lapsed** in a
  24-hour chaos run, invisible until it was measured. C11 reported **7 of 27 reproductions not
  comparable** against a fixed device, because the six-action window contains earlier instances of
  the same defect — which is C9's "six is arbitrary" arriving as a concrete consequence rather
  than an argument.
- **Two fixes were nearly silent behaviour changes and had to be caught.** Storing the chains as
  a matrix reorders each row, and `random.choices` reads a row as a list, so the same seed picks a
  different edge — hence the `i` field in `testcases/TC-01.json`. And `sample_resources` drawing on
  the session RNG shifted every subsequent worker selection, so a probe reached seven more cells
  just because a monitor started taking readings; telemetry now has its own stream. Both were
  caught by `test_consistency` asserting figures from earlier runs, which is the suite doing
  exactly what its docstring says it is for.

The three left standing as decisions rather than code: **E2** keeps `Power.OFF` reachable in
principle with nothing producing it, because a bench can cut power. **E3** deliberately leaves
`wifi = Disconnected` with `matter = Operational` legal — a commissioned fabric stays Operational
while the network is down. **A2** defaults to the old pacing so existing plans reproduce; a plan
that wants user-like timing has to ask, and it must ask before the first real-hardware run.
