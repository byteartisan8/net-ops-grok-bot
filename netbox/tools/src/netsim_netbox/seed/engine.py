"""Reconcile NetBox with a desired state (list of :class:`~netsim_netbox.seed.model.Spec`).

Per phase (see :data:`~netsim_netbox.seed.desired.PHASES`): fetch the candidates, match them to specs by natural
key, compare only the managed fields (after normalizing NetBox's read format: nested objects -> ID, choices and
selection custom fields -> raw value, M2M -> sorted IDs), then bulk-create what is missing and PATCH only the fields
that differ. A run against an up-to-date NetBox makes no write request at all.

``prune`` then deletes objects tagged ``netsim-lab`` that the desired state no longer contains, in reverse
dependency order. Independently of ``prune``, a lab L2VPN's terminations are reconciled as a set: a ``vpws`` L2VPN
holds at most two terminations, so a termination that is not desired is removed before a desired one is added.

When something changed (and not in a dry run), every affected device gets a journal entry listing the changes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from netsim_netbox.client import NetBoxClient, NetBoxError
from netsim_netbox.objects import INTERFACE_OBJECT, KINDS, LAB_TAG
from netsim_netbox.seed.desired import PHASES
from netsim_netbox.seed.model import Change, Key, Ref, SeedReport, Spec

#: Deletion order of ``--prune`` (dependents first).
PRUNE_ORDER = (
    "extras.eventrule",
    "extras.webhook",
    "vpn.l2vpntermination",
    "vpn.l2vpn",
    "ipam.ipaddress",
    "dcim.interface",
    "dcim.device",
    "ipam.vlan",
    "ipam.vlangroup",
    "tenancy.tenant",
    "tenancy.tenantgroup",
    "dcim.devicerole",
    "dcim.platform",
    "dcim.devicetype",
    "dcim.manufacturer",
    "dcim.site",
    "dcim.region",
)
WEBHOOK_KINDS = frozenset({"extras.webhook", "extras.eventrule"})
#: List fields whose order matters (all other lists compare as multisets).
ORDERED_FIELDS = frozenset({"extra_choices"})
SECRET_FIELDS = frozenset({"secret"})
JOURNAL_MAX_LINES = 60
#: Max values of one repeated query parameter (URL length).
QUERY_CHUNK = 100


class SeedError(RuntimeError):
    pass


class _New:
    """Placeholder ID of an object a dry run would create."""

    def __repr__(self) -> str:
        return "<new>"


NEW = _New()


# --------------------------------------------------------------------------------------------- normalization
def natural_key(kind: str, obj: dict[str, Any]) -> Key | None:
    """The key a NetBox object is matched on (``None`` = not matchable, e.g. a termination on a VLAN)."""
    match kind:
        case "extras.tag":
            return (obj["slug"],)
        case "extras.customfieldchoiceset" | "extras.customfield" | "extras.webhook" | "extras.eventrule":
            return (obj["name"],)
        case "dcim.device":
            return (obj["name"],)
        case "dcim.interface":
            return (obj["device"]["name"], obj["name"])
        case "ipam.ipaddress":
            return (obj["address"].split("/")[0],)
        case "ipam.vlan":
            group = obj.get("group") or {}
            return (group.get("slug"), obj["name"])
        case "vpn.l2vpntermination":
            target = obj.get("assigned_object") or {}
            if obj.get("assigned_object_type") != INTERFACE_OBJECT or "device" not in target:
                return None
            return (target["device"]["name"], target["name"])
        case _:
            return (obj["slug"],)


def normalize(value: Any) -> Any:
    """NetBox read format -> comparable write format."""
    if isinstance(value, dict):
        if "value" in value and "label" in value and len(value) <= 3:
            return value["value"]  # choice field or selection custom field (4.7: {"value", "label"})
        if "id" in value:
            return value["id"]
        return value
    if isinstance(value, list) and value and all(isinstance(v, dict) and "id" in v for v in value):
        return sorted(v["id"] for v in value)
    return value


def same(field: str, have: Any, want: Any) -> bool:
    if isinstance(have, list) and isinstance(want, list):
        if field in ORDERED_FIELDS:
            return have == want
        dump = lambda v: json.dumps(v, sort_keys=True, default=str)  # noqa: E731
        return sorted(map(dump, have)) == sorted(map(dump, want))
    return have == want


def show(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, Ref):
        return str(value)
    if isinstance(value, dict):
        if "value" in value and "label" in value:
            return str(value["value"])
        return str(value.get("display") or value.get("name") or value.get("id"))
    if isinstance(value, list):
        return "[" + ", ".join(show(v) for v in value) + "]"
    return json.dumps(value) if isinstance(value, str) and (not value or " " in value) else str(value)


# --------------------------------------------------------------------------------------------- engine
class Seeder:
    """Apply a desired state. ``dry_run`` never writes; ``prune`` deletes stale ``netsim-lab`` objects."""

    def __init__(
        self,
        client: NetBoxClient,
        *,
        dry_run: bool = False,
        prune: bool = False,
        manage_webhooks: bool = True,
        source: str = "contract/topology.yaml",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.prune = prune
        self.manage_webhooks = manage_webhooks
        self.source = source
        self.now = now
        self.ids: dict[tuple[str, Key], Any] = {}
        self.report = SeedReport(dry_run=dry_run)

    # ------------------------------------------------------------------ public
    def run(self, specs: list[Spec]) -> SeedReport:
        self.report.netbox_version = str(self.client.status().get("netbox-version", ""))
        by_phase: dict[str, list[Spec]] = defaultdict(list)
        for spec in specs:
            by_phase[spec.phase].append(spec)
        unknown = set(by_phase) - {p for p, _ in PHASES}
        if unknown:
            raise SeedError(f"specs in unknown phases: {sorted(unknown)}")
        for phase, kind in PHASES:
            if by_phase.get(phase):
                if kind == "vpn.l2vpntermination":
                    self._trim_terminations(by_phase[phase])
                self._phase(kind, by_phase[phase])
        if self.prune:
            self._prune(specs)
        if not self.dry_run:
            self.report.journal_entries = self._journal()
        return self.report

    # ------------------------------------------------------------------ fetch
    def _device_ids(self, specs: list[Spec]) -> list[int]:
        hosts = sorted({s.device for s in specs if s.device})
        return [i for h in hosts if isinstance(i := self.ids.get(("dcim.device", (h,))), int)]

    def _lab_tag_exists(self) -> bool:
        """NetBox rejects ``?tag=`` filters on unknown tags (HTTP 400), e.g. in a dry run on an empty NetBox."""
        return isinstance(self.ids.get(("extras.tag", (LAB_TAG,))), int)

    def _fetch(self, kind: str, specs: list[Spec]) -> list[dict[str, Any]]:
        endpoint = KINDS[kind].endpoint
        tagged = [{"tag": LAB_TAG}] if self._lab_tag_exists() else []
        queries: list[dict[str, Any]] = []
        if kind in ("dcim.interface", "ipam.ipaddress", "vpn.l2vpntermination"):
            if device_ids := self._device_ids(specs):
                queries.append({"device_id": device_ids})
            queries += tagged
            if kind == "ipam.ipaddress":
                queries.append({"address": sorted({s.key[0] for s in specs})})
        elif kind == "dcim.device":
            queries += [{"name": sorted({s.key[0] for s in specs})}, *tagged]
        else:
            queries.append({})
        seen: dict[int, dict[str, Any]] = {}
        for query in queries:
            for obj in self.client.list(endpoint, **query):
                seen.setdefault(obj["id"], obj)
        return list(seen.values())

    # ------------------------------------------------------------------ resolve
    def _resolve(self, value: Any, spec: Spec) -> Any:
        if isinstance(value, Ref):
            ident = self.ids.get((value.kind, value.key))
            if ident is None and (value.kind, value.key) not in self.ids:
                if self.dry_run:
                    return NEW
                raise SeedError(f"{spec.label}: {KINDS[value.kind].label} {value} does not exist")
            return NEW if ident is None else ident
        if isinstance(value, list):
            return [self._resolve(v, spec) for v in value]
        return value

    # ------------------------------------------------------------------ diff
    def _diff(self, spec: Spec, obj: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """(PATCH body, human-readable details) of the managed fields that differ."""
        body: dict[str, Any] = {}
        details: list[str] = []
        for field, want in spec.fields.items():
            resolved = self._resolve(want, spec)
            have = normalize(obj.get(field))
            if not same(field, have, resolved):
                body[field] = resolved
                old, new = ("***", "***") if field in SECRET_FIELDS else (show(obj.get(field)), show(want))
                details.append(f"{field}: {old} -> {new}")
        cf_have = obj.get("custom_fields") or {}
        cf_body = {}
        for name, want in spec.custom_fields.items():
            resolved = self._resolve(want, spec)  # object custom fields hold references
            have = normalize(cf_have.get(name))
            if have != resolved:
                cf_body[name] = resolved
                details.append(f"cf.{name}: {show(cf_have.get(name))} -> {show(want)}")
        if cf_body:
            body["custom_fields"] = cf_body
        if spec.tags and KINDS[spec.kind].taggable:
            have_tags = {t["slug"] for t in obj.get("tags") or []}
            if missing := set(spec.tags) - have_tags:
                body["tags"] = [{"slug": s} for s in sorted(have_tags | missing)]
                details.append(f"tags: +{', '.join(sorted(missing))}")
        return body, details

    def _create_body(self, spec: Spec) -> dict[str, Any]:
        body = {f: self._resolve(v, spec) for f, v in spec.fields.items()}
        body = {f: v for f, v in body.items() if v is not None}  # unset = NetBox default (null / empty)
        if cf := {k: self._resolve(v, spec) for k, v in spec.custom_fields.items() if v is not None}:
            body["custom_fields"] = cf
        if spec.tags and KINDS[spec.kind].taggable:
            body["tags"] = [{"slug": s} for s in spec.tags]
        return body

    # ------------------------------------------------------------------ phases
    def _phase(self, kind: str, specs: list[Spec]) -> None:
        endpoint = KINDS[kind].endpoint
        index: dict[Key, dict[str, Any]] = {}
        for obj in self._fetch(kind, specs):
            key = natural_key(kind, obj)
            if key is not None and key not in index:
                index[key] = obj
                self.ids[(kind, key)] = obj["id"]
        creates: list[Spec] = []
        updates: list[tuple[Spec, dict[str, Any], dict[str, Any], list[str]]] = []
        for spec in specs:
            obj = index.get(spec.key)
            if obj is None:
                if spec.update_only:
                    if self.dry_run and (kind, spec.key) in self.ids:  # created earlier in this dry run
                        fields = ", ".join(f"{f}: {show(v)}" for f, v in spec.fields.items())
                        self.report.changes.append(Change("update", kind, spec.key, [fields], spec.device))
                        continue
                    raise SeedError(f"{spec.label} does not exist (phase {spec.phase})")
                creates.append(spec)
                continue
            body, details = self._diff(spec, obj)
            if body:
                updates.append((spec, obj, body, details))
        if creates:
            bodies = [self._create_body(s) for s in creates]
            if self.dry_run:
                for spec in creates:
                    self.ids[(kind, spec.key)] = None
                    self.report.changes.append(Change("create", kind, spec.key, [], spec.device))
            else:
                created = self._write("POST", endpoint, bodies, creates)
                for spec, obj in zip(creates, created, strict=True):
                    self.ids[(kind, spec.key)] = obj["id"]
                    self.report.changes.append(Change("create", kind, spec.key, [], spec.device, obj["id"]))
        if updates:
            if not self.dry_run:
                self._write(
                    "PATCH", endpoint, [{"id": o["id"], **b} for _, o, b, _ in updates], [u[0] for u in updates]
                )
            for spec, obj, _, details in updates:
                self.report.changes.append(Change("update", kind, spec.key, details, spec.device, obj["id"]))

    def _write(self, method: str, endpoint: str, bodies: list[dict[str, Any]], specs: list[Spec]) -> list[Any]:
        try:
            if method == "POST":
                return self.client.bulk_create(endpoint, bodies)
            return self.client.bulk_update(endpoint, bodies)
        except NetBoxError as exc:
            labels = ", ".join(s.label for s in specs[:5]) + (" ..." if len(specs) > 5 else "")
            raise SeedError(f"{method} {endpoint} failed for {labels}: {exc.detail}") from exc

    def _trim_terminations(self, specs: list[Spec]) -> None:
        """Remove terminations of lab L2VPNs that are not desired, so desired ones fit (vpws: at most two)."""
        wanted = {(s.fields["l2vpn"].key, s.key) for s in specs}
        l2vpn_ids = {self.ids.get(("vpn.l2vpn", s.fields["l2vpn"].key)): s.fields["l2vpn"].key for s in specs}
        l2vpn_ids = {i: k for i, k in l2vpn_ids.items() if isinstance(i, int)}
        if not l2vpn_ids:
            return
        stale = []
        ids = sorted(l2vpn_ids)
        for start in range(0, len(ids), QUERY_CHUNK):  # keep the query string short at scale
            for obj in self.client.list("vpn/l2vpn-terminations", l2vpn_id=ids[start : start + QUERY_CHUNK]):
                l2vpn_key = l2vpn_ids[obj["l2vpn"]["id"]]
                if (l2vpn_key, natural_key("vpn.l2vpntermination", obj)) not in wanted:
                    stale.append(obj)
        for obj in stale:
            key = natural_key("vpn.l2vpntermination", obj) or (obj["display"],)
            device = key[0] if len(key) == 2 else None
            self.report.changes.append(
                Change("delete", "vpn.l2vpntermination", key, [f"from L2VPN {obj['l2vpn']['name']}"], device, obj["id"])
            )
        if stale and not self.dry_run:
            self.client.bulk_delete("vpn/l2vpn-terminations", [o["id"] for o in stale])

    def _prune(self, specs: list[Spec]) -> None:
        if not self._lab_tag_exists():
            return  # nothing can carry the tag yet
        desired = {(s.kind, s.key) for s in specs}
        for kind in PRUNE_ORDER:
            if kind in WEBHOOK_KINDS and not self.manage_webhooks:
                continue
            endpoint = KINDS[kind].endpoint
            stale = [o for o in self.client.list(endpoint, tag=LAB_TAG) if (kind, natural_key(kind, o)) not in desired]
            if kind == "dcim.interface":  # subinterfaces before their parents
                stale.sort(key=lambda o: o.get("parent") is None)
            for obj in stale:
                key = natural_key(kind, obj) or (obj.get("display") or obj["id"],)
                device = key[0] if kind in ("dcim.interface", "vpn.l2vpntermination", "dcim.device") else None
                self.report.changes.append(Change("delete", kind, key, [], device, obj["id"]))
            if stale and not self.dry_run:
                try:
                    if kind == "dcim.interface":
                        for obj in stale:
                            self.client.delete(endpoint, obj["id"])
                    else:
                        self.client.bulk_delete(endpoint, [o["id"] for o in stale])
                except NetBoxError as exc:
                    raise SeedError(f"prune: cannot delete {KINDS[kind].label}(s): {exc.detail}") from exc

    # ------------------------------------------------------------------ journal
    def _journal(self) -> int:
        by_device: dict[str, list[Change]] = defaultdict(list)
        for change in self.report.changes:
            if change.device:
                by_device[change.device].append(change)
        stamp = self.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        entries = []
        for host, changes in sorted(by_device.items()):
            device_id = self.ids.get(("dcim.device", (host,)))
            if not isinstance(device_id, int) or any(c.kind == "dcim.device" and c.action == "delete" for c in changes):
                continue
            lines = [f"- {c}" for c in changes]
            if len(lines) > JOURNAL_MAX_LINES:
                lines = [*lines[:JOURNAL_MAX_LINES], f"- ... and {len(lines) - JOURNAL_MAX_LINES} more"]
            text = (
                f"**Seeded by `netbox-seed`** from `{self.source}` at {stamp}: {len(changes)} change(s).\n\n"
                + "\n".join(lines)
            )
            entries.append(
                {
                    "assigned_object_type": "dcim.device",
                    "assigned_object_id": device_id,
                    "kind": "info",
                    "comments": text,
                    "tags": [{"slug": LAB_TAG}],
                }
            )
        if entries:
            self.client.bulk_create(KINDS["extras.journalentry"].endpoint, entries)
        return len(entries)
