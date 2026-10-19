"""Map ``L2VPN Topology``: hubs on top, spokes below, one link per spoke<->hub pair that carries PWs in the spec.

A link's status indicators are the PW triggers of that pair (``scope=l2vpn-pw`` on both ends, matched by the
``service`` / ``pw_role`` tags of the pair's PW leaves): High (``missing``, ``oper``) paints it red and bold, Average
(``spec``) orange and dashed. Built from the spec *after* discovery has created the triggers; links whose triggers do
not exist yet are drawn without indicators and reported, so the next sync completes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from netsim_common.bsm import BsmSpec
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi
from netsim_zabbix.plan import Plan

WIDTH, HEIGHT = 1200, 640
HUB_Y, SPOKE_Y = 110, 450
ICONS = {"hub": "Router_symbol_(64)", "spoke": "Router_symbol_(48)"}
LINK_OK = "2E7D32"
TRIGGER_STYLE = {4: ("D32F2F", 2), 3: ("F57C00", 4)}  # severity -> (color, drawtype: 2 bold, 4 dashed)
ELEMENT_HOST, INDICATOR_STATIC, INDICATOR_TRIGGER = 0, 0, 1


@dataclass(frozen=True)
class Pair:
    spoke: str
    hub: str
    pws: tuple[tuple[str, str, int], ...]  # (service, role, vc_id)

    @property
    def label(self) -> str:
        return "\n".join(f"{s} {r} ({vc})" for s, r, vc in self.pws)


def pairs(spec: BsmSpec) -> list[Pair]:
    by_pair: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    for svc, pw in spec.pseudowires():
        by_pair.setdefault((pw.a_end.host, pw.z_end.host), []).append((svc.service_id, pw.role.value, pw.vc_id))
    return [Pair(s, h, tuple(sorted(p))) for (s, h), p in sorted(by_pair.items())]


def layout(hubs: list[str], spokes: list[str]) -> dict[str, tuple[int, int]]:
    pos: dict[str, tuple[int, int]] = {}
    for row, y in ((hubs, HUB_Y), (spokes, SPOKE_Y)):
        step = WIDTH // (len(row) + 1) if row else 0
        for i, name in enumerate(sorted(row)):
            pos[name] = (step * (i + 1) - 32, y)
    return pos


def desired_map(
    spec: BsmSpec, hostids: dict[str, str], roles: dict[str, str], icons: dict[str, str], triggers: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """The map body (temporary selementids) and the pairs still waiting for discovered triggers."""
    hosts = sorted(h for h in spec.hosts() if h in hostids)
    hubs = [h for h in hosts if roles.get(h) == "hub"]
    pos = layout(hubs, [h for h in hosts if h not in hubs])
    selements = []
    ref: dict[str, str] = {}
    for i, host in enumerate(hosts, start=1):
        ref[host] = str(i)
        x, y = pos[host]
        selements.append(
            {
                "selementid": str(i),
                "elementtype": ELEMENT_HOST,
                "elements": [{"hostid": hostids[host]}],
                "iconid_off": icons["hub" if host in hubs else "spoke"],
                "label": "{HOST.NAME}\n{HOST.CONN}",
                "x": x,
                "y": y,
            }
        )
    pending: list[str] = []
    links = []
    for pair in pairs(spec):
        if pair.spoke not in ref or pair.hub not in ref:
            continue
        wanted = {(s, r) for s, r, _ in pair.pws}
        linktriggers = []
        found: set[tuple[str, str, str]] = set()
        for trig in triggers:
            # trigger tags keep "{HOST.HOST}" unresolved (it resolves in events), so take the host from the trigger
            tags = {t["tag"]: t["value"] for t in trig["tags"]}
            host = trig["hosts"][0]["host"] if trig.get("hosts") else None
            if host in (pair.spoke, pair.hub) and (tags.get("service"), tags.get("pw_role")) in wanted:
                color, drawtype = TRIGGER_STYLE.get(int(trig["priority"]), ("D32F2F", 2))
                linktriggers.append({"triggerid": trig["triggerid"], "color": color, "drawtype": drawtype})
                found.add((host, tags["service"], tags["pw_role"]))
        expected = {(h, s, r) for h in (pair.spoke, pair.hub) for s, r in wanted}
        if not expected <= found:
            pending.append(f"{pair.spoke}<->{pair.hub}")
        link = {
            "selementid1": ref[pair.spoke],
            "selementid2": ref[pair.hub],
            "color": LINK_OK,
            "drawtype": 0,
            "label": pair.label,
            "indicator_type": INDICATOR_TRIGGER if linktriggers else INDICATOR_STATIC,
        }
        if linktriggers:
            link["linktriggers"] = sorted(linktriggers, key=lambda t: int(t["triggerid"]))
        links.append(link)
    body = {
        "name": N.MAP,
        "width": WIDTH,
        "height": HEIGHT,
        "expand_macros": 1,
        "label_type": 0,
        "selements": selements,
        "links": links,
    }
    return body, pending


def _normalize(body: dict[str, Any]) -> Any:
    """Comparable form of a map (map.get output or a desired body): elements by host, links by host pair."""
    host_of = {s["selementid"]: s["elements"][0]["hostid"] for s in body.get("selements", [])}
    elements = sorted(
        (
            s["elements"][0]["hostid"],
            int(s["x"]),
            int(s["y"]),
            str(s["iconid_off"]),
            s["label"].replace("\r\n", "\n"),
        )
        for s in body.get("selements", [])
    )
    links = sorted(
        (
            tuple(sorted((host_of[ln["selementid1"]], host_of[ln["selementid2"]]))),
            ln["color"].upper(),
            int(ln["drawtype"]),
            ln["label"].replace("\r\n", "\n"),
            int(ln.get("indicator_type", 0)),
            tuple(
                sorted(
                    (str(t["triggerid"]), t["color"].upper(), int(t["drawtype"])) for t in ln.get("linktriggers", [])
                )
            ),
        )
        for ln in body.get("links", [])
    )
    return (int(body["width"]), int(body["height"]), int(body["expand_macros"]), elements, links)


def sync_map(
    api: ZabbixApi, spec: BsmSpec, hostids: dict[str, str], roles: dict[str, str], *, dry_run: bool = False
) -> Plan:
    plan = Plan()
    images = api.call("image.get", {"output": ["imageid", "name"], "filter": {"name": list(ICONS.values())}})
    by_name = {i["name"]: i["imageid"] for i in images}
    icons = {role: by_name[name] for role, name in ICONS.items()}
    triggers = (
        api.call(
            "trigger.get",
            {
                "output": ["triggerid", "priority"],
                "selectHosts": ["host"],
                "hostids": list(hostids.values()),
                "selectTags": "extend",
                "tags": [{"tag": "scope", "value": N.SCOPE_PW_TAG, "operator": 1}],
                "templated": False,
            },
        )
        if hostids
        else []
    )
    body, pending = desired_map(spec, hostids, roles, icons, triggers)
    for p in pending:
        plan.warn(f"map link {p}: PW triggers not discovered yet (re-run zabbix-sync after discovery)")
    current = api.get_one(
        "map.get",
        {"output": "extend", "selectSelements": "extend", "selectLinks": "extend", "filter": {"name": [N.MAP]}},
    )
    if current is None:
        plan.add("create", "map", N.MAP, f"{len(body['selements'])} hosts, {len(body['links'])} links")
        if not dry_run:
            api.call("map.create", body)
    elif _normalize(current) != _normalize(body):
        plan.add("update", "map", N.MAP, f"{len(body['selements'])} hosts, {len(body['links'])} links")
        if not dry_run:
            api.call("map.update", {"sysmapid": current["sysmapid"], **body})
    return plan
