"""Unit-test fixtures: an in-memory stand-in for the Zabbix service, SLA, map and macro API."""

from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest


class FakeZabbix:
    """Implements just what TreeSync, sync_slas, sync_macros and sync_map call, with Zabbix's semantics."""

    def __init__(self) -> None:
        self.ids = itertools.count(100)
        self.services: dict[str, dict[str, Any]] = {}
        self.slas: dict[str, dict[str, Any]] = {}
        self.macros: dict[str, dict[str, Any]] = {}
        self.maps: dict[str, dict[str, Any]] = {}
        self.triggers: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def _id(self) -> str:
        return str(next(self.ids))

    # ------------------------------------------------------------------ JSON-RPC
    def call(self, method: str, params: Any = None) -> Any:
        self.calls.append(method)
        handler = getattr(self, "_" + method.replace(".", "_"))
        return handler(copy.deepcopy(params))

    def get_one(self, method: str, params: Any) -> dict | None:
        result = self.call(method, params)
        assert len(result) <= 1
        return result[0] if result else None

    def writes(self) -> list[str]:
        return [c for c in self.calls if not c.endswith(".get")]

    # ------------------------------------------------------------------ services
    def _service_get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for sid, svc in self.services.items():
            tags = {(t["tag"], t["value"]) for t in svc["tags"]}
            if any((f["tag"], f["value"]) not in tags for f in params.get("tags", [])):
                continue
            parents = [{"serviceid": p, "name": self.services[p]["name"]} for p in svc["parents"]]
            out.append({**svc, "serviceid": sid, "parents": parents, "status": "-1"})
        return out

    def _store_service(self, sid: str, body: dict[str, Any]) -> None:
        cur = self.services.get(sid, {"parents": [], "tags": [], "problem_tags": [], "status_rules": []})
        for k, v in body.items():
            if k == "parents":
                cur["parents"] = [p["serviceid"] for p in v]
            elif k in ("tags", "problem_tags", "status_rules"):
                cur[k] = [{kk: str(vv) for kk, vv in row.items()} for row in v]
            elif k != "serviceid":
                cur[k] = str(v)
        self.services[sid] = cur

    def _service_create(self, bodies: list[dict[str, Any]]) -> dict[str, list[str]]:
        ids = []
        for body in bodies:
            for p in body.get("parents", []):
                assert p["serviceid"] in self.services, "parent must exist before the child"
            sid = self._id()
            self._store_service(sid, body)
            ids.append(sid)
        return {"serviceids": ids}

    def _service_update(self, body: dict[str, Any]) -> dict[str, list[str]]:
        self._store_service(body["serviceid"], body)
        return {"serviceids": [body["serviceid"]]}

    def _service_delete(self, ids: list[str]) -> dict[str, list[str]]:
        for sid in ids:
            del self.services[sid]
        for svc in self.services.values():
            svc["parents"] = [p for p in svc["parents"] if p not in ids]
        return {"serviceids": ids}

    # ------------------------------------------------------------------ SLA
    def _sla_get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{**s, "slaid": i} for i, s in self.slas.items()]

    def _sla_create(self, body: dict[str, Any]) -> dict[str, list[str]]:
        i = self._id()
        self.slas[i] = _stringify(body)
        return {"slaids": [i]}

    def _sla_update(self, body: dict[str, Any]) -> dict[str, list[str]]:
        self.slas[body["slaid"]].update(_stringify({k: v for k, v in body.items() if k != "slaid"}))
        return {"slaids": [body["slaid"]]}

    def _sla_delete(self, ids: list[str]) -> dict[str, list[str]]:
        for i in ids:
            del self.slas[i]
        return {"slaids": ids}

    # ------------------------------------------------------------------ host macros
    def _usermacro_get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        wanted = set(params.get("filter", {}).get("macro", []))
        return [
            {**m, "hostmacroid": i}
            for i, m in self.macros.items()
            if m["hostid"] in params["hostids"] and (not wanted or m["macro"] in wanted)
        ]

    def _usermacro_create(self, bodies: list[dict[str, Any]]) -> dict[str, list[str]]:
        ids = []
        for b in bodies:
            i = self._id()
            self.macros[i] = b
            ids.append(i)
        return {"hostmacroids": ids}

    def _usermacro_update(self, bodies: list[dict[str, Any]]) -> dict[str, list[str]]:
        for b in bodies:
            self.macros[b["hostmacroid"]]["value"] = b["value"]
        return {"hostmacroids": [b["hostmacroid"] for b in bodies]}

    def _usermacro_delete(self, ids: list[str]) -> dict[str, list[str]]:
        for i in ids:
            del self.macros[i]
        return {"hostmacroids": ids}

    # ------------------------------------------------------------------ map
    def _image_get(self, params: dict[str, Any]) -> list[dict[str, str]]:
        return [{"imageid": str(10 + n), "name": name} for n, name in enumerate(params["filter"]["name"])]

    def _trigger_get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return copy.deepcopy(self.triggers)

    def _map_get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [copy.deepcopy(m) for m in self.maps.values()]

    def _map_create(self, body: dict[str, Any]) -> dict[str, list[str]]:
        i = self._id()
        self.maps[i] = _stored_map(i, body)
        return {"sysmapids": [i]}

    def _map_update(self, body: dict[str, Any]) -> dict[str, list[str]]:
        self.maps[body["sysmapid"]] = _stored_map(body["sysmapid"], body)
        return {"sysmapids": [body["sysmapid"]]}


def _stringify(body: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if isinstance(v, list) else str(v)) for k, v in body.items()}


def _stored_map(sysmapid: str, body: dict[str, Any]) -> dict[str, Any]:
    """map.get shape: real selementids, string numbers, links pointing at the stored ids."""
    renum = {s["selementid"]: str(900 + n) for n, s in enumerate(body["selements"])}
    selements = [
        {**s, "selementid": renum[s["selementid"]], "x": str(s["x"]), "y": str(s["y"])} for s in body["selements"]
    ]
    links = [
        {
            **ln,
            "selementid1": renum[ln["selementid1"]],
            "selementid2": renum[ln["selementid2"]],
            "drawtype": str(ln["drawtype"]),
            "indicator_type": str(ln["indicator_type"]),
            "linktriggers": [{**t, "drawtype": str(t["drawtype"])} for t in ln.get("linktriggers", [])],
        }
        for ln in body["links"]
    ]
    return {
        "sysmapid": sysmapid,
        "name": body["name"],
        "width": str(body["width"]),
        "height": str(body["height"]),
        "expand_macros": str(body["expand_macros"]),
        "selements": selements,
        "links": links,
    }


@pytest.fixture
def fake_zabbix() -> FakeZabbix:
    return FakeZabbix()
