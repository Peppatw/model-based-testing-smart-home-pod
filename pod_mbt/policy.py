"""MarkovPolicy — one chain per component, and the maths that falls out of it.

Three chains, twenty-four transitions. Each transition says three things: the action that causes it,
the state it is expected to reach, and how likely a worker is to take it. Every state's
transitions sum to 1.

Two decisions shape this file.

**The transition belongs to the chain, not to the action.** An `ActionDefinition` says when a
call is legal and what must be true afterwards; it says nothing about where it starts.
The same call lands differently depending on where it is issued from — `api_bt_disconnect`
from `Connected` reaches `Idle`, from `Idle` it is refused and nothing moves — so the
transition is a property of the graph.

**Faults are not in the chain.** A chain answers "this worker is acting now, what does it
do" — a distribution over choices. A fault is a rate per unit time: the AP reboots, the
peer walks away, the power drops, and none of it waits for a worker's turn. Faults are
fired by a clock (see `plan.py`), which is also the only way one can land while another
action is still in flight.

What the chain gives you, beyond a walk: multiply the matrix by itself until it settles
and you have the share of time a healthy device spends in each state. That is a testable
steady state — the thing chaos engineering usually has to approximate with CPU graphs.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from .model import ACTION_DB, Category, CompositeState, Executor


@dataclass(frozen=True)
class Transition:
    """One transition: the action that causes it, where it lands, how likely it is.

    `nxt` is the *expected* destination — what the model says should happen. Whether it
    did is the oracle's question, answered from the action's postcondition, and the two
    disagreeing is itself a finding.
    """

    action: str
    nxt: str
    p: float


# component -> state -> transitions out of it. Only what a person or an app can do; anything
# that happens *to* the device is a fault, and faults are scheduled, not rolled.
CHAINS: dict[str, dict[str, list[Transition]]] = {
    "wifi": {
        "Disconnected": [
            Transition("api_connect_wifi", "Connected", 0.75),
            Transition("api_inject_wrong_psk", "Disconnected", 0.15),
            Transition("api_inject_wrong_ipaddr", "Disconnected", 0.10),
        ],
        "Connected": [
            Transition("api_streaming_uplink", "Streaming", 0.50),
            Transition("api_disconnect_wifi", "Disconnected", 0.30),
            Transition("api_get_dhcp", "Connected", 0.10),
            Transition("api_get_ipadd", "Connected", 0.10),
        ],
        "Streaming": [
            Transition("api_streaming_downlink", "Streaming", 0.60),
            Transition("api_disconnect_wifi", "Disconnected", 0.40),
        ],
    },
    "bt": {
        "Idle": [
            Transition("api_bt_pairing", "Connected · None", 0.50),
            Transition("api_bt_connect", "Connected · None", 0.25),
            Transition("api_bt_disconnect", "Idle", 0.10),          # issuable, expected refused
            Transition("api_inject_wrong_devicename", "Idle", 0.10),
            Transition("api_inject_no_peer", "Idle", 0.05),
        ],
        "Connected · None": [
            Transition("api_bt_audio_streaming", "Connected · Streaming", 0.65),
            Transition("api_bt_disconnect", "Idle", 0.35),
        ],
        "Connected · Streaming": [
            Transition("api_bt_disconnect", "Idle", 0.70),
            Transition("api_bt_audio_streaming", "Connected · Streaming", 0.30),
        ],
    },
    "matter": {
        "Uncommissioned": [
            Transition("api_start_matter", "Commissioning", 0.80),
            Transition("api_remove_matter_fabric", "Uncommissioned", 0.20),
        ],
        "Commissioning": [
            Transition("api_complete_matter_commissioning", "Operational", 0.65),
            Transition("inject_wrong_setup_code", "Uncommissioned", 0.35),
        ],
        "Operational": [
            Transition("api_remove_matter_fabric", "Uncommissioned", 0.70),
            Transition("api_start_matter", "Operational", 0.30),
        ],
    },
}

# Everything the chains do not contain. Power and resource pressure live here too: no
# user action turns a Pod off, the bench does.
FAULTS: list[str] = sorted(
    a.name for a in ACTION_DB
    if not any(e.action == a.name for c in CHAINS.values() for r in c.values() for e in r)
)


class MarkovPolicy:
    """Rolls one row. Nothing filters it first — the printed number is the rolled number."""

    def __init__(self, chains: dict[str, dict[str, list[Transition]]] | None = None,
                 rng: random.Random | None = None):
        self.chains = chains if chains is not None else CHAINS
        self.rng = rng or random.Random()

    def row(self, component: str, state: str) -> list[Transition]:
        return self.chains.get(component, {}).get(state, [])

    def next_transition(self, component: str, state: str) -> Optional[Transition]:
        row = self.row(component, state)
        if not row:
            return None
        return self.rng.choices(row, weights=[e.p for e in row], k=1)[0]

    # ------------------------------------------------------------------ maths
    def states(self, component: str) -> list[str]:
        return list(self.chains.get(component, {}))

    def matrix(self, component: str) -> list[list[float]]:
        """The chain as a matrix. Two transitions into the same state add up here, which is why
        the matrix is a view for humans and for arithmetic, never what the engine reads."""
        names = self.states(component)
        idx = {s: i for i, s in enumerate(names)}
        m = [[0.0] * len(names) for _ in names]
        for s, row in self.chains[component].items():
            for e in row:
                m[idx[s]][idx[e.nxt]] += e.p
        return m

    def stationary(self, component: str, iterations: int = 2000) -> dict[str, float]:
        """Where a long run spends its time — the steady state a chaos run is measured
        against. Observed time-in-state drifting from this is a finding no single
        postcondition can catch: every step passed, and the device still got stuck."""
        names = self.states(component)
        if not names:
            return {}
        m = self.matrix(component)
        v = [1.0 / len(names)] * len(names)
        for _ in range(iterations):
            v = [sum(v[i] * m[i][j] for i in range(len(names))) for j in range(len(names))]
        return {names[i]: round(v[i], 4) for i in range(len(names))}

    def transitions(self) -> set[tuple[str, str, str]]:
        """(component, state, action) — the coverage denominator for a state-led run."""
        return {(c, s, e.action) for c, rows in self.chains.items()
                for s, row in rows.items() for e in row}


def validate_chains(db=ACTION_DB, chains: dict | None = None) -> list[str]:
    """Checks a person would otherwise do by eye, and would eventually stop doing."""
    chains = chains if chains is not None else CHAINS
    known = {a.name: a for a in db}
    problems: list[str] = []
    for comp, rows in chains.items():
        for state, row in rows.items():
            total = sum(e.p for e in row)
            if abs(total - 1.0) > 1e-6:
                problems.append(f"{comp}/{state} sums to {total:.4f}, not 1")
            if len(row) < 2:
                problems.append(f"{comp}/{state} has one exit — no choice to weight")
            for e in row:
                if e.action not in known:
                    problems.append(f"{comp}/{state} names unknown action {e.action!r}")
                elif known[e.action].component != comp:
                    problems.append(f"{comp}/{state} lists {e.action!r}, a "
                                    f"{known[e.action].component} action")
                if e.nxt not in rows:
                    problems.append(f"{comp}/{state} → {e.nxt!r}, which is not a state "
                                    f"of this chain")
    for name in FAULTS:                      # a fault the bench cannot cause is a mistake
        a = known[name]
        if a.category is Category.API and a.executor is Executor.DUT:
            problems.append(f"{name} is an ordinary API call but sits outside every chain")
    return problems
