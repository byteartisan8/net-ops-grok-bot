"""Minimal asyncio SNMPv2c manager on pysnmp's message API: GET, GETNEXT, GETBULK and walks.

Used by the container healthcheck, ``netsim-show`` and the tests. Walks verify that the agent returns
strictly increasing OIDs (a non-increasing answer raises :class:`SnmpError`), which is how the tests prove
lexicographic ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pyasn1.codec.ber import decoder, encoder
from pysnmp.proto import api, rfc1905

from netsim_simd.snmpdb import oid_str, oid_tuple
from netsim_simd.traps import render_value

_P = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
_ids = itertools.count(random.randint(1, 1_000_000))

Kind = Literal["get", "next", "bulk"]
END_TYPES = (rfc1905.EndOfMibView, rfc1905.NoSuchInstance, rfc1905.NoSuchObject)


class SnmpError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VarBind:
    oid: str
    value: Any

    @property
    def end(self) -> bool:
        return isinstance(self.value, END_TYPES)

    @property
    def rendered(self) -> Any:
        if isinstance(self.value, END_TYPES):
            return type(self.value).__name__
        return render_value(self.value)


class _Client(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.waiters: dict[int, asyncio.Future[Any]] = {}

    def datagram_received(self, data: bytes, addr: Any) -> None:
        try:
            msg, _ = decoder.decode(data, asn1Spec=_P.Message())
        except Exception:
            return
        pdu = _P.apiMessage.get_pdu(msg)
        waiter = self.waiters.get(int(_P.apiPDU.get_request_id(pdu)))
        if waiter is not None and not waiter.done():
            waiter.set_result(pdu)


async def request(
    host: str,
    port: int,
    community: str,
    kind: Kind,
    oids: Sequence[str],
    *,
    non_repeaters: int = 0,
    max_repetitions: int = 10,
    timeout: float = 1.0,
    retries: int = 2,
) -> list[VarBind]:
    if kind == "bulk":
        pdu = _P.GetBulkRequestPDU()
        _P.apiBulkPDU.set_defaults(pdu)
        _P.apiBulkPDU.set_non_repeaters(pdu, non_repeaters)
        _P.apiBulkPDU.set_max_repetitions(pdu, max_repetitions)
    else:
        pdu = _P.GetRequestPDU() if kind == "get" else _P.GetNextRequestPDU()
        _P.apiPDU.set_defaults(pdu)
    request_id = next(_ids) % 2_000_000_000 + 1
    _P.apiPDU.set_request_id(pdu, request_id)
    _P.apiPDU.set_varbinds(pdu, [(_P.ObjectIdentifier(oid_str(o)), _P.Null("")) for o in oids])
    msg = _P.Message()
    _P.apiMessage.set_defaults(msg)
    _P.apiMessage.set_community(msg, community)
    _P.apiMessage.set_pdu(msg, pdu)
    data = encoder.encode(msg)

    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_Client, remote_addr=(host, port))
    try:
        for _attempt in range(retries + 1):
            fut: asyncio.Future[Any] = loop.create_future()
            proto.waiters[request_id] = fut
            transport.sendto(data)
            try:
                rsp = await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                continue
            status = int(_P.apiPDU.get_error_status(rsp))
            if status:
                raise SnmpError(
                    f"{kind} {list(oids)}: error-status {status} at index {int(_P.apiPDU.get_error_index(rsp))}"
                )
            return [VarBind(oid_str(tuple(oid)), val) for oid, val in _P.apiPDU.get_varbinds(rsp)]
        raise SnmpError(f"no response from {host}:{port} ({kind} {list(oids)})")
    finally:
        with contextlib.suppress(Exception):
            transport.close()


async def get(host: str, port: int, community: str, *oids: str, **kw: Any) -> list[VarBind]:
    return await request(host, port, community, "get", oids, **kw)


async def get_next(host: str, port: int, community: str, *oids: str, **kw: Any) -> list[VarBind]:
    return await request(host, port, community, "next", oids, **kw)


async def get_bulk(
    host: str, port: int, community: str, *oids: str, non_repeaters: int = 0, max_repetitions: int = 10, **kw: Any
) -> list[VarBind]:
    return await request(
        host, port, community, "bulk", oids, non_repeaters=non_repeaters, max_repetitions=max_repetitions, **kw
    )


async def walk(
    host: str, port: int, community: str, root: str, *, bulk: bool = True, max_repetitions: int = 25, **kw: Any
) -> list[VarBind]:
    """All variables under ``root`` (GETBULK or GETNEXT), checking strictly increasing order."""
    root_t = oid_tuple(root)
    out: list[VarBind] = []
    current = oid_str(root)
    last: tuple[int, ...] | None = None
    while True:
        batch = (
            await get_bulk(host, port, community, current, max_repetitions=max_repetitions, **kw)
            if bulk
            else await get_next(host, port, community, current, **kw)
        )
        if not batch:
            return out
        for vb in batch:
            t = oid_tuple(vb.oid)
            if vb.end or t[: len(root_t)] != root_t:
                return out
            if last is not None and t <= last:
                raise SnmpError(f"agent returned {vb.oid} after {oid_str(last)}: not lexicographically increasing")
            out.append(vb)
            last = t
        current = out[-1].oid
