"""Where grok-bot finds lab-api and the roles' routines, and how the office behaves.

Every value comes from the process environment, else the repository's ``.env``, else a default. ``.env`` is read
again on every :func:`load_settings` call (like lab-api), so a routine URL or key added to ``.env`` while the bot runs
is used from the next page on, without a restart (the compose service mounts the repository read-only).

* ``BOT_LAB_API_URL``: lab-api (default ``http://127.0.0.1:8090``; the container uses ``http://172.30.10.50:8090``);
  ``LAB_API_TOKEN``: lab-api's write token (dry runs, approved changes, field repairs, acknowledgments);
* ``BOT_AUTONOMY``: ``observe`` (the roles diagnose and report only) or ``propose`` (default: they file change
  tickets that a human approves). ``auto`` is gone: it is read as ``propose`` and reported in the warnings;
* the alert intake: ``BOT_ZABBIX_HOOK_TOKEN`` (Bearer token of ``POST /hooks/zabbix``; empty = the intake is off),
  ``BOT_BATCH_S`` (10: a batch closes this long after its latest alert), ``BOT_MAX_BATCH_S`` (30: at most this long
  after its first);
* the roles on ``/mcp``: ``BOT_NOCENG_TOKEN``, ``BOT_NETENG_TOKEN``, ``BOT_NOCMGR_TOKEN`` (``Authorization: Bearer``;
  the token decides the role); ``BOT_MCP_HOSTS`` (a tunnel's Host names that may reach ``/mcp`` and nothing else);
* the roles' routines: ``BOT_ROUTINE_<ROLE>_URL`` and ``BOT_ROUTINE_<ROLE>_KEY`` (``Authorization: Bearer <key>``)
  for ``NOCENG``, ``NETENG``, ``NOCMGR``; ``BOT_PAGE_MAX_PER_HOUR`` (12 pages per role and hour);
* ``BOT_POLL_S`` (2: the data feed of the room), ``BOT_FIELD_DELAY_S`` (10: dispatched -> on site),
  ``BOT_ASK_TIMEOUT_S`` (600: how long a question waits for the NOC Manager), ``BOT_REVIEW_TIMEOUT_S`` (300: how
  long a change ticket waits for the NOC Manager's review before it goes to the operator without one);
* ``BOT_OPERATOR_TOKEN``: header ``X-Operator-Token`` for approvals and control (the NOC page served on this
  machine carries it); ``BOT_ALLOWED_HOSTS``: Host headers the web server answers (DNS-rebinding guard);
* ``BOT_DATA_DIR``: SQLite database; default ``build/grok-bot`` in the repository;
* ``ZABBIX_PUBLIC_URL``, ``NETBOX_PUBLIC_URL``, ``LAB_API_PUBLIC_URL``, ``BOT_PUBLIC_URL``: links in the page and in
  pages.

Settings of the earlier in-house crew (``XAI_API_KEY``, ``BOT_BRAIN``, ``BOT_PAGER_*``, ``BOT_AGENT_*``, ...) are
ignored if they are still in an older ``.env``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Autonomy = Literal["observe", "propose"]
AUTONOMIES: tuple[Autonomy, ...] = ("observe", "propose")


@dataclass(frozen=True)
class RoleSpec:
    """One of the three NOC roles: its key (also its Zabbix user), its robot in the room, its settings."""

    key: str
    bot: str
    title: str
    color: str
    hat: str

    @property
    def token_env(self) -> str:
        return f"BOT_{self.bot}_TOKEN"

    @property
    def url_env(self) -> str:
        return f"BOT_ROUTINE_{self.bot}_URL"

    @property
    def key_env(self) -> str:
        return f"BOT_ROUTINE_{self.bot}_KEY"


ROLES: dict[str, RoleSpec] = {
    r.key: r
    for r in (
        RoleSpec("noc-engineer", "NOCENG", "NOC Engineer", "#f5b041", "headset"),
        RoleSpec("network-engineer", "NETENG", "Network Engineer", "#4fc3f7", "cap"),
        RoleSpec("noc-manager", "NOCMGR", "NOC Manager", "#f06292", "tie"),
    )
}
ROLE_BY_BOT: dict[str, RoleSpec] = {r.bot: r for r in ROLES.values()}
NOCENG, NETENG, NOCMGR = "noc-engineer", "network-engineer", "noc-manager"


class SettingsError(RuntimeError):
    """A setting is missing or invalid."""


def find_repo_root(start: Path | None = None) -> Path | None:
    """The NetSim repository (the directory with ``contract/topology.yaml`` and ``.env.example``), if any."""
    candidates = [start or Path.cwd(), Path(__file__).resolve()]
    for base in candidates:
        for d in (base, *base.parents):
            if (d / "contract" / "topology.yaml").is_file() and (d / ".env.example").is_file():
                return d
    return None


def default_env_file() -> Path | None:
    explicit = os.environ.get("BOT_ENV_FILE")
    if explicit:
        return Path(explicit)
    root = find_repo_root()
    return root / ".env" if root else None


def read_env_file(path: Path | None) -> dict[str, str]:
    """``KEY=value`` lines (no interpolation; quotes around the whole value are stripped). Missing file: ``{}``."""
    if path is None or not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class Routine:
    """A role's Grok Bot routine ("when a webhook fires"): its webhook URL and key."""

    url: str | None = None
    key: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class BotSettings:
    lab_api_url: str
    lab_api_token: str | None
    autonomy: Autonomy
    poll_s: float
    field_delay_s: float
    batch_s: float
    max_batch_s: float
    ask_timeout_s: float
    review_timeout_s: float
    page_max_per_hour: int
    operator_token: str | None
    hook_token: str | None
    allowed_hosts: list[str]
    data_dir: Path
    zabbix_public_url: str
    netbox_public_url: str
    lab_api_public_url: str
    env_file: Path | None
    public_url: str = "http://127.0.0.1:8095"
    role_tokens: dict[str, str | None] = field(default_factory=dict)
    routines: dict[str, Routine] = field(default_factory=dict)
    mcp_hosts: list[str] = field(default_factory=list)
    warnings: tuple[str, ...] = ()
    actor: str = "grok-bot"

    def routine(self, role: str) -> Routine:
        return self.routines.get(role) or Routine()

    def links(self) -> dict[str, str]:
        return {
            "noc_room": self.public_url,
            "zabbix": self.zabbix_public_url,
            "netbox": self.netbox_public_url,
            "lab_api": self.lab_api_public_url,
        }

    def public(self) -> dict[str, object]:
        """What the NOC page and ``GET /api/state`` may show (no secrets: no tokens, no routine URLs or keys)."""
        return {
            "autonomy": self.autonomy,
            "poll_s": self.poll_s,
            "batch_s": self.batch_s,
            "max_batch_s": self.max_batch_s,
            "field_delay_s": self.field_delay_s,
            "ask_timeout_s": self.ask_timeout_s,
            "review_timeout_s": self.review_timeout_s,
            "page_max_per_hour": self.page_max_per_hour,
            "lab_api_url": self.lab_api_url,
            "lab_write_token": bool(self.lab_api_token),
            "hook": bool(self.hook_token),
            "roles": {
                key: {
                    "bot": spec.bot,
                    "title": spec.title,
                    "mcp_token": bool(self.role_tokens.get(key)),
                    "routine": self.routine(key).configured,
                }
                for key, spec in ROLES.items()
            },
            "warnings": list(self.warnings),
            "links": self.links(),
        }


