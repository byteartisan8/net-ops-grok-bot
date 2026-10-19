"""Constrained scalar types shared by the models.

Constraints mirror ``contract/yang/netsim-device.yang`` (patterns, lengths, ranges). Integers and
booleans are strict, as in RFC 7951 JSON: ``"vc-id": "1001"`` or ``"enabled": "true"`` are rejected.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, Strict, StringConstraints

_OCTET = r"([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])"
IPV4_PATTERN = rf"^({_OCTET}\.){{3}}{_OCTET}$"
HOSTNAME_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$"
SERVICE_ID_PATTERN = r"^SVC-[0-9]{4}$"

IPv4 = Annotated[
    str,
    StringConstraints(pattern=IPV4_PATTERN),
    Field(description="Dotted-quad IPv4 address (YANG inet:ipv4-address-no-zone)", examples=["10.255.0.1"]),
]
Hostname = Annotated[
    str,
    StringConstraints(min_length=1, max_length=63, pattern=HOSTNAME_PATTERN),
    Field(description="Hostname (a DNS label)", examples=["spoke-1"]),
]
ServiceId = Annotated[
    str,
    StringConstraints(pattern=SERVICE_ID_PATTERN),
    Field(description="Service identifier SVC-NNNN", examples=["SVC-0001"]),
]
InterfaceName = Annotated[
    str,
    # 80 = SIZE of cpwVcRemoteIfString, which carries the peer's AC name.
    StringConstraints(min_length=1, max_length=80),
    Field(description="Interface name", examples=["GigabitEthernet0/0/1.101"]),
]
InterfaceDescription = Annotated[
    str,
    # 64 = SIZE of ifAlias, which carries the description.
    StringConstraints(max_length=64),
    Field(description="Interface description (also SNMP ifAlias)"),
]
CustomerName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255),
    Field(description="Customer (NetBox tenant) name; also SNMP cpwVcDescr", examples=["Customer-A"]),
]
Text255 = Annotated[str, StringConstraints(max_length=255)]

VlanId = Annotated[int, Strict(), Field(ge=1, le=4094, description="802.1Q VLAN ID")]
Mtu = Annotated[int, Strict(), Field(ge=64, le=65535, description="MTU in bytes")]
VcId = Annotated[int, Strict(), Field(ge=1, le=4294967295, description="Pseudowire VC ID")]
Port = Annotated[int, Strict(), Field(ge=1, le=65535)]
