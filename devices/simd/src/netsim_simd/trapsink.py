"""``netsim-trapsink``: a small SNMP trap receiver for tests and debugging (not part of the lab topology).

Listens for SNMPv1/v2c traps on UDP, decodes them with contract/oids.yaml names, prints one JSON line per
trap and serves them over HTTP:

* ``GET /traps?since=<epoch>`` -> ``{"now": ..., "traps": [...]}`` (oldest first)
* ``DELETE /traps`` -> clear
* ``GET /health``

    netsim-trapsink --listen 0.0.0.0:1162 --http 0.0.0.0:9162

The integration tests run it in a container on the test network as the devices' ``TRAP_TARGETS``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque
from typing import Any

import uvicorn
from fastapi import FastAPI, Query

from netsim_common.oids import OidRegistry, load_oids
from netsim_simd.jsonlog import iso_ts
from netsim_simd.traps import decode_trap, render_value


def _name(oids: OidRegistry, oid: str) -> tuple[str | None, list[int]]:
    """``(object name, instance index)`` for a varbind OID."""
    name = oids.name_of(oid)
    if name is None:
        return None, []
    base = oids.oid(name)
    rest = oid[len(base) :].strip(".")
    return name, [int(x) for x in rest.split(".") if x]


class TrapStore(asyncio.DatagramProtocol):
    def __init__(self, oids: OidRegistry, maxlen: int = 5000, echo: bool = True) -> None:
        self.oids = oids
        self.traps: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.echo = echo
        self.trap_oid_instance = oids.instance("snmpTrapOID")
        self.uptime_instance = oids.instance("sysUpTime")

    def datagram_received(self, data: bytes, addr: Any) -> None:
        now = time.time()
        try:
            decoded = decode_trap(data)
        except Exception as exc:
            entry = {"received_at": now, "time": iso_ts(now), "source": f"{addr[0]}:{addr[1]}", "error": str(exc)}
        else:
            trap_oid = decoded.value(self.trap_oid_instance)
            varbinds = []
            for oid, value in decoded.varbinds:
                name, index = _name(self.oids, oid)
                varbinds.append({"oid": oid, "name": name, "index": index, "value": render_value(value)})
            entry = {
                "received_at": now,
                "time": iso_ts(now),
                "source": f"{addr[0]}:{addr[1]}",
                "version": decoded.version,
                "community": decoded.community,
                "trap_oid": trap_oid,
                "trap": self.oids.name_of(trap_oid) if isinstance(trap_oid, str) else None,
                "uptime": decoded.value(self.uptime_instance),
                "varbinds": varbinds,
            }
        self.traps.append(entry)
        if self.echo:
            print(json.dumps(entry, default=str), flush=True)

    def since(self, ts: float | None) -> list[dict[str, Any]]:
        return [t for t in self.traps if ts is None or t["received_at"] > ts]


def create_app(store: TrapStore) -> FastAPI:
    app = FastAPI(title="netsim trap sink", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "traps": len(store.traps)}

    @app.get("/traps")
    def traps(since: float | None = Query(default=None, description="Epoch seconds")) -> dict[str, Any]:
        return {"now": time.time(), "traps": store.since(since)}

    @app.delete("/traps")
    def clear() -> dict[str, Any]:
        n = len(store.traps)
        store.traps.clear()
        return {"cleared": n}

    return app


def _hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "0.0.0.0", int(port)


async def serve(listen: tuple[str, int], http: tuple[str, int], quiet: bool) -> None:
    store = TrapStore(load_oids(), echo=not quiet)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: store, local_addr=listen)
    server = uvicorn.Server(uvicorn.Config(create_app(store), host=http[0], port=http[1], log_level="warning"))
    try:
        await server.serve()
    finally:
        transport.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", default="0.0.0.0:1162", help="UDP host:port for traps")
    parser.add_argument("--http", default="0.0.0.0:9162", help="HTTP host:port for the API")
    parser.add_argument("--quiet", action="store_true", help="do not print traps")
    args = parser.parse_args(argv)
    asyncio.run(serve(_hostport(args.listen), _hostport(args.http), args.quiet))
    return 0


if __name__ == "__main__":
    sys.exit(main())
