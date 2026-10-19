"""snmpsim variation module ``netsim``: serves the MIB view that simd writes to SQLite.

snmpsim executes the module file ``devices/snmpsim/variation/netsim.py`` (which imports this module) and
calls ``init`` / ``variate`` / ``shutdown``. The data file ``<community>.snmprec`` holds a single subtree
line, ``<anchor>|:netsim|``, so snmpsim hands every GET / GETNEXT under the anchor to :func:`variate`;
GETBULK and walks are built by snmpsim/pysnmp from repeated GETNEXTs. When the module returns
``endOfMibView`` snmpsim moves on to the next data-file line (there is none), so a walk ends cleanly.

Options (``--variation-module-options=netsim:db:/run/netsim/snmp/agent.db``): ``db`` = SQLite path.
The agent is read-only: SET requests get the error status snmpsim proposes.
"""

from __future__ import annotations

from typing import Any

from netsim_simd.snmpdb import SnmpDbReader

DEFAULT_DB = "/run/netsim/snmp/agent.db"

_reader: SnmpDbReader | None = None


def _options(raw: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (raw or "").split(","):
        key, sep, value = item.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def init(**context: Any) -> None:
    global _reader
    _reader = SnmpDbReader(_options(context.get("options")).get("db", DEFAULT_DB))


def variate(oid: Any, tag: str, value: str, **context: Any) -> tuple[Any, str, Any]:
    orig = context["origOid"]
    if _reader is None or context.get("setFlag"):
        return orig, tag, context["errorStatus"]
    found = _reader.next(str(orig)) if context.get("nextFlag") else _reader.get(str(orig))
    if found is None:
        return orig, tag, context["errorStatus"]
    return orig.clone(found.oid), found.tag, found.value


def shutdown(**context: Any) -> None:
    global _reader
    if _reader is not None:
        _reader.close()
    _reader = None
