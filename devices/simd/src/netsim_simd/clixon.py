"""Local Clixon access: RESTCONF (RFC 8040) for config, and the commit notification stream.

The image's backend plugin (``devices/clixon/netsim_backend.c``) emits the standard RFC 6470
``ietf-netconf-notifications:netconf-config-change`` on the ``NETCONF`` stream after every commit that changed
the running datastore (``changed-by`` = the session user, ``edit`` = target + operation per changed node).
simd subscribes to it as a RESTCONF event stream (server-sent events, ``GET /streams/NETCONF``), the same
way any NMS could.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("simd.clixon")

YANG_JSON = "application/yang-data+json"
RESTCONF_DATA = "/restconf/data"
NETCONF_STREAM = "NETCONF"
NCN_NS = "urn:ietf:params:xml:ns:yang:ietf-netconf-notifications"
NOTIF_NS = "urn:ietf:params:xml:ns:netconf:notification:1.0"


class RestconfError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path}: HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class ConfigCommit:
    """One ``netconf-config-change`` notification."""

    event_time: str | None
    username: str | None
    server: bool
    datastore: str
    edits: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_time": self.event_time,
            "changed_by": "server" if self.server else self.username,
            "datastore": self.datastore,
            "edits": [{"target": t, "operation": o} for t, o in self.edits],
        }


def parse_config_change(xml_text: str) -> ConfigCommit | None:
    """Parse an RFC 5277 ``<notification>`` carrying ``netconf-config-change``; None for other notifications."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    change = root.find(f"{{{NCN_NS}}}netconf-config-change")
    if change is None:
        return None
    by = change.find(f"{{{NCN_NS}}}changed-by")
    server = by is not None and by.find(f"{{{NCN_NS}}}server") is not None
    username = by.findtext(f"{{{NCN_NS}}}username") if by is not None else None
    edits = tuple(
        ((e.findtext(f"{{{NCN_NS}}}target") or "").strip(), (e.findtext(f"{{{NCN_NS}}}operation") or "").strip())
        for e in change.findall(f"{{{NCN_NS}}}edit")
    )
    return ConfigCommit(
        event_time=root.findtext(f"{{{NOTIF_NS}}}eventTime"),
        username=username,
        server=server,
        datastore=change.findtext(f"{{{NCN_NS}}}datastore") or "running",
        edits=edits,
    )


class Restconf:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers={"Accept": YANG_JSON})

    async def aclose(self) -> None:
        await self.client.aclose()

    async def ready(self) -> bool:
        """The RESTCONF root resource answers (RFC 8040 section 3.1)."""
        try:
            resp = await self.client.get("/restconf")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def get_running(self) -> dict[str, Any]:
        """Running config as RESTCONF JSON (``{}`` when the datastore is empty)."""
        resp = await self.client.get(RESTCONF_DATA, params={"content": "config"})
        if resp.status_code in (204, 404):
            return {}
        if resp.status_code != 200:
            raise RestconfError("GET", RESTCONF_DATA, resp.status_code, resp.text)
        data = resp.json() if resp.content else {}
        return data.get("ietf-restconf:data", data) if isinstance(data, dict) else {}

    async def replace_running(self, payload: dict[str, Any]) -> None:
        """Replace the whole datastore in one commit (``PUT /restconf/data``, RFC 8040 section 4.5)."""
        body = json.dumps({"ietf-restconf:data": payload})
        resp = await self.client.put(RESTCONF_DATA, content=body, headers={"Content-Type": YANG_JSON})
        if resp.status_code not in (200, 201, 204):
            raise RestconfError("PUT", RESTCONF_DATA, resp.status_code, resp.text)

    async def notifications(
        self, stream: str = NETCONF_STREAM, on_open: Callable[[], Awaitable[None]] | None = None
    ) -> AsyncIterator[str]:
        """Yield every event of a RESTCONF notification stream (SSE) as XML text, until the stream ends.

        ``on_open`` runs once the server accepted the subscription (before the first event).
        """
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        timeout = httpx.Timeout(5.0, read=None)
        async with self.client.stream("GET", f"/streams/{stream}", headers=headers, timeout=timeout) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RestconfError("GET", f"/streams/{stream}", resp.status_code, body)
            if on_open is not None:
                await on_open()
            data: list[str] = []
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
                elif not line.strip() and data:
                    yield "\n".join(data)
                    data = []
            if data:
                yield "\n".join(data)
