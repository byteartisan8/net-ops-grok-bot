"""Where the NetBox tools find NetBox and their credentials.

Each value comes from the process environment, else the repository's ``.env``, else the default:

* ``NETBOX_URL`` (default: the topology's NetBox host port, ``http://127.0.0.1:8000``): base URL for API calls;
  from a container on labnet use ``http://172.30.10.30:8080``;
* ``NETBOX_PUBLIC_URL`` (same default): base of the UI links written into the BSM spec (what a browser opens);
* ``NETBOX_TOKEN_RO``: read-only v2 token of user ``netsim-bot`` (intent rendering, BSM spec, queries);
* ``NETBOX_TOKEN_RW``: superuser v2 token (seeding, journal entries);
* ``NETBOX_TIMEOUT`` (default ``30``): HTTP timeout in seconds.

``netbox-bootstrap`` writes both tokens into ``.env`` (contract section 11).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from netsim_netbox.client import NetBoxClient
from netsim_netbox.envfile import default_env_file, read_env_file

TOKEN_RW = "NETBOX_TOKEN_RW"
TOKEN_RO = "NETBOX_TOKEN_RO"


class SettingsError(RuntimeError):
    """A required setting (usually a token) is missing."""


def default_url() -> str:
    from netsim_common.constants import lab_constants

    return lab_constants().netbox_url


@dataclass(frozen=True)
class Settings:
    url: str
    public_url: str
    token_rw: str | None = None
    token_ro: str | None = None
    timeout: float = 30.0
    env_file: Path | None = None

    def client(self, *, write: bool = False) -> NetBoxClient:
        """API client with the read-only token (default) or the read-write token (``write=True``)."""
        name, token = (TOKEN_RW, self.token_rw) if write else (TOKEN_RO, self.token_ro)
        if not token:
            raise SettingsError(f"{name} is not set (run `make netbox-bootstrap`, which writes it into .env)")
        return NetBoxClient(self.url, token, timeout=self.timeout)


def get_setting(name: str, environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> str | None:
    """One value, looked up like the settings above (environment, then ``.env``)."""
    env = os.environ if environ is None else environ
    return env.get(name) or read_env_file(env_file if env_file is not None else default_env_file()).get(name) or None


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings:
    """Settings from ``environ`` (default ``os.environ``), then ``env_file`` (default: the repo's ``.env``)."""
    env = os.environ if environ is None else environ
    file = env_file if env_file is not None else default_env_file()
    from_file = read_env_file(file)

    def get(name: str) -> str | None:
        return env.get(name) or from_file.get(name) or None

    url = (get("NETBOX_URL") or default_url()).rstrip("/")
    public = (get("NETBOX_PUBLIC_URL") or default_url()).rstrip("/")
    return Settings(
        url=url,
        public_url=public,
        token_rw=get(TOKEN_RW),
        token_ro=get(TOKEN_RO),
        timeout=float(get("NETBOX_TIMEOUT") or 30),
        env_file=file,
    )