def _default_data_dir() -> Path:
    root = find_repo_root()
    return root / "build" / "grok-bot" if root else Path("/var/lib/grok-bot")


def _bearer(value: str | None) -> str | None:
    """A key pasted as the whole header value (``Bearer crsr_...``) is stripped to the key."""
    if value and value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return value


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> BotSettings:
    env = os.environ if environ is None else environ
    file = env_file if env_file is not None else default_env_file()
    from_file = read_env_file(file)

    def get(name: str) -> str | None:
        value = env.get(name)
        if value is None or value == "":
            value = from_file.get(name)
        return value.strip() if value and value.strip() else None

    def number(name: str, default: float, lo: float, hi: float) -> float:
        raw = get(name)
        try:
            value = float(raw) if raw is not None else default
        except ValueError:
            raise SettingsError(f"{name} must be a number, not {raw!r}") from None
        if not lo <= value <= hi:
            raise SettingsError(f"{name} must be between {lo:g} and {hi:g}, not {value:g}")
        return value

    warnings: list[str] = []
    autonomy = (get("BOT_AUTONOMY") or "propose").lower()
    if autonomy == "auto":
        warnings.append("BOT_AUTONOMY=auto is gone: every change waits for a human (read as propose)")
        autonomy = "propose"
    if autonomy not in AUTONOMIES:
        raise SettingsError(f"BOT_AUTONOMY must be one of {', '.join(AUTONOMIES)}, not {autonomy!r}")
    batch_s = number("BOT_BATCH_S", 10.0, 0.0, 300)
    max_batch_s = number("BOT_MAX_BATCH_S", 30.0, 0.0, 900)
    if max_batch_s < batch_s:
        raise SettingsError(f"BOT_MAX_BATCH_S ({max_batch_s:g}) must be at least BOT_BATCH_S ({batch_s:g})")
    tokens = {key: get(spec.token_env) for key, spec in ROLES.items()}
    given = [t for t in tokens.values() if t]
    if len(set(given)) != len(given):
        raise SettingsError(
            "BOT_NOCENG_TOKEN, BOT_NETENG_TOKEN and BOT_NOCMGR_TOKEN must differ: the token is the role"
        )
    hosts = get("BOT_ALLOWED_HOSTS") or "127.0.0.1,localhost,172.30.10.60,grok-bot"
    return BotSettings(
        lab_api_url=(get("BOT_LAB_API_URL") or "http://127.0.0.1:8090").rstrip("/"),
        lab_api_token=get("LAB_API_TOKEN"),
        autonomy=autonomy,  # type: ignore[arg-type]
        poll_s=number("BOT_POLL_S", 2.0, 0.2, 60),
        field_delay_s=number("BOT_FIELD_DELAY_S", 10.0, 0.0, 3600),
        batch_s=batch_s,
        max_batch_s=max_batch_s,
        ask_timeout_s=number("BOT_ASK_TIMEOUT_S", 600.0, 5.0, 86_400),
        review_timeout_s=number("BOT_REVIEW_TIMEOUT_S", 300.0, 5.0, 86_400),
        page_max_per_hour=int(number("BOT_PAGE_MAX_PER_HOUR", 12, 0, 600)),
        operator_token=get("BOT_OPERATOR_TOKEN"),
        hook_token=_bearer(get("BOT_ZABBIX_HOOK_TOKEN")),
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        data_dir=Path(get("BOT_DATA_DIR") or _default_data_dir()),
        zabbix_public_url=(get("ZABBIX_PUBLIC_URL") or "http://127.0.0.1:8080").rstrip("/"),
        netbox_public_url=(get("NETBOX_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/"),
        lab_api_public_url=(get("LAB_API_PUBLIC_URL") or "http://127.0.0.1:8090").rstrip("/"),
        env_file=file,
        public_url=(get("BOT_PUBLIC_URL") or "http://127.0.0.1:8095").rstrip("/"),
        role_tokens=tokens,
        routines={key: Routine(url=get(spec.url_env), key=_bearer(get(spec.key_env))) for key, spec in ROLES.items()},
        mcp_hosts=[h.strip() for h in (get("BOT_MCP_HOSTS") or "").split(",") if h.strip()],
        warnings=tuple(warnings),
    )
