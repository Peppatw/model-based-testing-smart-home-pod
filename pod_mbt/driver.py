"""The Driver layer — the only place that knows whether hardware is real.

Fig. 3's `Handshake` branch is the single point where simulation and real hardware
diverge; everything downstream is the same graph either way. `SimulatedDriver` is what
makes this package runnable without a bench.

Note what the driver does NOT do: it never consults a guard. Whether an action should
have been allowed is the engine's question (`guardMode`); the driver's job is to issue
the call and report what the device did — which is exactly the split that makes
`guardMode: none` meaningful.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass

from .model import ActionDefinition, CompositeState


@dataclass
class DriverResult:
    accepted: bool          # did the device act on the call at all
    error: str | None       # error string if it refused
    # Never set by SimulatedDriver — a simulator has no way to die. It is read in five
    # places (the oracle here and in replay, the loop's exit condition, session.json), so
    # a real Driver that reports a panic lights all of them up without another change.
    crashed: bool = False


class Driver:
    """Interface. A real bench implementation swaps in here and nothing else moves.

    `device_log` is the device's own output — read off UART on a bench, written by the
    simulator here. It is deliberately not the framework's log: comparing what the
    framework believes against what the device said is most of what a person does with
    a failure, and that comparison needs both, kept apart.
    """

    device_log: list[str]

    def execute(self, action: ActionDefinition, state: CompositeState
                ) -> tuple[CompositeState, DriverResult]:
        raise NotImplementedError

    def observe(self, state: CompositeState) -> CompositeState:
        """State Observer — ground truth read back from the device.

        Nothing calls this yet: the engine trusts the state `execute()` hands back, which
        on a simulator is the same thing. On a bench it is not — what the framework
        believes and what the device reports can differ, and that difference is most of
        what a person looks at after a failure. A real Driver overrides this and the loop
        gains a second opinion; until then it is an identity function on purpose.
        """
        return state


class SimulatedDriver(Driver):
    """A cooperative device model, plus optional deliberate defects.

    `defects` exists so a demo run produces findings instead of a clean slide:

      accepts_illegal   the device acts on a call its guard would have blocked —
                        precisely what `guardMode: none` is built to catch
      slow_matter       commissioning sometimes exceeds its postcondition budget
      flaky_dhcp        api_connect_wifi reports success without connecting
    """

    def __init__(self, rng: random.Random | None = None,
                 defects: frozenset[str] = frozenset()):
        self.rng = rng or random.Random()
        self.defects = defects
        self.device_log: list[str] = []
        self._lock = threading.Lock()

    def execute(self, action: ActionDefinition, state: CompositeState
                ) -> tuple[CompositeState, DriverResult]:
        with self._lock:
            legal = action.guard(state)

            if not legal:
                if "accepts_illegal" in self.defects and self.rng.random() < 0.5:
                    # The bug worth finding: state moves on a call that should have
                    # been refused. Nothing about this looks wrong to the driver.
                    self.device_log.append(f"{action.component}: {action.name} applied")
                    return action.effect(state), DriverResult(accepted=True, error=None)
                self.device_log.append(
                    f"{action.component}: {action.name} refused ({action.guard_text})")
                return state, DriverResult(
                    accepted=False, error=f"rejected: {action.guard_text} not satisfied")

            new = action.effect(state)
            self.device_log.append(f"{action.component}: {action.name} applied")

            if action.name == "api_connect_wifi" and "flaky_dhcp" in self.defects \
                    and self.rng.random() < 0.25:
                # Answers "done" and stays disconnected. A caller checking only whether
                # the call returned sees success; the postcondition, which checks the
                # state rather than the reply, does not.
                return state, DriverResult(accepted=True, error=None)

            if action.name == "api_complete_matter_commissioning" \
                    and "slow_matter" in self.defects and self.rng.random() < 0.3:
                return state, DriverResult(accepted=True, error=None)  # never resolves

            return new, DriverResult(accepted=True, error=None)
