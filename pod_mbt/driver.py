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

from .model import ActionDefinition, CompositeState, Stress


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

    def elapsed_ms(self, action: ActionDefinition, state: CompositeState,
                   rng: "random.Random | None" = None) -> int:
        """How long the device took. Measured on a bench; modelled by a simulator.

        This is the device's property, not the loop's, which is why it lives here — and moving
        it here is what lets Replay judge a timeout at all. `rng` is passed in rather than taken
        from the driver: the *shape* of the delay belongs to the device model, while the dice
        belong to the session, and a simulated driver that rolled its own would consume the seed
        out of step with the loop that drives it. A real driver ignores `rng` entirely.
        """
        raise NotImplementedError

    def sample_resources(self, state: CompositeState, t_ms: int,
                         rng: "random.Random | None" = None) -> dict:
        """What the device is carrying right now, in numbers.

        A device property, like `elapsed_ms`, which is why it lives here. On a bench this reads
        `/proc/stat`, `/proc/<pid>/task/<tid>/stat` and `/proc/<pid>/smaps_rollup` — not `top`,
        whose first iteration reports an average since boot and whose columns differ between
        busybox and procps. Here it is modelled.

        The three-level `Stress` enum this replaces could not carry a leak: its only writers were
        `inject_cpu_stress`, `inject_memory_stress` and `clear_system_stress`, so the monitor read
        back exactly what the fault schedule wrote. A heap climb, an fd leak in a reconnect loop,
        or a thread whose idle CPU is creeping all produced an identical series.
        """
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
        # How long the session is meant to run, so a modelled drift can be expressed as a share
        # of it rather than as a rate nobody chose. Set by `Session`.
        self._session_ms = 8 * 3600 * 1000

    def elapsed_ms(self, action: ActionDefinition, state: CompositeState,
                   rng: random.Random | None = None) -> int:
        """Virtual duration, stretched by whatever pressure the device is under — which is what
        makes `inject_cpu_stress` mean something rather than flip a flag."""
        r = rng or self.rng
        base = action.postcondition_timeout_ms
        load = {Stress.NORMAL: 1.0, Stress.ELEVATED: 1.8, Stress.CRITICAL: 3.2}
        factor = load[state.cpu_load] * (1.0 if state.mem_pressure is Stress.NORMAL else 1.4)
        return max(500, int(r.uniform(0.15, 0.75) * base * factor))

    # One entry per modelled process: baseline PSS in kB, baseline fd count, and the fraction
    # each grows over a full session. Memory is an accumulating level, so a leak here is a slope
    # and nothing dilutes it — which is why memory supports a verdict and per-thread CPU does not.
    _PROCS = {
        "wpa_supplicant":  (14_600,  34, 0.00),
        "netd":            (20_800,  52, 0.00),
        "bluetoothd":      (59_400,  88, 0.17),
        "com.pod.matter": (115_400,  96, 0.46),   # the leak worth finding
        "audioserver":     (42_200,  41, 0.00),
        "logd":            (22_100,  23, 0.09),   # under a 10% gate on purpose
    }
    _FD_DRIFT = {"com.pod.matter": 0.64}          # sockets not closed on re-subscribe

    def sample_resources(self, state: CompositeState, t_ms: int,
                         rng: random.Random | None = None) -> dict:
        """Modelled telemetry. Deterministic in `t_ms`, so a seed reproduces the series."""
        r = rng or self.rng
        frac = min(1.0, t_ms / max(1, self._session_ms))
        load = {Stress.NORMAL: 0.0, Stress.ELEVATED: 28.0, Stress.CRITICAL: 62.0}
        procs, total_pss = {}, 0
        for name, (base, fds, drift) in self._PROCS.items():
            pss = int(base * (1 + drift * frac) + r.gauss(0, base * 0.012))
            fd = int(fds * (1 + self._FD_DRIFT.get(name, 0.0) * frac)) + r.choice([0, 0, 1, -1])
            procs[name] = {"pss_kb": max(0, pss), "fd_count": max(0, fd)}
            total_pss += max(0, pss)
        return {
            "cpu_pct": round(min(100.0, 18.0 + load[state.cpu_load] + r.gauss(0, 3.0)), 2),
            "mem_available_kb": max(0, int(420_000 - total_pss * 0.55 + r.gauss(0, 1_200))),
            "processes": procs,
        }

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
