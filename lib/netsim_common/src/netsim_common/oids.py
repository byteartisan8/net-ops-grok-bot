"""Typed access to ``contract/oids.yaml`` (generated from the pinned MIBs by ``tools/gen_oids.py``).

Example::

    oids = load_oids()
    oids.oid("cpwVcOperStatus")                 # '1.3.6.1.4.1.9.10.106.1.2.1.26'
    oids.instance("cpwVcOperStatus", 3)         # '1.3.6.1.4.1.9.10.106.1.2.1.26.3'
    oids.value("cpwVcOperStatus", "up")         # 1
    oids.notification("cpwVcDown").objects      # varbinds in PDU order (after sysUpTime.0, snmpTrapOID.0)
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from netsim_common.paths import contract_dir

OIDS_FILE = "oids.yaml"

#: SMI base type (as written by pysmi) -> class name in ``pysnmp.proto.rfc1902``.
PYSNMP_TYPE_BY_BASE: dict[str, str] = {
    "INTEGER": "Integer32",
    "Integer32": "Integer32",
    "Unsigned32": "Unsigned32",
    "Gauge32": "Gauge32",
    "Counter32": "Counter32",
    "Counter64": "Counter64",
    "TimeTicks": "TimeTicks",
    "OCTET STRING": "OctetString",
    "OBJECT IDENTIFIER": "ObjectIdentifier",
    "IpAddress": "IpAddress",
    "Bits": "Bits",
    "Opaque": "Opaque",
}

ObjectKind = Literal["node", "scalar", "table", "row", "column"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Syntax(_Model):
    type: str = Field(description="Type as written in the MIB (textual convention or base type)")
    base: str = Field(description="SMI base type after resolving textual conventions")
    tc_module: str | None = Field(default=None, description="Module defining the textual convention")
    display_hint: str | None = None
    enum: dict[str, int] | None = Field(default=None, description="label -> value, ordered by value")
    range: str | None = Field(default=None, description="Value range(s), MIB notation, e.g. '1..2147483647'")
    size: str | None = Field(default=None, description="Size range(s), MIB notation, e.g. '0..255'")

    @property
    def pysnmp_type(self) -> str:
        return PYSNMP_TYPE_BY_BASE[self.base]


class MibObject(_Model):
    name: str
    module: str
    oid: str
    kind: ObjectKind
    access: str | None = None
    syntax: Syntax | None = None
    instance: str | None = Field(default=None, description="Instance OID of a scalar (oid + '.0')")
    table: str | None = Field(default=None, description="Table of a row or column")
    row: str | None = Field(default=None, description="Row (entry) of a column")
    index: list[str] | None = Field(default=None, description="INDEX objects of a row (inherited by AUGMENTS)")
    augments: str | None = None


class Varbind(_Model):
    name: str
    module: str
    oid: str
    index: list[str] = Field(description="Index objects that form the instance suffix ([] = scalar, suffix .0)")

    def instance(self, *index: int) -> str:
        if not self.index:
            return f"{self.oid}.0"
        if len(index) != len(self.index):
            raise ValueError(f"{self.name} needs {len(self.index)} index value(s): {self.index}")
        return ".".join([self.oid, *(str(i) for i in index)])


class Notification(_Model):
    name: str
    module: str
    oid: str
    objects: list[Varbind] = Field(description="OBJECTS clause in order; varbinds follow sysUpTime.0 and snmpTrapOID.0")
    description: str


class MibModule(_Model):
    file: str
    sha256: str
    identity: str | None = None
    oid: str | None = None
    revision: str | None = None


class OidRegistry(_Model):
    """The parsed ``contract/oids.yaml`` (entries are keyed by name; ``name`` is filled in from the key)."""

    generator: dict[str, str]
    mibs: dict[str, MibModule]
    objects: dict[str, MibObject]
    notifications: dict[str, Notification]

    @model_validator(mode="before")
    @classmethod
    def _names_from_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for section in ("objects", "notifications"):
                if isinstance(data.get(section), dict):
                    data[section] = {
                        k: {"name": k, **v} if isinstance(v, dict) else v for k, v in data[section].items()
                    }
        return data

    def object(self, name: str) -> MibObject:
        try:
            return self.objects[name]
        except KeyError:
            raise KeyError(f"{name!r} is not in contract/oids.yaml") from None

    def notification(self, name: str) -> Notification:
        try:
            return self.notifications[name]
        except KeyError:
            raise KeyError(f"notification {name!r} is not in contract/oids.yaml") from None

    def oid(self, name: str) -> str:
        """Numeric OID of an object or notification."""
        if name in self.notifications:
            return self.notifications[name].oid
        return self.object(name).oid

    def instance(self, name: str, *index: int) -> str:
        """Instance OID: ``oid.0`` for scalars, ``oid.<index...>`` for columns."""
        obj = self.object(name)
        if obj.kind == "scalar":
            if index:
                raise ValueError(f"{name} is a scalar")
            return f"{obj.oid}.0"
        if obj.kind != "column" or obj.row is None:
            raise ValueError(f"{name} is a {obj.kind}, not a scalar or column")
        row_index = self.object(obj.row).index or []
        if len(index) != len(row_index):
            raise ValueError(f"{name} needs {len(row_index)} index value(s): {row_index}")
        return ".".join([obj.oid, *(str(i) for i in index)])

    def enum(self, name: str) -> dict[str, int]:
        obj = self.object(name)
        if obj.syntax is None or obj.syntax.enum is None:
            raise ValueError(f"{name} is not an enumeration")
        return dict(obj.syntax.enum)

    def value(self, name: str, label: str) -> int:
        return self.enum(name)[label]

    def label(self, name: str, value: int) -> str:
        for label, v in self.enum(name).items():
            if v == value:
                return label
        raise ValueError(f"{value} is not a valid {name} value")

    def columns(self, table: str) -> list[MibObject]:
        """Columns of ``table`` listed in oids.yaml, in OID order."""
        cols = [o for o in self.objects.values() if o.kind == "column" and o.table == table]
        return sorted(cols, key=lambda o: tuple(int(x) for x in o.oid.split(".")))

    def name_of(self, oid: str) -> str | None:
        """Reverse lookup: the object/notification whose OID equals ``oid`` or is its longest prefix."""
        oid = oid.lstrip(".")
        best: tuple[int, str] | None = None
        for name, node in [*self.objects.items(), *self.notifications.items()]:
            if oid == node.oid or oid.startswith(node.oid + "."):
                length = node.oid.count(".")
                if best is None or length > best[0]:
                    best = (length, name)
        return best[1] if best else None


@functools.cache
def _load(path: str, mtime_ns: int) -> OidRegistry:
    with open(path, encoding="utf-8") as fh:
        return OidRegistry.model_validate(yaml.safe_load(fh))


def load_oids(path: str | Path | None = None) -> OidRegistry:
    """Load ``contract/oids.yaml`` (or ``path``). Results are cached per file version."""
    file = Path(path) if path is not None else contract_dir() / OIDS_FILE
    file = file.resolve()
    return _load(str(file), file.stat().st_mtime_ns)
