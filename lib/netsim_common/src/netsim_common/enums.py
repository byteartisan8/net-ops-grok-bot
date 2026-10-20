"""Enumerations shared by every component (contract sections 4, 6, 7, 8, 10).

SNMP enumerations mirror the pinned MIBs; ``tests/test_oids.py`` checks them against
``contract/oids.yaml`` so they can never silently diverge from the MIB files.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class DeviceRole(StrEnum):
    """PE role (contract section 4); also the NetBox device role slug."""

    HUB = "hub"
    SPOKE = "spoke"


class PwRole(StrEnum):
    """Pseudowire role (YANG ``nsd:pw-role``).

    Definition order is the YANG enum order and the canonical list order (primary first).
    """

    PRIMARY = "primary"
    BACKUP = "backup"

    @property
    def sort_index(self) -> int:
        return _PW_ROLE_ORDER[self]


_PW_ROLE_ORDER = {role: index for index, role in enumerate(PwRole)}


class InterfaceType(StrEnum):
    """Interface type (YANG ``nsd:interface-type``)."""

    CORE_UPLINK = "core-uplink"
    ACCESS = "access"
    SUBINTERFACE = "subinterface"


class SlaTier(StrEnum):
    """NetBox tenant custom field ``sla_tier`` (contract section 10)."""

    GOLD = "gold"
    SILVER = "silver"
    BRONZE = "bronze"


class PwOperStatus(IntEnum):
    """``cpwVcOperStatus`` / ``CpwOperStatus`` (CISCO-IETF-PW-TC-MIB). Member names are the MIB labels."""

    up = 1
    down = 2
    testing = 3
    unknown = 4
    dormant = 5
    notPresent = 6
    lowerLayerDown = 7


class IfOperStatus(IntEnum):
    """``ifOperStatus`` (IF-MIB). Member names are the MIB labels."""

    up = 1
    down = 2
    testing = 3
    unknown = 4
    dormant = 5
    notPresent = 6
    lowerLayerDown = 7


class AdminStatus(IntEnum):
    """``ifAdminStatus`` (IF-MIB) and ``cpwVcAdminStatus`` (CISCO-IETF-PW-MIB) share these values."""

    up = 1
    down = 2
    testing = 3


class PwReason(StrEnum):
    """Reason behind a pseudowire's oper status (contract section 7).

    Definition order is the evaluation order of the contract's table: the first matching rule wins.
    """

    ADMIN_DOWN = "admin-down"
    DUPLICATE_PW = "duplicate-pw"
    AC_DOWN = "ac-down"
    LOCAL_UPLINK_DOWN = "local-uplink-down"
    PEER_UNREACHABLE = "peer-unreachable"
    REMOTE_NOT_CONFIGURED = "remote-not-configured"
    REMOTE_ADMIN_DOWN = "remote-admin-down"
    REMOTE_AC_DOWN = "remote-ac-down"
    MTU_MISMATCH = "mtu-mismatch"
    OK = "ok"

    @property
    def rule(self) -> int:
        """1-based row number in the contract's evaluation table."""
        return PW_EVALUATION_ORDER.index(self) + 1

    @property
    def oper_status(self) -> PwOperStatus:
        """The ``cpwVcOperStatus`` this reason maps to."""
        return _REASON_OPER_STATUS[self]

    @property
    def condition(self) -> str:
        """The contract's wording of the rule's condition."""
        return _REASON_CONDITION[self]


PW_EVALUATION_ORDER: tuple[PwReason, ...] = tuple(PwReason)

_REASON_OPER_STATUS: dict[PwReason, PwOperStatus] = {
    PwReason.ADMIN_DOWN: PwOperStatus.down,
    PwReason.DUPLICATE_PW: PwOperStatus.down,
    PwReason.AC_DOWN: PwOperStatus.lowerLayerDown,
    PwReason.LOCAL_UPLINK_DOWN: PwOperStatus.lowerLayerDown,
    PwReason.PEER_UNREACHABLE: PwOperStatus.down,
    PwReason.REMOTE_NOT_CONFIGURED: PwOperStatus.down,
    PwReason.REMOTE_ADMIN_DOWN: PwOperStatus.down,
    PwReason.REMOTE_AC_DOWN: PwOperStatus.down,
    PwReason.MTU_MISMATCH: PwOperStatus.down,
    PwReason.OK: PwOperStatus.up,
}

_REASON_CONDITION: dict[PwReason, str] = {
    PwReason.ADMIN_DOWN: "xconnect or PW enabled=false",
    PwReason.DUPLICATE_PW: "another local PW has same (peer, vc-id)",
    PwReason.AC_DOWN: "local AC disabled or AC fault",
    PwReason.LOCAL_UPLINK_DOWN: "local uplink down",
    PwReason.PEER_UNREACHABLE: "no fresh advert with loopback P, or unreachable",
    PwReason.REMOTE_NOT_CONFIGURED: "peer has no PW with peer=A.loopback and vc-id=N",
    PwReason.REMOTE_ADMIN_DOWN: "remote PW admin down",
    PwReason.REMOTE_AC_DOWN: "remote AC down",
    PwReason.MTU_MISMATCH: "effective MTU differs",
    PwReason.OK: "otherwise",
}
