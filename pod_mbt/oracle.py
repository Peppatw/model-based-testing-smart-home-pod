"""The oracle: given what was issued and what the device did, which assertion applies.

This module exists because there were two copies of it. `EngineLoop.run_step` judged four ways
and `ReplayEngine.replay` judged two, and the missing two were not a gap in coverage — they made
replay structurally unable to reproduce most of what a run finds. In one 24-hour session, 20 of
27 signatures were `postcondition_timeout`; replaying them against an unfixed device printed
`0/27 reproduced` and then "Every reproduction is now clean — this is what a verified fix looks
like." A regression mechanism that reports a fix it cannot see is worse than no mechanism.

So there is one copy, here, and both loops call it. The four readings, in the order they are
decided:

* the guard did not hold -> the only correct outcome is a refusal that moved nothing, and
  anything else is `illegal_call_accepted` — the ambiguous finding, because either the guard is
  stricter than the device needs or the device is missing a check;
* the guard held and the call took longer than its declared budget -> `postcondition_timeout`;
* the guard held and the postcondition is false -> `postcondition_failure`;
* the guard held, the postcondition is true, and the chain named a destination the device did not
  reach -> `unexpected_destination`, which a postcondition cannot catch on its own because the
  call worked; what is wrong is the model's idea of the graph.

`unexpected_destination` is checked last and overrides, because a call can satisfy its own
contract and still land somewhere the chain did not predict — that disagreement is the finding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .driver import DriverResult
from .model import ActionDefinition, CompositeState

# The refusal a call whose guard did not hold is held to. Kept as a constant because it is
# logged as `expected` text and a person reads it beside the guard that produced it.
REFUSAL = "state unchanged ∧ error returned ∧ no crash"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    trigger: str        # postcondition_failure | postcondition_timeout |
                        # illegal_call_accepted | unexpected_destination
    detail: str         # what was expected, in the words a person reads


def judge(action: ActionDefinition, before: CompositeState, after: CompositeState,
          result: DriverResult, elapsed_ms: int, guard_held: bool,
          expected_state: Optional[str] = None) -> Verdict:
    """One call, one verdict. No I/O, no logging, no state — so both loops can share it.

    `before` and `after` are whole composite states rather than region names, because the
    refusal check is "nothing moved anywhere", not "this component did not move".
    `expected_state` is what the chain said this transition would reach, and is `None` for a
    worker that never consulted a chain.
    """
    if not guard_held:
        ok = (not result.accepted
              and after.as_tuple() == before.as_tuple()
              and not result.crashed)
        return Verdict(ok, "illegal_call_accepted", REFUSAL)

    timed_out = elapsed_ms > action.postcondition_timeout_ms
    held = action.postcondition(after)
    verdict = Verdict(held and not timed_out,
                      "postcondition_timeout" if timed_out else "postcondition_failure",
                      action.postcondition_text)

    # Checked last, and it overrides: a call can pass its own contract and still arrive
    # somewhere the chain did not name.
    reached = after.chain_state(action.component)
    if expected_state and reached and reached != expected_state:
        return Verdict(False, "unexpected_destination",
                       f"chain expected {expected_state}, reached {reached}")
    return verdict
