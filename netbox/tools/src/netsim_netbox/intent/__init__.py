"""The lab's intent, read from NetBox only (never ``contract/topology.yaml``): edit NetBox and the intent changes.

    from netsim_netbox.intent import load_intent, render_device, bsm_spec, blast_radius

    intent = load_intent()                       # NETBOX_URL + NETBOX_TOKEN_RO (environment or .env)
    config = render_device("spoke-2", intent)    # netsim_common.DeviceConfig
    spec = bsm_spec(intent)                      # netsim_common.BsmSpec (contract section 9.3)
    blast_radius("hub-a", intent)                # [ServiceImpact(service_id="SVC-0001", impact="degraded", ...)]

Every function also works without ``intent`` (it then loads one). One :class:`LabIntent` is a consistent snapshot:
load it once per question. ``write_journal`` needs ``NETBOX_TOKEN_RW``.
"""

from __future__ import annotations

from typing import Any

from netsim_common.bsm import BsmSpec
from netsim_common.device_config import DeviceConfig
from netsim_netbox.client import NetBoxClient
from netsim_netbox.intent import bsm as _bsm
from netsim_netbox.intent import journal as _journal
from netsim_netbox.intent import queries as _queries
from netsim_netbox.intent import render as _render
from netsim_netbox.intent.journal import JOURNAL_KINDS, JournalError, ObjectRef, resolve_object
from netsim_netbox.intent.model import (
    CustomerIntent,
    DeviceIntent,
    IntentError,
    IntentProblem,
    InterfaceIntent,
    LabIntent,
    PwEndIntent,
    PwIntent,
    build_intent,
)
from netsim_netbox.intent.queries import ExpectedPw, ServiceImpact, ServiceOnDevice
from netsim_netbox.intent.snapshot import Snapshot, load_snapshot
from netsim_netbox.settings import load_settings

__all__ = [
    "JOURNAL_KINDS",
    "CustomerIntent",
    "DeviceIntent",
    "ExpectedPw",
    "IntentError",
    "IntentProblem",
    "InterfaceIntent",
    "JournalError",
    "LabIntent",
    "PwEndIntent",
    "PwIntent",
    "ServiceImpact",
    "ServiceOnDevice",
    "Snapshot",
    "blast_radius",
    "bsm_spec",
    "build_intent",
    "expected_pws",
    "load_intent",
    "load_snapshot",
    "render_all",
    "render_device",
    "resolve_object",
    "service_detail",
    "services_on_device",
    "write_journal",
]


def load_intent(client: NetBoxClient | None = None, *, public_url: str | None = None) -> LabIntent:
    """Read NetBox (default: settings' URL and read-only token) and interpret it."""
    settings = load_settings()
    if client is None:
        with settings.client() as ro:
            snapshot = load_snapshot(ro)
    else:
        snapshot = load_snapshot(client)
    return build_intent(snapshot, public_url or settings.public_url)


def _intent(intent: LabIntent | None) -> LabIntent:
    return intent if intent is not None else load_intent()


def render_device(hostname: str, intent: LabIntent | None = None) -> DeviceConfig:
    """Intended ``netsim-device`` config of ``hostname`` (contract section 10 rendering rule)."""
    return _render.render_device(_intent(intent), hostname)


def render_all(intent: LabIntent | None = None) -> dict[str, DeviceConfig]:
    return _render.render_all(_intent(intent))


def bsm_spec(intent: LabIntent | None = None) -> BsmSpec:
    """BSM spec (contract section 9.3) from NetBox."""
    return _bsm.bsm_spec(_intent(intent))


def services_on_device(host: str, intent: LabIntent | None = None) -> list[ServiceOnDevice]:
    return _queries.services_on_device(_intent(intent), host)


def expected_pws(host: str, intent: LabIntent | None = None) -> list[ExpectedPw]:
    return _queries.expected_pws(_intent(intent), host)


def blast_radius(host: str, intent: LabIntent | None = None) -> list[ServiceImpact]:
    return _queries.blast_radius(_intent(intent), host)


def service_detail(service_id: str, intent: LabIntent | None = None) -> dict[str, Any]:
    return _queries.service_detail(_intent(intent), service_id)


def write_journal(obj: ObjectRef, message: str, kind: str = "info", client: NetBoxClient | None = None) -> dict:
    """Journal entry on a NetBox object (default client: settings' URL and ``NETBOX_TOKEN_RW``)."""
    if client is not None:
        return _journal.write_journal(client, obj, message, kind)
    with load_settings().client(write=True) as rw:
        return _journal.write_journal(rw, obj, message, kind)
