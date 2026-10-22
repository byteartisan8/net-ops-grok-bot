"""Where the Zabbix tools find Zabbix, NetBox and their credentials.

Each value comes from the process environment, else the repository's ``.env``, else the default:

* ``ZABBIX_URL`` (default: the topology's Zabbix host port, ``http://127.0.0.1:8080``): frontend/API base URL;
  from a container on labnet use ``http://172.30.10.41:8080``;
* ``ZABBIX_API_TOKEN``: API token of the dedicated user ``ZABBIX_API_USER`` (provisioning, sync, check-now webhook,
  lab-api); written into ``.env`` by ``zabbix-bootstrap``;
* ``ZABBIX_ADMIN_PASSWORD``, ``ZABBIX_API_USER``, ``ZABBIX_API_PASSWORD``: used only by ``zabbix-bootstrap``;
* ``BOT_ZABBIX_HOOK_TOKEN`` (the secret macro of the NOC media type) and the NOC role users' tokens
  ``ZABBIX_NOCENG_TOKEN``, ``ZABBIX_NETENG_TOKEN``, ``ZABBIX_NOCMGR_TOKEN`` (written by ``zabbix-bootstrap``; lab-api
  acknowledges problems with them);
* ``NETSIM_BSM_SOURCE`` (default ``netbox``): where ``zabbix-provision`` / ``zabbix-sync`` take the BSM spec from
  (``netbox`` = ``netbox-intent bsm-spec`` with ``NETBOX_TOKEN_RO``; ``topology`` = ``contract/topology.yaml``);
* ``ZABBIX_TIMEOUT`` (default ``30``): HTTP timeout in seconds.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from netsim_netbox.envfile import default_env_file, read_env_file

from netsim_zabbix.api import ZabbixApi

TOKEN = "ZABBIX_API_TOKEN"
SOURCES = ("netbox", "topology")
NOC_TOKENS = ("ZABBIX_NOCENG_TOKEN", "ZABBIX_NETENG_TOKEN", "ZABBIX_NOCMGR_TOKEN")


class SettingsError(RuntimeError):
    """A required setting (usually the API token) is missing."""


def default_url() -> str:
    from netsim_common.constants import lab_constants

    return lab_constants().zabbix_url


@dataclass(frozen=True)
class Settings:
    url: str
    token: str | None = None
    admin_password: str | None = None
    api_user: str = "netsim-api"
    api_password: str | None = None
    bsm_source: str = "netbox"
    timeout: float = 30.0
    env_file: Path | None = None
    bot_hook_token: str | None = None
    noc_tokens: dict[str, str | None] = field(default_factory=dict)

    def api(self) -> ZabbixApi:
        """Client authenticated with ``ZABBIX_API_TOKEN``."""
        if not self.token:
            raise SettingsError(f"{TOKEN} is not set (run `make zabbix-bootstrap`, which writes it into .env)")
        return ZabbixApi(self.url, self.token, timeout=self.timeout)


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
    """Settings from ``environ`` (default ``os.environ``), then ``env_file`` (default: the repo's ``.env``)."""
    env = os.environ if environ is None else environ
    file = env_file if env_file is not None else default_env_file()
    from_file = read_env_file(file)

    def get(name: str) -> str | None:
        return env.get(name) or from_file.get(name) or None

    source = get("NETSIM_BSM_SOURCE") or "netbox"
    if source not in SOURCES:
        raise SettingsError(f"NETSIM_BSM_SOURCE must be one of {', '.join(SOURCES)}, not {source!r}")
    return Settings(
        url=(get("ZABBIX_URL") or default_url()).rstrip("/"),
        token=get(TOKEN),
        admin_password=get("ZABBIX_ADMIN_PASSWORD"),
        api_user=get("ZABBIX_API_USER") or "netsim-api",
        api_password=get("ZABBIX_API_PASSWORD"),
        bsm_source=source,
        timeout=float(get("ZABBIX_TIMEOUT") or 30),
        env_file=file,
        bot_hook_token=get("BOT_ZABBIX_HOOK_TOKEN"),
        noc_tokens={name: get(name) for name in NOC_TOKENS},
    )
