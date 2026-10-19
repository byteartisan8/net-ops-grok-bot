"""Simulated control plane on the ``core`` Redis bus (contract section 7): keys, timers, advert shape.

simd (session 02) writes adverts and reads faults; labctl / lab-api (session 05) write faults and cuts.
Both sides must build keys with these helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from netsim_common.datatypes import Hostname, InterfaceName, IPv4, ServiceId, VcId
from netsim_common.enums import PwRole

#: Advert refresh ("hello") interval and TTL ("hold time"), seconds.
HELLO_INTERVAL_S = 1.0
HOLD_TIME_S = 3.0

KEY_PREFIX = "netsim:"
ADVERT_PREFIX = "netsim:adv:"
LOOPBACK_INDEX_PREFIX = "netsim:lo:"
FAULT_UPLINK_PREFIX = "netsim:fault:uplink:"
FAULT_AC_PREFIX = "netsim:fault:ac:"
CUT_PREFIX = "netsim:cut:"
EVENTS_CHANNEL = "netsim:events"
#: Value of a set fault / cut key.
FAULT_ON = "1"

ADVERT_PATTERN = ADVERT_PREFIX + "*"
FAULT_PATTERN = "netsim:fault:*"
CUT_PATTERN = CUT_PREFIX + "*"


def advert_key(hostname: str) -> str:
    """``netsim:adv:<hostname>``: JSON :class:`Advert`, refreshed every hello, TTL = hold time."""
    return ADVERT_PREFIX + hostname


def loopback_index_key(loopback: str) -> str:
    """Optional index ``netsim:lo:<loopback>`` -> hostname."""
    return LOOPBACK_INDEX_PREFIX + loopback


def uplink_fault_key(hostname: str) -> str:
    """``netsim:fault:uplink:<hostname>`` = 1: core uplink physically down."""
    return FAULT_UPLINK_PREFIX + hostname


def ac_fault_key(hostname: str, ifname: str) -> str:
    """``netsim:fault:ac:<hostname>:<ifname>`` = 1: attachment circuit physically down."""
    return f"{FAULT_AC_PREFIX}{hostname}:{ifname}"


def cut_key(host_a: str, host_b: str) -> str:
    """``netsim:cut:<hostA>|<hostB>`` = 1 (hostnames sorted): core path between the pair broken."""
    a, b = sorted((host_a, host_b))
    return f"{CUT_PREFIX}{a}|{b}"


@dataclass(frozen=True, slots=True)
class FaultKey:
    """A parsed fault or cut key."""

    kind: Literal["uplink", "ac", "cut"]
    hosts: tuple[str, ...]
    ifname: str | None = None

    @property
    def key(self) -> str:
        if self.kind == "uplink":
            return uplink_fault_key(self.hosts[0])
        if self.kind == "ac":
            return ac_fault_key(self.hosts[0], self.ifname or "")
        return cut_key(*self.hosts)


def parse_fault_key(key: str) -> FaultKey | None:
    """Inverse of the key helpers; ``None`` for keys that are not faults or cuts."""
    if key.startswith(FAULT_UPLINK_PREFIX):
        return FaultKey("uplink", (key.removeprefix(FAULT_UPLINK_PREFIX),))
    if key.startswith(FAULT_AC_PREFIX):
        host, sep, ifname = key.removeprefix(FAULT_AC_PREFIX).partition(":")
        return FaultKey("ac", (host,), ifname) if sep and ifname else None
    if key.startswith(CUT_PREFIX):
        a, sep, b = key.removeprefix(CUT_PREFIX).partition("|")
        return FaultKey("cut", (a, b)) if sep and b else None
    return None


class AdvertPw(BaseModel):
    """One PW in an advert."""

    model_config = ConfigDict(extra="allow")

    service_id: ServiceId
    role: PwRole
    peer: IPv4 = Field(description="Configured peer loopback")
    vc_id: VcId
    enabled: bool = Field(description="xconnect enabled and PW enabled")
    ac_oper_up: bool
    mtu: int = Field(description="Effective MTU of the AC")
    ac_name: InterfaceName


class Advert(BaseModel):
    """Simulated targeted-LDP hello, stored at :func:`advert_key` (contract section 7).

    Extra fields are allowed so simd can add diagnostics without breaking readers.
    """

    model_config = ConfigDict(extra="allow")

    hostname: Hostname
    loopback: IPv4
    mgmt_ip: IPv4
    boot_id: str = Field(min_length=1, description="Changes on every simd start")
    seq: int = Field(ge=0, description="Monotonic per boot_id")
    uplink_oper_up: bool
    pws: list[AdvertPw] = Field(default_factory=list)
