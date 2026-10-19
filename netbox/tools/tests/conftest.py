"""Fixtures for the NetBox tools unit tests (owner: 03-netbox). No running NetBox needed.

``FakeNetBox`` replays ``data/netbox-seeded.json``: every object of a freshly seeded NetBox 4.7.3, recorded with
``record_fixture.py`` exactly as the REST API returns them. It implements the :class:`NetBoxClient` methods the tools
call, the list filters they use (including NetBox's HTTP 400 for unknown tag/platform slugs), and records writes
without applying them.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from typing import Any

import pytest
from netsim_netbox.client import NetBoxError
from netsim_netbox.intent import LabIntent, build_intent, load_snapshot

from netsim_common.topology import Topology, load_topology

DATA = Path(__file__).parent / "data"
FIXTURE = DATA / "netbox-seeded.json"
PUBLIC_URL = "http://netbox.test"
TEST_WEBHOOK_SECRET = "test-webhook-secret"


@functools.cache
def _recorded() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _device_of(obj: dict[str, Any]) -> dict[str, Any] | None:
    if "device" in obj and isinstance(obj["device"], dict):
        return obj["device"]
    target = obj.get("assigned_object")
    return target.get("device") if isinstance(target, dict) else None


class FakeNetBox:
    """In-memory stand-in for :class:`netsim_netbox.client.NetBoxClient` (reads replay the fixture)."""

    url = "http://fake-netbox"

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        recorded = data or _recorded()
        self.version = recorded["netbox_version"]
        self.data: dict[str, list[dict[str, Any]]] = copy.deepcopy(recorded["endpoints"])
        self.writes: list[tuple[str, str, Any]] = []
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 10_000

    # ------------------------------------------------------------------ test helpers
    def objects(self, endpoint: str) -> list[dict[str, Any]]:
        return self.data.setdefault(endpoint.strip("/"), [])

    def find(self, endpoint: str, **match: Any) -> dict[str, Any]:
        """The one object whose (possibly dotted) fields equal ``match``; mutate it to simulate NetBox edits."""
        hits = [o for o in self.objects(endpoint) if all(_get(o, k) == v for k, v in match.items())]
        assert len(hits) == 1, f"{endpoint} {match}: {len(hits)} matches"
        return hits[0]

    def remove(self, endpoint: str, **match: Any) -> dict[str, Any]:
        obj = self.find(endpoint, **match)
        self.objects(endpoint).remove(obj)
        return obj

    # ------------------------------------------------------------------ client API
    def __enter__(self) -> FakeNetBox:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def close(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"netbox-version": self.version}

    def get(self, endpoint: str, object_id: int | None = None, **params: Any) -> Any:
        if object_id is None:
            raise NotImplementedError(f"fake GET {endpoint}")
        return copy.deepcopy(self.find(endpoint, id=object_id))

    def list(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        endpoint = endpoint.strip("/")
        self.reads.append((endpoint, params))
        out = []
        for obj in self.objects(endpoint):
            if all(self._match(endpoint, obj, k, v) for k, v in params.items()):
                out.append(copy.deepcopy(obj))
        if params.get("limit") is not None and endpoint in ("dcim/platforms", "extras/tags"):
            out = out[: int(params["limit"])]
        return out

    def create(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.bulk_create(endpoint, [data])[0]

    def bulk_create(self, endpoint: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.writes.append(("POST", endpoint.strip("/"), copy.deepcopy(items)))
        created = []
        for item in items:
            self._next_id += 1
            created.append({"id": self._next_id, **copy.deepcopy(item)})
        return created

    def update(self, endpoint: str, object_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self.bulk_update(endpoint, [{"id": object_id, **data}])[0]

    def bulk_update(self, endpoint: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.writes.append(("PATCH", endpoint.strip("/"), copy.deepcopy(items)))
        return copy.deepcopy(items)

    def delete(self, endpoint: str, object_id: int) -> None:
        self.bulk_delete(endpoint, [object_id])

    def bulk_delete(self, endpoint: str, ids: Any) -> None:
        self.writes.append(("DELETE", endpoint.strip("/"), sorted(ids)))

    # ------------------------------------------------------------------ filters
    def _match(self, endpoint: str, obj: dict[str, Any], key: str, value: Any) -> bool:
        if key in ("limit", "offset", "ordering", "brief", "public"):
            return True
        values = [str(v) for v in (value if isinstance(value, list) else [value])]
        if key == "tag":
            known = {t["slug"] for t in self.objects("extras/tags")}
            if unknown := [v for v in values if v not in known]:
                raise NetBoxError("GET", endpoint, 400, {"tag": [f"Select a valid choice. {unknown[0]} is not one"]})
            return any(t["slug"] in values for t in obj.get("tags") or [])
        if key == "platform":
            known = {p["slug"] for p in self.objects("dcim/platforms")}
            if unknown := [v for v in values if v not in known]:
                raise NetBoxError("GET", endpoint, 400, {"platform": [f"{unknown[0]} is not one of the choices"]})
            return (obj.get("platform") or {}).get("slug") in values
        if key in ("id", "name", "slug", "key", "assigned_object_type", "assigned_object_id"):
            return str(obj.get(key)) in values
        if key in ("device_id", "device"):
            device = _device_of(obj) or {}
            return str(device.get("id" if key == "device_id" else "name")) in values
        if key == "address":
            return obj["address"].split("/")[0] in [v.split("/")[0] for v in values]
        if key in ("l2vpn_id", "group_id", "user_id"):
            nested = obj.get(key.removesuffix("_id")) or {}
            return str(nested.get("id")) in values
        raise NotImplementedError(f"FakeNetBox: filter {key!r} on {endpoint}")


def _get(obj: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


@pytest.fixture
def fake() -> FakeNetBox:
    """A fresh copy of the recorded, freshly seeded NetBox."""
    return FakeNetBox()


@pytest.fixture
def topology() -> Topology:
    return load_topology()


def intent_of(fake: FakeNetBox) -> LabIntent:
    return build_intent(load_snapshot(fake), PUBLIC_URL)


@pytest.fixture
def intent(fake: FakeNetBox) -> LabIntent:
    return intent_of(fake)


@pytest.fixture
def make_intent():
    """``make_intent(fake)``: intent of a (mutated) fake NetBox."""
    return intent_of


@pytest.fixture
def empty_netbox() -> FakeNetBox:
    """A NetBox with nothing in it (no tags, platforms, ...)."""
    return FakeNetBox({"netbox_version": _recorded()["netbox_version"], "endpoints": {}})
