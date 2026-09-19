"""States, actions, and the Action Database.

Mirrors docs/data_structure.md. Guards are stored twice on purpose: `guard_text` is
what the Blueprint prints, `guard` is what GuardCheck actually evaluates. Keeping both
means the document and the code cannot drift silently — test_consistency.py compares
them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional


# --------------------------------------------------------------------------- enums
class Power(str, Enum):
    ACTIVE = "Active"
    OFF = "Power_Off"


class Wifi(str, Enum):
    DISCONNECTED = "Disconnected"
    SCANNING = "Scanning"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    STREAMING = "Streaming"


class BtLink(str, Enum):
    IDLE = "Idle"
    ADVERTISING = "Advertising"
    PAIRING = "Pairing"
    CONNECTED = "Connected"


class BtAudio(str, Enum):
    NONE = "None"
    STREAMING = "Streaming"


class Matter(str, Enum):
    UNCOMMISSIONED = "Uncommissioned"
    COMMISSIONING = "Commissioning"
    OPERATIONAL = "Operational"
    FABRIC_REMOVING = "Fabric_Removing"


class Uplink(str, Enum):
    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    DOWN = "Down"


class Stress(str, Enum):
    NORMAL = "Normal"
    ELEVATED = "Elevated"
    CRITICAL = "Critical"


class Category(str, Enum):
    API = "api"
    FAULT = "fault_injection"


class Executor(str, Enum):
    DUT = "dut"
    EQUIPMENT = "equipment"


class EventType(str, Enum):
    ACTION_TRIGGER = "action_trigger"
    STATE_CHANGE = "state_change"
    ORACLE_RESULT = "oracle_result"
    # Emitted by nothing today: like DriverResult.crashed, this is the shape a real
    # bench's failure takes, kept so the log format does not have to change when one
    # arrives. ASYNC_MISSED is the same — a scheduled fault the clock could not land.
    SYS_PANIC = "sys_panic"
    TELEMETRY = "resource_snapshot"
    ASYNC_FIRED = "async_fault_fired"
    ASYNC_MISSED = "async_fault_missed"


# ----------------------------------------------------------------- composite state
@dataclass(frozen=True)
class CompositeState:
    """Six parallel regions plus the flat environment variables.

    `uplink`, `cpu_load` and `mem_pressure` are deliberately NOT regions: folding an
    external dependency into the coverage tuple is how state explosion comes back
    (docs/data_structure.md). They are read by guards and postconditions only.
    """

    power: Power = Power.ACTIVE
    wifi: Wifi = Wifi.DISCONNECTED
    bt_link: BtLink = BtLink.IDLE
    bt_audio: BtAudio = BtAudio.NONE
    matter: Matter = Matter.UNCOMMISSIONED
    # flat, not part of the coverage tuple
    uplink: Uplink = Uplink.HEALTHY
    cpu_load: Stress = Stress.NORMAL
    mem_pressure: Stress = Stress.NORMAL

    def as_tuple(self) -> tuple:
        """What CoverageTracker counts. Flat variables are excluded on purpose."""
        return (
            self.power.value,
            self.wifi.value,
            self.bt_link.value,
            self.bt_audio.value,
            self.matter.value,
        )

    def chain_state(self, component: str) -> Optional[str]:
        """This component's state as the chain names it, or None when it is mid-transition.

        The chains hold stable states only. While `api_connect_wifi` is in flight the
        device is in `Connecting`, which no chain lists — a stepwise worker has no turn
        there, and a fault that wants that window has to be scheduled.
        """
        if component == "wifi":
            return {Wifi.DISCONNECTED: "Disconnected",
                    Wifi.CONNECTED: "Connected",
                    Wifi.STREAMING: "Streaming"}.get(self.wifi)
        if component == "bt":
            if self.bt_link is BtLink.CONNECTED:
                return f"Connected · {self.bt_audio.value}"
            return {BtLink.IDLE: "Idle"}.get(self.bt_link)
        if component == "matter":
            return {Matter.UNCOMMISSIONED: "Uncommissioned",
                    Matter.COMMISSIONING: "Commissioning",
                    Matter.OPERATIONAL: "Operational"}.get(self.matter)
        return None

    def inconsistencies(self) -> list[str]:
        """Combinations no healthy device can be in.

        A run with `guardMode: none` against a device that accepts illegal calls can
        leave the model here — audio streaming over a link that is down, say. That is
        the finding. It matters again at replay time: a reproduction whose *initial*
        state is one of these is only reachable on a broken device, so replaying it
        against a fixed one proves nothing either way.
        """
        bad = []
        if self.bt_link is not BtLink.CONNECTED and self.bt_audio is not BtAudio.NONE:
            bad.append(f"bt_audio = {self.bt_audio.value} while bt_link = {self.bt_link.value}")
        if self.power is Power.OFF and (self.wifi is not Wifi.DISCONNECTED
                                        or self.bt_link is not BtLink.IDLE
                                        or self.matter is not Matter.UNCOMMISSIONED):
            bad.append("regions still active while power = Power_Off")
        return bad

    def to_dict(self) -> dict:
        """So a reproduction can record where it started from."""
        return {k: (v.value if isinstance(v, Enum) else v)
                for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "CompositeState":
        types = {"power": Power, "wifi": Wifi, "bt_link": BtLink,
                 "bt_audio": BtAudio, "matter": Matter, "uplink": Uplink,
                 "cpu_load": Stress, "mem_pressure": Stress}
        return cls(**{k: (types[k](v) if k in types else v) for k, v in d.items()})

    def pretty(self) -> str:
        return (
            f"{self.power.value}|wifi={self.wifi.value}"
            f"|bt={self.bt_link.value}/{self.bt_audio.value}|matter={self.matter.value}"
        )


Guard = Callable[[CompositeState], bool]
Effect = Callable[[CompositeState], CompositeState]
Post = Callable[[CompositeState], bool]


@dataclass(frozen=True)
class ActionDefinition:
    """One call the framework can make, and the contract it is held to.

    Deliberately says nothing about where it starts or where it lands. The same call is
    issuable from many states, and what it does depends on the state it finds — so the
    transition belongs to the chain (MarkovPolicy), not here. What lives here is the contract:
    `guard` says when the call is legal, `postcondition` says what must be true once it
    has been accepted. Between them they judge every call, in either kind of run.
    """

    name: str
    category: Category
    executor: Executor
    component: str
    guard_text: str
    guard: Guard
    postcondition_text: str
    postcondition: Post
    effect: Effect
    postcondition_timeout_ms: int = 10_000
    cleanup_action: Optional[str] = None


# --------------------------------------------------------------- the 32 actions
def _s(state: CompositeState, **kw) -> CompositeState:
    return replace(state, **kw)


def _connected_wifi(s: CompositeState) -> CompositeState:
    return _s(s, wifi=Wifi.CONNECTED)


def _drop_wifi(s: CompositeState) -> CompositeState:
    return _s(s, wifi=Wifi.DISCONNECTED)


def _connected_bt(s: CompositeState) -> CompositeState:
    return _s(s, bt_link=BtLink.CONNECTED, bt_audio=BtAudio.NONE)


def _drop_bt(s: CompositeState) -> CompositeState:
    return _s(s, bt_link=BtLink.IDLE, bt_audio=BtAudio.NONE)


def _build() -> list[ActionDefinition]:
    A = ActionDefinition
    api, fault = Category.API, Category.FAULT
    dut, eq = Executor.DUT, Executor.EQUIPMENT
    out: list[ActionDefinition] = []

    # ---- wifi / dut -------------------------------------------------------
    out += [
        A("api_connect_wifi", api, dut, "wifi",
          "wifi = Disconnected", lambda s: s.wifi is Wifi.DISCONNECTED,
          "wifi = Connected within 30s",
          lambda s: s.wifi is Wifi.CONNECTED,
          _connected_wifi, postcondition_timeout_ms=30_000),
        A("api_disconnect_wifi", api, dut, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "wifi = Disconnected within 5s", lambda s: s.wifi is Wifi.DISCONNECTED,
          _drop_wifi, postcondition_timeout_ms=5_000),
        A("api_get_dhcp", api, dut, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "lease renewed ∧ wifi unchanged within 10s",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          lambda s: s),
        A("api_get_ipadd", api, dut, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "address read back ∧ wifi unchanged within 3s",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          lambda s: s, postcondition_timeout_ms=3_000),
        A("api_streaming_uplink", api, dut, "wifi",
          "wifi = Connected", lambda s: s.wifi is Wifi.CONNECTED,
          "wifi = Streaming within 5s", lambda s: s.wifi is Wifi.STREAMING,
          lambda s: _s(s, wifi=Wifi.STREAMING), postcondition_timeout_ms=5_000),
        A("api_streaming_downlink", api, dut, "wifi",
          "wifi = Streaming", lambda s: s.wifi is Wifi.STREAMING,
          "wifi = Streaming within 5s", lambda s: s.wifi is Wifi.STREAMING,
          lambda s: s, postcondition_timeout_ms=5_000),
        A("api_inject_wrong_psk", fault, dut, "wifi",
          "wifi = Disconnected", lambda s: s.wifi is Wifi.DISCONNECTED,
          "wifi = Disconnected ∧ lastError = auth_failure",
          lambda s: s.wifi is Wifi.DISCONNECTED,
          # A failed attempt with a bad password: the Pod stays Disconnected and the
          # very next api_connect_wifi can still succeed — there is no stored credential
          # left to wipe, so this needs no cleanupAction.
          lambda s: s),
        A("api_inject_wrong_ipaddr", fault, dut, "wifi",
          "wifi = Disconnected", lambda s: s.wifi is Wifi.DISCONNECTED,
          "wifi = Disconnected ∧ lastError = invalid_ip_config",
          lambda s: s.wifi is Wifi.DISCONNECTED,
          lambda s: s),
    ]

    # ---- wifi / equipment -------------------------------------------------
    out += [
        A("inject_wifi_drop", fault, eq, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "wifi = Disconnected within 5s", lambda s: s.wifi is Wifi.DISCONNECTED,
          _drop_wifi, postcondition_timeout_ms=5_000, cleanup_action="api_connect_wifi"),
        A("inject_router_reboot", fault, eq, "wifi",
          "power = Active", lambda s: s.power is Power.ACTIVE,
          "NetworkUplink returns to Healthy within 90s",
          lambda s: s.uplink is Uplink.HEALTHY,
          # Rebooting the router restores the uplink — which is why this fault needs
          # no cleanupAction. Without this the simulator strands uplink at Down.
          lambda s: _s(s, uplink=Uplink.HEALTHY), postcondition_timeout_ms=90_000),
        A("inject_internet_down", fault, eq, "wifi",
          "NetworkUplink = Healthy", lambda s: s.uplink is Uplink.HEALTHY,
          "NetworkUplink = Down ∧ wifi stays Connected",
          lambda s: s.uplink is Uplink.DOWN,
          lambda s: _s(s, uplink=Uplink.DOWN)),
        A("inject_dhcp_timeout", fault, eq, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "wifi leaves Connected within 30s",
          lambda s: s.wifi is not Wifi.CONNECTED,
          _drop_wifi, postcondition_timeout_ms=30_000, cleanup_action="api_connect_wifi"),
        A("inject_ap_psk_changed", fault, eq, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "wifi = Disconnected within 10s", lambda s: s.wifi is Wifi.DISCONNECTED,
          _drop_wifi, postcondition_timeout_ms=10_000, cleanup_action="api_connect_wifi"),
        A("inject_ap_ssid_changed", fault, eq, "wifi",
          "wifi ∈ {Connected, Streaming}",
          lambda s: s.wifi in (Wifi.CONNECTED, Wifi.STREAMING),
          "wifi = Disconnected within 10s", lambda s: s.wifi is Wifi.DISCONNECTED,
          _drop_wifi, postcondition_timeout_ms=10_000, cleanup_action="api_connect_wifi"),
    ]

    # ---- bt / dut ---------------------------------------------------------
    out += [
        A("api_bt_pairing", api, dut, "bt",
          "bt_link = Idle", lambda s: s.bt_link is BtLink.IDLE,
          "bt_link = Connected ∧ bt_audio = None within 10s",
          lambda s: s.bt_link is BtLink.CONNECTED,
          _connected_bt, postcondition_timeout_ms=10_000),
        A("api_bt_connect", api, dut, "bt",
          "bt_link = Idle", lambda s: s.bt_link is BtLink.IDLE,
          "bt_link = Connected ∧ bt_audio = None within 5s — already bonded, no pairing handshake",
          lambda s: s.bt_link is BtLink.CONNECTED,
          _connected_bt, postcondition_timeout_ms=5_000),
        A("api_bt_disconnect", api, dut, "bt",
          "bt_link = Connected", lambda s: s.bt_link is BtLink.CONNECTED,
          "bt_link = Idle ∧ bt_audio = None",
          lambda s: s.bt_link is BtLink.IDLE and s.bt_audio is BtAudio.NONE,
          _drop_bt),
        A("api_bt_audio_streaming", api, dut, "bt",
          "bt_link = Connected ∧ bt_audio = None",
          lambda s: s.bt_link is BtLink.CONNECTED and s.bt_audio is BtAudio.NONE,
          "bt_audio = Streaming ∧ stream active within 5s",
          lambda s: s.bt_audio is BtAudio.STREAMING,
          lambda s: _s(s, bt_audio=BtAudio.STREAMING), postcondition_timeout_ms=5_000),
        A("api_inject_wrong_devicename", fault, dut, "bt",
          "bt_link = Idle", lambda s: s.bt_link is BtLink.IDLE,
          "bt_link = Idle ∧ error = wrong_device_name",
          lambda s: s.bt_link is BtLink.IDLE,
          lambda s: s, postcondition_timeout_ms=5_000),
        A("api_inject_no_peer", fault, dut, "bt",
          "bt_link = Idle", lambda s: s.bt_link is BtLink.IDLE,
          "bt_link = Idle ∧ error = no_peer_found",
          lambda s: s.bt_link is BtLink.IDLE,
          lambda s: s, postcondition_timeout_ms=5_000),
    ]

    # ---- bt / equipment ---------------------------------------------------
    out += [
        A("inject_phone_disconnect", fault, eq, "bt",
          "bt_link = Connected", lambda s: s.bt_link is BtLink.CONNECTED,
          "bt_link = Idle within 5s", lambda s: s.bt_link is BtLink.IDLE,
          _drop_bt, postcondition_timeout_ms=5_000, cleanup_action="api_bt_connect"),
        A("inject_phone_out_of_range", fault, eq, "bt",
          "bt_link = Connected", lambda s: s.bt_link is BtLink.CONNECTED,
          "bt_link = Idle within 15s", lambda s: s.bt_link is BtLink.IDLE,
          _drop_bt, postcondition_timeout_ms=15_000,
          cleanup_action="api_bt_connect"),
        A("inject_phone_reboot", fault, eq, "bt",
          "bt_link = Connected", lambda s: s.bt_link is BtLink.CONNECTED,
          "bt_link = Idle within 15s", lambda s: s.bt_link is BtLink.IDLE,
          _drop_bt, postcondition_timeout_ms=15_000,
          cleanup_action="api_bt_connect"),
    ]

    # ---- matter -----------------------------------------------------------
    out += [
        A("api_start_matter", api, dut, "matter",
          "matter = Uncommissioned ∧ wifi = Connected",
          lambda s: s.matter is Matter.UNCOMMISSIONED and s.wifi is Wifi.CONNECTED,
          "matter = Commissioning within 5s", lambda s: s.matter is Matter.COMMISSIONING,
          lambda s: _s(s, matter=Matter.COMMISSIONING), postcondition_timeout_ms=5_000),
        A("api_complete_matter_commissioning", api, dut, "matter",
          "matter = Commissioning", lambda s: s.matter is Matter.COMMISSIONING,
          "matter = Operational within 75s", lambda s: s.matter is Matter.OPERATIONAL,
          lambda s: _s(s, matter=Matter.OPERATIONAL), postcondition_timeout_ms=75_000),
        A("api_remove_matter_fabric", api, dut, "matter",
          "matter = Operational", lambda s: s.matter is Matter.OPERATIONAL,
          "matter = Uncommissioned within 20s",
          lambda s: s.matter is Matter.UNCOMMISSIONED,
          lambda s: _s(s, matter=Matter.UNCOMMISSIONED), postcondition_timeout_ms=20_000),
        A("inject_wrong_setup_code", fault, dut, "matter",
          "matter = Commissioning", lambda s: s.matter is Matter.COMMISSIONING,
          "matter = Uncommissioned ∧ error = invalid_setup_code",
          lambda s: s.matter is Matter.UNCOMMISSIONED,
          lambda s: _s(s, matter=Matter.UNCOMMISSIONED)),
        A("inject_matter_controller_reject", fault, eq, "matter",
          "matter = Commissioning", lambda s: s.matter is Matter.COMMISSIONING,
          "matter = Uncommissioned within 30s ∧ error = controller_refused",
          lambda s: s.matter is Matter.UNCOMMISSIONED,
          lambda s: _s(s, matter=Matter.UNCOMMISSIONED), postcondition_timeout_ms=30_000),
    ]

    # ---- system -----------------------------------------------------------
    out += [
        A("inject_cpu_stress", fault, dut, "system",
          "power = Active ∧ cpuLoad = Normal",
          lambda s: s.power is Power.ACTIVE and s.cpu_load is Stress.NORMAL,
          "cpuLoad = Critical within 10s", lambda s: s.cpu_load is Stress.CRITICAL,
          lambda s: _s(s, cpu_load=Stress.CRITICAL),
          cleanup_action="clear_system_stress"),
        A("inject_memory_stress", fault, dut, "system",
          "power = Active ∧ memPressure = Normal",
          lambda s: s.power is Power.ACTIVE and s.mem_pressure is Stress.NORMAL,
          "memPressure = Critical within 10s",
          lambda s: s.mem_pressure is Stress.CRITICAL,
          lambda s: _s(s, mem_pressure=Stress.CRITICAL),
          cleanup_action="clear_system_stress"),
        A("clear_system_stress", fault, dut, "system",
          "cpuLoad ≠ Normal ∨ memPressure ≠ Normal",
          lambda s: s.cpu_load is not Stress.NORMAL or s.mem_pressure is not Stress.NORMAL,
          "cpuLoad = Normal ∧ memPressure = Normal within 10s",
          lambda s: s.cpu_load is Stress.NORMAL and s.mem_pressure is Stress.NORMAL,
          lambda s: _s(s, cpu_load=Stress.NORMAL, mem_pressure=Stress.NORMAL)),
        # One action, not two: the bench cuts power and restores it as a single
        # atomic fault, so there is nothing left for a separate cleanupAction to do —
        # the postcondition already asserts the full cold-boot recovery.
        A("inject_power_reboot", fault, eq, "system",
          "power = Active", lambda s: s.power is Power.ACTIVE,
          "power = Active ∧ wifi = Disconnected ∧ bt_link = Idle ∧ "
          "matter = Uncommissioned within 50s",
          lambda s: (s.power is Power.ACTIVE and s.wifi is Wifi.DISCONNECTED
                     and s.bt_link is BtLink.IDLE
                     and s.matter is Matter.UNCOMMISSIONED),
          lambda s: CompositeState(), postcondition_timeout_ms=50_000),
    ]
    return out


class ActionDatabase:
    """One database, shared by however many concurrent workers exist."""

    def __init__(self, actions: list[ActionDefinition] | None = None):
        self._actions = actions if actions is not None else _build()
        self._by_name = {a.name: a for a in self._actions}

    def __len__(self) -> int:
        return len(self._actions)

    def __iter__(self):
        return iter(self._actions)

    def get(self, name: str) -> ActionDefinition:
        return self._by_name[name]

    def names(self) -> list[str]:
        return [a.name for a in self._actions]



ACTION_DB = ActionDatabase()
